"""M2-b daemon: consume real workflow commands without projecting an engine room."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Mapping

import executor_factory
import ic_client
import run_controller
import state_reader
from executor_contract import Executor, ExecutorContext, ExecutorExecutionError
from image_production_executor import ImageProductionExecutor
from openai_image_executor import OpenAIImageExecutor
from workflow_production_controller import (
    PRODUCTION_TOTAL_IMAGES,
    ProductionGateError,
    apply_production_requested_outputs,
    human_step_message,
    next_gated_command,
    resolve_gated_step,
    resolve_production_selection,
)
from workflow_production_projection import (
    WorkflowProductionArtifact,
    artifact_from_path,
    build_output_projection_ops,
    output_node_id,
)
from workflow_production_render_observer import ProductionRenderObserverExecutor


COMMAND_MAX_AGE_MS = 8_000
UPSTREAM_STEPS = {"identity", "style_master", "angle_inventory", "main_vc", "detail_vc", "final_prompts"}
M2B_STEPS = UPSTREAM_STEPS | {"integrity", "renders"}


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(parent.resolve(strict=False))
        return True
    except ValueError:
        return False


def _path_values(value: Any) -> tuple[Path, ...]:
    values = value if isinstance(value, list) else [value]
    return tuple(Path(item) for item in values if isinstance(item, str) and item)


def discover_accepted_artifacts(manifest: Mapping[str, Any]) -> tuple[WorkflowProductionArtifact, ...]:
    batch_id = str(manifest.get("product_id") or "")
    workspace_value = (manifest.get("workspace") or {}).get("root") if isinstance(manifest.get("workspace"), Mapping) else None
    if not batch_id or not isinstance(workspace_value, str) or not workspace_value:
        return ()
    workspace = Path(workspace_value)
    outputs = manifest.get("outputs") if isinstance(manifest.get("outputs"), Mapping) else {}
    found: dict[str, WorkflowProductionArtifact] = {}
    for key in ("renders", "repaired"):
        for root in _path_values(outputs.get(key)):
            if not _inside(root, workspace) or not root.is_dir():
                continue
            for path in sorted(root.glob("*.png")):
                try:
                    artifact = artifact_from_path(batch_id, path)
                except (OSError, ValueError):
                    continue
                accepted = (
                    artifact.width == artifact.height
                    if artifact.kind == "main"
                    else artifact.width * 4 == artifact.height * 3
                )
                if accepted and artifact.config_id not in found:
                    found[artifact.config_id] = artifact
    return tuple(found[key] for key in sorted(found, key=lambda item: (not item.startswith("main_"), item)))


class WorkflowProductionService:
    def __init__(
        self,
        repository_root: Path,
        *,
        client: Any = ic_client,
        executor_builder: Callable[[str, Mapping[str, Any], Path, Callable[[WorkflowProductionArtifact], None]], Executor] | None = None,
        route_reader: Callable[[Path], dict[str, Any]] = state_reader.read_batch_route,
        integrity_reader: Callable[[dict[str, Any]], dict[str, Any]] = state_reader.integrity_report_status,
        artifact_reader: Callable[[Mapping[str, Any]], tuple[WorkflowProductionArtifact, ...]] = discover_accepted_artifacts,
        clock_ms: Callable[[], int] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        interval: float = 2.0,
        production_base_url: str = "http://127.0.0.1:17373",
        persistence_timeout_ms: int = 12_000,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.repository_root = repository_root.resolve()
        self.client = client
        self.executor_builder = executor_builder or self._build_executor
        self.route_reader = route_reader
        self.integrity_reader = integrity_reader
        self.artifact_reader = artifact_reader
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.sleep = sleep
        self.interval = interval
        self.production_base_url = production_base_url.rstrip("/")
        self.persistence_timeout_ms = max(0, int(persistence_timeout_ms))
        self.environment = environment if environment is not None else os.environ
        self.consumed_content: dict[str, str] = {}
        self.stopping = False

    @staticmethod
    def _production_state(node: Mapping[str, Any]) -> dict[str, Any]:
        metadata = node.get("metadata") if isinstance(node.get("metadata"), Mapping) else {}
        value = metadata.get("workflowProduction") if isinstance(metadata.get("workflowProduction"), Mapping) else {}
        return dict(value)

    def _manifest_path(self, batch_id: str) -> Path:
        if not batch_id or Path(batch_id).name != batch_id or any(char in batch_id for char in ("/", "\\", "\0")):
            raise ProductionGateError("批次号无效，真实制作没有开始。")
        path = self.repository_root / "manifests" / f"{batch_id}.batch_manifest.json"
        if not path.is_file():
            raise ProductionGateError("找不到这张信息卡对应的批次清单。")
        return path

    @staticmethod
    def _load_manifest(path: Path, batch_id: str) -> dict[str, Any]:
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ProductionGateError("批次清单无法读取，真实制作没有开始。") from exc
        if not isinstance(manifest, dict) or manifest.get("product_id") != batch_id:
            raise ProductionGateError("批次清单与信息卡不一致，真实制作没有开始。")
        workspace_value = (manifest.get("workspace") or {}).get("root") if isinstance(manifest.get("workspace"), Mapping) else None
        if not isinstance(workspace_value, str) or not workspace_value:
            raise ProductionGateError("批次工作区信息缺失，真实制作没有开始。")
        workspace = Path(workspace_value)
        try:
            marker = json.loads((workspace / ".canvas_batch").read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ProductionGateError("批次安全标记无效，真实制作没有开始。") from exc
        if not isinstance(marker, dict) or marker.get("type") != "canvas-batch-v1" or marker.get("product_id") != batch_id:
            raise ProductionGateError("批次安全标记与信息卡不一致，真实制作没有开始。")
        return manifest

    @staticmethod
    def _journal_seen(path: Path, request_id: str) -> bool:
        if not path.is_file():
            return False
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict) and entry.get("request_id") == request_id:
                return True
        return False

    @staticmethod
    def _journal_path(manifest_path: Path, batch_id: str) -> Path:
        return run_controller.journal_path(manifest_path, batch_id)

    def _machine_update(
        self,
        node: Mapping[str, Any],
        *,
        status: str,
        content: str,
        step: str | None = None,
        produced_count: int | None = None,
        error_message: str | None = None,
        message: str | None = None,
    ) -> dict[str, Any]:
        state = self._production_state(node)
        state["status"] = status
        state["updatedAt"] = self.clock_ms()
        if step:
            state["step"] = step
            state["message"] = message or human_step_message(step, produced_count=produced_count or 0)
        else:
            state.pop("step", None)
            if message:
                state["message"] = message
        if produced_count is not None:
            state["producedCount"] = produced_count
        state["totalCount"] = PRODUCTION_TOTAL_IMAGES
        if error_message:
            state["errorMessage"] = error_message
        else:
            state.pop("errorMessage", None)
        return {
            "type": "update_node",
            "id": str(node.get("id") or ""),
            "metadata": {"content": content, "workflowProduction": state},
        }

    def _apply_with_reconnect(self, ops: list[dict[str, Any]]) -> None:
        while not self.stopping:
            try:
                self.client.apply_ops(ops)
                return
            except ic_client.CanvasAgentError:
                self.sleep(max(self.interval, 2.0))
        raise ExecutorExecutionError("真实工作流服务已停止")

    def _persisted(self, node_id: str, sha256: str) -> bool:
        state = self.client.call_tool("canvas_get_state")
        for node in state.get("nodes") or []:
            if node.get("id") != node_id:
                continue
            metadata = node.get("metadata") if isinstance(node.get("metadata"), Mapping) else {}
            output = metadata.get("workflowProductionOutput") if isinstance(metadata.get("workflowProductionOutput"), Mapping) else {}
            return bool(metadata.get("storageKey")) and output.get("sha256") == sha256
        return False

    def _wait_for_persistence(self, artifact: WorkflowProductionArtifact) -> None:
        node_id = output_node_id(artifact.batch_id, artifact.config_id)
        deadline = time.monotonic() + self.persistence_timeout_ms / 1000
        while True:
            if self._persisted(node_id, artifact.sha256):
                return
            if time.monotonic() >= deadline or self.stopping:
                raise ExecutorExecutionError("真实图片没有在规定时间内完成浏览器持久化")
            self.sleep(0.1)

    def _project_artifact(
        self,
        machine: Mapping[str, Any],
        artifact: WorkflowProductionArtifact,
        journal: Path,
        request_id: str,
    ) -> None:
        state = self.client.call_tool("canvas_get_state")
        current_machine = next(
            (item for item in state.get("nodes") or [] if item.get("id") == machine.get("id")),
            dict(machine),
        )
        ops, _projected = build_output_projection_ops(
            dict(current_machine),
            list(state.get("nodes") or []),
            artifact,
            self.production_base_url,
        )
        self._apply_with_reconnect(ops)
        self._wait_for_persistence(artifact)
        run_controller.append_event(
            journal,
            "image_persisted",
            request_id=request_id,
            config_id=artifact.config_id,
            sha256=artifact.sha256,
            byte_count=artifact.byte_count,
            width=artifact.width,
            height=artifact.height,
        )

    def _sync_existing(
        self,
        machine: Mapping[str, Any],
        manifest: Mapping[str, Any],
        journal: Path,
        request_id: str,
    ) -> tuple[WorkflowProductionArtifact, ...]:
        artifacts = self.artifact_reader(manifest)
        for artifact in artifacts:
            if not self._persisted(output_node_id(artifact.batch_id, artifact.config_id), artifact.sha256):
                self._project_artifact(machine, artifact, journal, request_id)
        return artifacts

    def _build_executor(
        self,
        step: str,
        manifest: Mapping[str, Any],
        manifest_path: Path,
        on_output: Callable[[WorkflowProductionArtifact], None],
    ) -> Executor:
        if step in UPSTREAM_STEPS:
            return executor_factory.build_executor("codex-dev", manifest, manifest_path)
        context = ExecutorContext(manifest=manifest, manifest_path=manifest_path, environment=self.environment)
        if step == "integrity":
            return ImageProductionExecutor(context)
        if step == "renders":
            workspace = Path(str((manifest.get("workspace") or {}).get("root") or ""))

            def image_factory(inner_context: ExecutorContext) -> Executor:
                return ProductionRenderObserverExecutor(
                    OpenAIImageExecutor(inner_context),
                    batch_id=str(manifest.get("product_id") or ""),
                    audit_root=workspace / "artifacts" / "audit",
                    on_output=on_output,
                )

            return ImageProductionExecutor(context, image_executor_factory=image_factory)
        raise ProductionGateError("M2-b 不允许执行这个步骤。")

    @staticmethod
    def _safe_failure(exc: BaseException) -> str:
        if isinstance(exc, ProductionGateError):
            return str(exc)
        if isinstance(exc, ExecutorExecutionError) and "2:3" in str(exc):
            return "详情图返回 2:3，原图已保留。机器已停下，等待人工尺寸处理批准。"
        if isinstance(exc, ExecutorExecutionError) and "OPENAI_API_KEY" in str(exc):
            return "前面的成果已保留。本机还没有准备图片服务凭据，当前未出图、未产生新的图片费用。"
        return "这一步没做好，机器已停下。已经完成的成果都保留了。"

    def _reject(self, node: Mapping[str, Any], request_id: str, message: str) -> None:
        self._apply_with_reconnect(
            [
                self._machine_update(
                    node,
                    status="failed",
                    content=f"# request-id: {request_id}\n# failed",
                    error_message=message,
                )
            ]
        )

    def _process(self, machine: Mapping[str, Any], canvas_state: Mapping[str, Any]) -> None:
        production = self._production_state(machine)
        request_id = str(production.get("requestId") or "")
        requested_at = production.get("requestedAt")
        content = str((machine.get("metadata") or {}).get("content") or "")
        if not request_id:
            self._reject(machine, "missing-request", "这次真实制作请求格式不正确，没有执行。")
            return
        if not isinstance(requested_at, (int, float)) or self.clock_ms() - int(requested_at) >= COMMAND_MAX_AGE_MS:
            self._reject(machine, request_id, "本机工作台没有及时接单，请重新开始一次。")
            return
        selection = resolve_production_selection(str(machine.get("id") or ""), canvas_state)
        if production.get("batchId") != selection.batch_id:
            raise ProductionGateError("机器请求的批次号与信息卡不一致。")
        manifest_path = self._manifest_path(selection.batch_id)
        manifest = self._load_manifest(manifest_path, selection.batch_id)
        journal = self._journal_path(manifest_path, selection.batch_id)
        if self._journal_seen(journal, request_id):
            self._reject(machine, request_id, "这次请求已经处理，不会重复执行。")
            return

        pre_route = self.route_reader(manifest_path)
        style_count = (((pre_route.get("inputs") or {}).get("style_reference_images") or {}).get("file_count"))
        if not isinstance(style_count, int) or style_count <= 0:
            raise ProductionGateError("还缺风格参考图。请先在信息卡补登，当前没有调用模型。")

        # The approved edit happens only after the fee card has queued this request.
        edit_result = apply_production_requested_outputs(manifest_path)
        manifest = self._load_manifest(manifest_path, selection.batch_id)
        run_controller.append_event(
            journal,
            "command_received",
            request_id=request_id,
            command="workflow-production",
            machine_id=selection.machine_id,
        )
        if edit_result.get("changed"):
            run_controller.append_event(
                journal,
                "requested_outputs_declared",
                request_id=request_id,
                requested_outputs=list(edit_result["requested_outputs"]),
            )

        accepted_content = f"# request-id: {request_id}\n# accepted"
        existing = self._sync_existing(machine, manifest, journal, request_id)
        produced_count = len(existing)
        command_text = content

        while not self.stopping:
            route = self.route_reader(manifest_path)
            integrity = self.integrity_reader(route)
            if produced_count >= PRODUCTION_TOTAL_IMAGES and route.get("current_stage") == "needs_qc_reports":
                self._apply_with_reconnect(
                    [
                        self._machine_update(
                            machine,
                            status="completed",
                            content=f"# request-id: {request_id}\n# completed",
                            produced_count=PRODUCTION_TOTAL_IMAGES,
                        )
                    ]
                )
                run_controller.append_event(journal, "production_completed", request_id=request_id, produced_count=PRODUCTION_TOTAL_IMAGES)
                return
            step = resolve_gated_step(command_text, route, integrity)
            if step not in M2B_STEPS:
                raise ProductionGateError("M2-b 已停在待质检，不会越界执行 QC。")
            # This is a deny-only phase boundary, not an execution route.  The
            # existing run controller has already parsed and resolved the next
            # step above; when the image gate is closed we stop before gate 3,
            # so no integrity/render executor is called or recorded as started.
            if step in {"integrity", "renders"} and self.environment.get("RENDER_ALLOW_REAL_EXECUTION") != "1":
                self._apply_with_reconnect(
                    [
                        self._machine_update(
                            machine,
                            status="paused",
                            content=f"# request-id: {request_id}\n# gate-paused",
                            step=step,
                            produced_count=produced_count,
                            message="上游准备完成，已停在出图前。等待批准下一闸门。",
                        )
                    ]
                )
                run_controller.append_event(
                    journal,
                    "production_paused",
                    request_id=request_id,
                    produced_count=produced_count,
                    reason="awaiting_render_gate",
                )
                return
            run_controller.append_event(journal, "step_started", request_id=request_id, step=step)
            self._apply_with_reconnect(
                [
                    self._machine_update(
                        machine,
                        status="running",
                        content=accepted_content,
                        step=step,
                        produced_count=produced_count,
                    )
                ]
            )

            def on_output(artifact: WorkflowProductionArtifact) -> None:
                self._project_artifact(machine, artifact, journal, request_id)
                current = len(self.artifact_reader(manifest))
                self._apply_with_reconnect(
                    [
                        self._machine_update(
                            machine,
                            status="running",
                            content=accepted_content,
                            step="renders",
                            produced_count=current,
                        )
                    ]
                )

            executor = self.executor_builder(step, manifest, manifest_path, on_output)
            result = run_controller.execute_step(executor, step)
            run_controller.append_event(journal, "step_succeeded", request_id=request_id, step=step, detail=result.detail[:160])
            if step == "renders":
                produced_count = len(self.artifact_reader(manifest))
                route = self.route_reader(manifest_path)
                if produced_count >= PRODUCTION_TOTAL_IMAGES and route.get("current_stage") == "needs_qc_reports":
                    self._apply_with_reconnect(
                        [
                            self._machine_update(
                                machine,
                                status="completed",
                                content=f"# request-id: {request_id}\n# completed",
                                produced_count=PRODUCTION_TOTAL_IMAGES,
                            )
                        ]
                    )
                    run_controller.append_event(journal, "production_completed", request_id=request_id, produced_count=PRODUCTION_TOTAL_IMAGES)
                else:
                    self._apply_with_reconnect(
                        [
                            self._machine_update(
                                machine,
                                status="paused",
                                content=f"# request-id: {request_id}\n# paused",
                                produced_count=produced_count,
                            )
                        ]
                    )
                    run_controller.append_event(journal, "production_paused", request_id=request_id, produced_count=produced_count)
                return
            produced_count = len(self.artifact_reader(manifest))
            route = self.route_reader(manifest_path)
            command_text = next_gated_command(route, accepted_render_count=produced_count) or ""
            if not command_text:
                continue

    def poll_once(self) -> None:
        state = self.client.call_tool("canvas_get_state")
        for node in list(state.get("nodes") or []):
            if node.get("type") != "workflow":
                continue
            production = self._production_state(node)
            if production.get("status") != "queued":
                continue
            content = str((node.get("metadata") or {}).get("content") or "")
            node_id = str(node.get("id") or "")
            if not node_id or self.consumed_content.get(node_id) == content:
                continue
            self.consumed_content[node_id] = content
            request_id = str(production.get("requestId") or "missing-request")
            try:
                self._process(node, state)
            except (ProductionGateError, run_controller.RunExecutionError, ExecutorExecutionError) as exc:
                batch_id = str(production.get("batchId") or "")
                try:
                    manifest_path = self._manifest_path(batch_id)
                    run_controller.append_event(
                        self._journal_path(manifest_path, batch_id),
                        "step_failed",
                        request_id=request_id,
                        detail="执行已停止，未自动重试",
                    )
                except ProductionGateError:
                    pass
                self._reject(node, request_id, self._safe_failure(exc))

    def serve_forever(self) -> None:
        while not self.stopping:
            try:
                self.poll_once()
            except ic_client.CanvasAgentError:
                pass
            self.sleep(self.interval)
