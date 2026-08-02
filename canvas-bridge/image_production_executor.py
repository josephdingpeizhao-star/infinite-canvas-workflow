"""Production workflow adapter for prompt integrity and ordered image renders."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Mapping

import ic_client
from executor_contract import (
    ExecutionRequest,
    ExecutionResult,
    Executor,
    ExecutorContext,
    ExecutorExecutionError,
)
from failure_text_safety import is_disclosable
from openai_image_executor import OpenAIImageExecutor
from render_task_assembler import (
    RenderTaskAssemblyError,
    RenderTaskPlan,
    assemble_render_tasks,
    resolve_final_prompt_index_path,
)


ROOT = Path(__file__).resolve().parents[1]
_RENDER_FAILURE_FIELDS = (
    "code",
    "http_status",
    "provider_error_type",
    "provider_error_code",
    "provider_request_id",
    "timeout_seconds",
    "missing_files",
    "missing_count",
    "remaining_count",
)


def _first_path(value: Any, label: str) -> Path:
    if isinstance(value, list):
        if not value:
            raise ExecutorExecutionError(f"{label} 未声明路径")
        value = value[0]
    if not isinstance(value, str) or not value.strip():
        raise ExecutorExecutionError(f"{label} 未声明路径")
    return Path(value)


def _wrapped_render_failure(
    message: str,
    cause: BaseException,
    *,
    successful_count: int,
    planned_count: int,
    skipped_count: int,
) -> ExecutorExecutionError:
    message = message[:200]
    failure = ExecutorExecutionError(message)
    for name in _RENDER_FAILURE_FIELDS:
        if hasattr(cause, name):
            setattr(failure, name, getattr(cause, name))
    if not hasattr(cause, "code"):
        if isinstance(cause, ic_client.CanvasAgentError):
            failure.code = "render_canvas_unavailable"
        elif is_disclosable(message):
            failure.code = "render_pipeline_error"
    failure.successful_count = successful_count
    failure.planned_count = planned_count
    failure.skipped_count = skipped_count
    return failure


class ImageProductionExecutor:
    """Run the prompts-only gate or render its accepted task sequence."""

    name = "image-production"

    def __init__(
        self,
        context: ExecutorContext,
        *,
        subprocess_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        image_executor_factory: Callable[[ExecutorContext], Executor] | None = None,
        repo_report_dir: Path | None = None,
        integrity_script: Path | None = None,
        task_assembler: Callable[[Mapping[str, Any], Path], RenderTaskPlan] | None = None,
    ) -> None:
        self.context = context
        self.manifest = context.manifest
        self.environment = context.environment
        self.subprocess_runner = subprocess_runner or subprocess.run
        self.image_executor_factory = image_executor_factory or OpenAIImageExecutor
        self.repo_report_dir = repo_report_dir or ROOT / "reports"
        self.integrity_script = integrity_script or ROOT / "scripts" / "validate_final_prompt_integrity.py"
        self.task_assembler = task_assembler or assemble_render_tasks

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        if request.step not in {"integrity", "renders"}:
            raise ExecutorExecutionError("image-production 仅接受 integrity 或 renders")
        if request.step == "integrity":
            return self._execute_integrity()
        return self._execute_renders(request)

    def _external_report_path(self) -> Path:
        artifacts = self.manifest.get("artifacts")
        if not isinstance(artifacts, Mapping):
            raise ExecutorExecutionError("manifest.artifacts 缺失")
        return _first_path(artifacts.get("qc_reports"), "artifacts.qc_reports") / "final_prompt_integrity_report.json"

    def _execute_integrity(self) -> ExecutionResult:
        manifest_path = self.context.manifest_path
        if manifest_path is None:
            raise ExecutorExecutionError("image-production integrity 缺少 batch manifest 路径")
        report_path = self._external_report_path()
        command = [
            sys.executable,
            str(self.integrity_script),
            "--prompts-only",
            "--batch-manifest",
            str(manifest_path),
            "--repo-report-dir",
            str(self.repo_report_dir),
        ]
        try:
            completed = self.subprocess_runner(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ExecutorExecutionError("完整性门禁进程无法启动") from exc
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ExecutorExecutionError("完整性门禁未生成有效报告") from exc
        if not isinstance(report, dict):
            raise ExecutorExecutionError("完整性门禁未生成有效报告")
        status = str(report.get("status") or "")
        render_blocked = report.get("render_blocked") is True
        if completed.returncode != 0 or status not in {"pass", "needs_review"} or render_blocked:
            raise ExecutorExecutionError("完整性门禁未通过，渲染保持阻断")
        return ExecutionResult(
            detail=f"完整性门禁通过：{status}",
            outputs=(report_path,),
            provider=self.name,
            metadata={
                "status": status,
                "render_blocked": False,
                "checked_prompt_count": int(report.get("checked_prompt_count") or 0),
                "report_path": str(report_path),
            },
        )

    def _render_limit(self) -> int | None:
        raw = (self.environment.get("RENDER_MAX_IMAGES") or "").strip()
        if not raw:
            return None
        try:
            value = int(raw)
        except ValueError as exc:
            raise ExecutorExecutionError("RENDER_MAX_IMAGES 必须是正整数") from exc
        if value <= 0 or str(value) != raw:
            raise ExecutorExecutionError("RENDER_MAX_IMAGES 必须是正整数")
        return value

    def _execute_renders(self, request: ExecutionRequest) -> ExecutionResult:
        if self.environment.get("RENDER_ALLOW_REAL_EXECUTION") != "1":
            raise ExecutorExecutionError("真实渲染未开启：RENDER_ALLOW_REAL_EXECUTION 必须为 1")
        api_key = (self.environment.get("OPENAI_API_KEY") or "").strip()
        if not api_key:
            raise ExecutorExecutionError("服务器未配置 OPENAI_API_KEY")
        limit = self._render_limit()
        try:
            index_path = resolve_final_prompt_index_path(self.manifest)
            plan = self.task_assembler(self.manifest, index_path)
        except (RenderTaskAssemblyError, OSError, ValueError) as exc:
            reason = self._sanitize_reason(exc, api_key, ())
            failure = ExecutorExecutionError(f"渲染任务组装失败：{reason}")
            for name in _RENDER_FAILURE_FIELDS:
                if hasattr(exc, name):
                    setattr(failure, name, getattr(exc, name))
            raise failure from exc

        selected = plan.tasks if limit is None else plan.tasks[:limit]
        planned_count = len(selected)
        skipped_count = len(plan.skipped)
        if not selected:
            return ExecutionResult(
                detail=f"成功 0/计划 0（跳过 {skipped_count}）",
                provider=self.name,
                metadata=self._render_metadata(plan, selected, successful_count=0),
            )
        try:
            image_executor = self.image_executor_factory(self.context)
        except Exception as exc:
            reason = self._sanitize_reason(exc, api_key, selected)
            raise _wrapped_render_failure(
                reason,
                exc,
                successful_count=0,
                planned_count=planned_count,
                skipped_count=skipped_count,
            ) from exc

        outputs: list[Path] = []
        successful_count = 0
        model = ""
        for task in selected:
            try:
                result = image_executor.execute(
                    ExecutionRequest(step="renders", payload=task, metadata=request.metadata)
                )
            except Exception as exc:
                reason = self._sanitize_reason(exc, api_key, selected)
                raise _wrapped_render_failure(
                    reason,
                    exc,
                    successful_count=successful_count,
                    planned_count=planned_count,
                    skipped_count=skipped_count,
                ) from exc
            successful_count += 1
            outputs.extend(result.outputs)
            model = result.model or model
        return ExecutionResult(
            detail=f"成功 {successful_count}/计划 {planned_count}（跳过 {skipped_count}）",
            outputs=tuple(outputs),
            provider=self.name,
            model=model,
            metadata=self._render_metadata(plan, selected, successful_count=successful_count),
        )

    @staticmethod
    def _render_metadata(
        plan: RenderTaskPlan,
        selected: tuple[Any, ...],
        *,
        successful_count: int,
    ) -> dict[str, Any]:
        selected_ids = tuple(task.output_path.stem for task in selected)
        return {
            "full_missing_count": len(plan.tasks),
            "selected_count": len(selected),
            "successful_count": successful_count,
            "remaining_count": len(plan.tasks) - len(selected),
            "skipped_count": len(plan.skipped),
            "planned": plan.planned,
            "selected": selected_ids,
            "skipped": plan.skipped,
        }

    @staticmethod
    def _sanitize_reason(exc: Exception, api_key: str, tasks: tuple[Any, ...]) -> str:
        message = str(exc).replace(api_key, "[REDACTED]") if api_key else str(exc)
        for task in tasks:
            prompt = getattr(task, "prompt", "")
            if prompt:
                message = message.replace(prompt, "[PROMPT]")
        message = " ".join(message.split())
        return (message or "图片服务执行失败")[:240]
