"""Execute QC repair work orders one at a time through image-production."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Callable, Mapping

import run_controller
from executor_contract import (
    ExecutionRequest,
    ExecutionResult,
    Executor,
    ExecutorContext,
    ExecutorExecutionError,
)
from image_production_executor import ImageProductionExecutor
from openai_image_executor import OpenAIImageExecutor
from qc_repair import RepairPlan, RepairWorkOrder
from render_task_assembler import RenderTaskPlan
from workflow_production_projection import artifact_from_path, read_png_dimensions
from workflow_production_render_observer import ProductionRenderObserverExecutor


def _first_path(value: Any, label: str) -> Path:
    if isinstance(value, list):
        value = value[0] if value else None
    if not isinstance(value, str) or not value.strip():
        raise ExecutorExecutionError(f"{label} 未声明路径")
    return Path(value)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _render_snapshot(renders_dir: Path) -> dict[str, str]:
    if not renders_dir.is_dir():
        return {}
    return {
        path.name: _sha256(path)
        for path in sorted(renders_dir.glob("*.png"), key=lambda item: item.name)
        if path.is_file()
    }


def _valid_existing(order: RepairWorkOrder) -> bool:
    try:
        read_png_dimensions(order.task.output_path)
    except (OSError, ValueError):
        return False
    return True


class QcRepairExecutor:
    """Continue after item failures while never retrying an item automatically."""

    name = "qc-repair"

    def __init__(
        self,
        context: ExecutorContext,
        *,
        plan: RepairPlan,
        journal_path: Path,
        request_id: str,
        image_executor_factory: Callable[[ExecutorContext], Executor] | None = None,
    ) -> None:
        self.context = context
        self.plan = plan
        self.journal_path = journal_path
        self.request_id = request_id
        self.image_executor_factory = image_executor_factory or OpenAIImageExecutor

    def _event(self, event: str, **fields: Any) -> None:
        run_controller.append_event(
            self.journal_path,
            event,
            request_id=self.request_id,
            **fields,
        )

    def _image_production(
        self,
        order: RepairWorkOrder,
    ) -> ImageProductionExecutor:
        delegate = ProductionRenderObserverExecutor(
            self.image_executor_factory(self.context),
            batch_id=self.plan.product_id,
            on_output=lambda _artifact: None,
        )
        single = RenderTaskPlan(
            tasks=(order.task,),
            planned=(order.config_id,),
            skipped=(),
        )
        return ImageProductionExecutor(
            self.context,
            task_assembler=lambda _manifest, _index_path: single,
            image_executor_factory=lambda _context: delegate,
        )

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        if request.step != "repair":
            raise ExecutorExecutionError("qc-repair 仅接受 repair")
        outputs = self.context.manifest.get("outputs")
        if not isinstance(outputs, Mapping):
            raise ExecutorExecutionError("manifest.outputs 缺失")
        renders_dir = _first_path(outputs.get("renders"), "outputs.renders")
        if not self.plan.work_orders:
            raise ExecutorExecutionError("返修计划为空")
        repaired_dir = self.plan.work_orders[0].task.output_path.parent
        if any(order.task.output_path.parent != repaired_dir for order in self.plan.work_orders):
            raise ExecutorExecutionError("返修计划包含多个输出目录")
        repaired_dir.mkdir(parents=True, exist_ok=True)
        lock_path = repaired_dir / ".repair.lock"
        try:
            lock_handle = lock_path.open("x", encoding="utf-8")
        except FileExistsError:
            raise ExecutorExecutionError("返修任务正在运行或上次未正常收尾，已拒绝重复启动") from None

        succeeded: list[str] = []
        failed: list[str] = []
        skipped: list[str] = []
        result_outputs: list[Path] = []
        model = ""
        try:
            with lock_handle:
                lock_handle.write(self.request_id)
            before_renders = _render_snapshot(renders_dir)
            for order in self.plan.work_orders:
                self._event(
                    "repair_item_started",
                    step="repair",
                    config_id=order.config_id,
                    target_count=len(order.actionable_targets),
                    review_count=len(order.review_targets),
                )
                if order.task.output_path.is_file():
                    if _valid_existing(order):
                        skipped.append(order.config_id)
                        self._event(
                            "repair_item_skipped_existing",
                            step="repair",
                            config_id=order.config_id,
                            sha256=_sha256(order.task.output_path),
                        )
                    else:
                        failed.append(order.config_id)
                        self._event(
                            "repair_item_failed",
                            step="repair",
                            config_id=order.config_id,
                            detail="已有返修图格式无效，未覆盖且未自动重试",
                        )
                    continue
                try:
                    result = self._image_production(order).execute(
                        ExecutionRequest(step="renders")
                    )
                    if not order.task.output_path.is_file() or not _valid_existing(order):
                        raise ExecutorExecutionError("返修图未形成合规文件")
                except Exception:
                    failed.append(order.config_id)
                    self._event(
                        "repair_item_failed",
                        step="repair",
                        config_id=order.config_id,
                        detail="图片返修执行失败，未自动重试",
                    )
                    continue
                artifact = artifact_from_path(self.plan.product_id, order.task.output_path)
                succeeded.append(order.config_id)
                result_outputs.append(order.task.output_path)
                model = result.model or model
                self._event(
                    "repair_item_succeeded",
                    step="repair",
                    config_id=order.config_id,
                    sha256=artifact.sha256,
                    byte_count=artifact.byte_count,
                    width=artifact.width,
                    height=artifact.height,
                )
            after_renders = _render_snapshot(renders_dir)
            if before_renders != after_renders:
                self._event(
                    "renders_protection_failed",
                    step="repair",
                    detail="正式 renders 在返修期间发生变化，已停止",
                )
                raise ExecutorExecutionError("正式 renders 在返修期间发生变化，已停止")
            self._event(
                "repair_completed",
                step="repair",
                succeeded_count=len(succeeded),
                failed_count=len(failed),
                skipped_count=len(skipped),
                failed_config_ids=failed,
            )
            if failed:
                status = "completed_with_failures"
                self._event(
                    "step_completed_with_failures",
                    step="repair",
                    succeeded_count=len(succeeded),
                    failed_count=len(failed),
                    skipped_count=len(skipped),
                    failed_config_ids=failed,
                )
            else:
                status = "succeeded"
                self._event(
                    "step_succeeded",
                    step="repair",
                    detail=f"返修处理完成：成功 {len(succeeded)}，跳过 {len(skipped)}",
                )
            return ExecutionResult(
                detail=(
                    f"返修处理完成：成功 {len(succeeded)}，失败 {len(failed)}，"
                    f"跳过 {len(skipped)}"
                ),
                outputs=tuple(result_outputs),
                provider=self.name,
                model=model,
                metadata={
                    "status": status,
                    "succeeded": tuple(succeeded),
                    "failed": tuple(failed),
                    "skipped": tuple(skipped),
                },
            )
        finally:
            try:
                lock_path.unlink(missing_ok=True)
            except OSError:
                pass
