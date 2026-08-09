"""M2-c daemon: consume real workflow commands without projecting an engine room."""

from __future__ import annotations

import copy
import json
import os
import queue
import re
import threading
import time
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Mapping

from category_recipes import CategoryRecipeError, load_manifest_category
from codex_dev_qc import qc_chunk_count
from codex_dev_downstream import manifest_config_ids
from batch_recycle_lock import BatchOperationBusy, existing_batch_operation
from batch_recycle_state import (
    BUSY_MESSAGE,
    RECYCLED_MESSAGE,
    BatchLifecycleReadError,
    read_batch_lifecycle,
)
from batch_type_gate import set_batch_blocked_message
import executor_factory
from failure_text_safety import (
    _UNSAFE_FAILURE_DETAIL_PATTERN,
    is_disclosable,
    is_sensitive_identifier,
)
import ic_client
import run_controller
import state_reader
from executor_contract import Executor, ExecutorContext, ExecutorExecutionError, ExecutionResult
from image_production_executor import ImageProductionExecutor
from openai_image_executor import OpenAIImageExecutor
from workflow_production_controller import (
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
    build_render_source_backfill_op,
    build_output_projection_ops,
    output_node_id,
)
from workflow_production_render_observer import ProductionRenderObserverExecutor
from white_bg_recovery import sanitize_filenames


COMMAND_MAX_AGE_MS = 8_000
UPSTREAM_STEPS = {"identity", "style_master", "angle_inventory", "main_vc", "detail_vc", "final_prompts"}
STEP_AUTO_RETRY_STEPS = frozenset(UPSTREAM_STEPS)
DEFAULT_STEP_AUTO_RETRY_LIMIT = 2
MAX_STEP_AUTO_RETRY_LIMIT = 2
M2C_STEPS = UPSTREAM_STEPS | {"integrity", "renders", "qc"}
_M2C_BOUNDARY_MESSAGE = "M2-c 已停在质检完成，返修与交付属后续里程碑。"
_REAL_EXECUTION_DISABLED_CODE = "real_execution_disabled"
_REAL_EXECUTION_DISABLED_WORKBENCH_MESSAGE = (
    "本机真实执行开关未开启，本次没有调用模型、没有产生费用。"
    "请先关闭工作台窗口，按闸门流程用带开关的命令重新启动工作台，再回到画布重新开始。"
)
_REAL_EXECUTION_DISABLED_EVENT_DETAIL = "真实执行开关未开启，执行已停止，未自动重试"
_INTEGRITY_FAILURE_CODE = "integrity_check_failed"
_RENDER_FAILURE_CODES = frozenset(
    {
        "render_http_error",
        "render_response_invalid",
        "render_timeout",
        "render_network_error",
        "render_image_download_failed",
        "render_input_missing",
        "render_inputs_unavailable",
        "render_pipeline_error",
        "render_canvas_unavailable",
    }
)
_SAFE_RENDER_FAILURE_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_RENDER_FAILURE_INTEGER_RANGES = {
    "http_status": (100, 599),
    "successful_count": (0, 9_999),
    "planned_count": (0, 9_999),
    "skipped_count": (0, 9_999),
    "timeout_seconds": (1, 9_999),
}
_RENDER_FAILURE_TOKEN_FIELDS = (
    "provider_error_type",
    "provider_error_code",
    "provider_request_id",
)
_RENDER_FAILURE_SHAPE_FIELDS = (
    "response_top_keys",
    "response_data0_keys",
)
_IMAGE_SERVICE_FAILURE_SOURCE = "image_service"
_IMAGE_SERVICE_FAILURE_CODES = frozenset(
    {
        "render_http_error",
        "render_response_invalid",
        "render_timeout",
        "render_network_error",
        "render_image_download_failed",
    }
)
_PERSISTENCE_TIMEOUT_DETAIL = "真实图片没有在规定时间内完成浏览器持久化"
_QC_HEARTBEAT_HTTP_TIMEOUT_SECONDS = 1.0
_QC_HEARTBEAT_JOIN_TIMEOUT_SECONDS = 12.0
_QC_HEARTBEAT_STOP = object()
_TURN_PROGRESS_HEARTBEAT_STEPS = {"main_vc", "detail_vc", "final_prompts"}
_STATUS_PROJECTION_HTTP_TIMEOUT_SECONDS = 1.0
BATCH_CLOSED_MESSAGE = "本批次已关账，不能再发起制作、质检、返修或上桌操作。"


class BatchClosedGateError(ProductionGateError):
    pass


class BatchRecycledGateError(ProductionGateError):
    pass


class BatchOperationBusyGateError(ProductionGateError):
    pass


class BatchLifecycleGateError(ProductionGateError):
    pass


def _guard_batch_side_effect(*, quiet: bool = False):
    """Hold the shared batch lock before any workspace, journal or Canvas write."""

    def decorate(method):
        @wraps(method)
        def guarded(self, machine, canvas_state, *args, **kwargs):
            try:
                selection = resolve_production_selection(
                    str(machine.get("id") or ""),
                    canvas_state,
                )
                manifest_path = self._manifest_path(selection.batch_id)
                journal = self._journal_path(manifest_path, selection.batch_id)
                with existing_batch_operation(
                    selection.batch_id,
                    lock_root=self.batch_lock_root,
                ):
                    lifecycle = read_batch_lifecycle(journal)
                    if lifecycle.recycled:
                        if quiet:
                            return None
                        raise BatchRecycledGateError(RECYCLED_MESSAGE)
                    return method(self, machine, canvas_state, *args, **kwargs)
            except BatchOperationBusy:
                if quiet:
                    return None
                raise BatchOperationBusyGateError(BUSY_MESSAGE) from None
            except BatchLifecycleReadError:
                if quiet:
                    return None
                raise BatchLifecycleGateError(
                    "批次账本暂时无法读取，真实制作没有开始。"
                ) from None
            except ProductionGateError:
                if quiet:
                    return None
                raise

        return guarded

    return decorate


_CONTROLLED_CODEX_FAILURE_LABELS = frozenset(
    {"主图变量配置", "详情图变量配置", "主图最终提示词", "详情图最终提示词"}
)
_CONTROLLED_CODEX_SIMPLE_REASONS = frozenset(
    {
        "违反用户确认场景边界",
        "角度绑定异常",
        "使用了缺失的 D 槽位",
        "手持规则调用异常",
    }
)
_CONTROLLED_CODEX_CLAIM_CATEGORIES = frozenset(
    {"未确认参数", "未确认商品事实"}
)
_CONTROLLED_CODEX_CLAIM_PATH_PATTERN = re.compile(
    r"(?:\$|notes|common_constraints(?:/(?:未知字段\d+|[\u4e00-\u9fffA-Za-z0-9_]+))?|"
    r"configs/\d+/(?:notes|per_image_overrides/(?:未知字段\d+|[\u4e00-\u9fffA-Za-z0-9_]+))|"
    r"prompts/\d+/(?:final_prompt|negative_prompt))"
)
def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(parent.resolve(strict=False))
        return True
    except ValueError:
        return False


def _path_values(value: Any) -> tuple[Path, ...]:
    values = value if isinstance(value, list) else [value]
    return tuple(Path(item) for item in values if isinstance(item, str) and item)


def discover_source_artifacts(
    manifest: Mapping[str, Any],
    source: str,
    repository_root: Path | None = None,
) -> tuple[WorkflowProductionArtifact, ...]:
    if source not in {"renders", "repaired"}:
        return ()
    batch_id = str(manifest.get("product_id") or "")
    workspace_value = (manifest.get("workspace") or {}).get("root") if isinstance(manifest.get("workspace"), Mapping) else None
    if not batch_id or not isinstance(workspace_value, str) or not workspace_value:
        return ()
    try:
        expected_ids = frozenset(
            manifest_config_ids(
                manifest,
                repository_root or Path(__file__).resolve().parent.parent,
            )
        )
    except ExecutorExecutionError:
        return ()
    workspace = Path(workspace_value)
    outputs = manifest.get("outputs") if isinstance(manifest.get("outputs"), Mapping) else {}
    found: dict[str, WorkflowProductionArtifact] = {}
    for root in _path_values(outputs.get(source)):
        if not _inside(root, workspace) or not root.is_dir():
            continue
        for path in sorted(root.glob("*.png")):
            if path.stem not in expected_ids:
                continue
            try:
                artifact = artifact_from_path(batch_id, path, source=source)
            except (OSError, ValueError):
                continue
            if artifact.config_id not in found:
                found[artifact.config_id] = artifact
    return tuple(found[key] for key in sorted(found, key=lambda item: (not item.startswith("main_"), item)))


def discover_accepted_artifacts(
    manifest: Mapping[str, Any],
    repository_root: Path | None = None,
) -> tuple[WorkflowProductionArtifact, ...]:
    found: dict[str, WorkflowProductionArtifact] = {}
    for source in ("renders", "repaired"):
        for artifact in discover_source_artifacts(manifest, source, repository_root):
            found.setdefault(artifact.config_id, artifact)
    return tuple(found[key] for key in sorted(found, key=lambda item: (not item.startswith("main_"), item)))


class _QcHeartbeatWorker:
    """Asynchronously send canvas heartbeat ops for QC and turn progress."""

    def __init__(
        self,
        request_id: str,
        sender: Callable[[list[dict[str, Any]]], None],
    ) -> None:
        self.request_id = request_id
        self.sender = sender
        self._queue: queue.Queue[object] = queue.Queue()
        self._state_lock = threading.Lock()
        self._accepting = True
        self._cancel_pending = False
        self._thread = threading.Thread(
            target=self._run,
            name=f"qc-heartbeat-{request_id[:12] or 'unknown'}",
            daemon=True,
        )
        self._thread.start()

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    def submit(self, ops: list[dict[str, Any]]) -> None:
        with self._state_lock:
            if not self._accepting:
                return
            self._queue.put(ops)

    def close(self, *, drain: bool) -> None:
        with self._state_lock:
            if not self._accepting:
                return
            self._accepting = False
            self._cancel_pending = not drain
            self._queue.put(_QC_HEARTBEAT_STOP)
        self._thread.join(_QC_HEARTBEAT_JOIN_TIMEOUT_SECONDS)

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is _QC_HEARTBEAT_STOP:
                return
            with self._state_lock:
                cancel_pending = self._cancel_pending
            if cancel_pending:
                continue
            try:
                self.sender(item)
            except Exception:
                pass


class _StatusProjectionOutbox:
    """Coalesce idempotent node updates and retry them without blocking work."""

    def __init__(
        self,
        sender: Callable[[list[dict[str, Any]]], None],
        *,
        retry_seconds: float,
        should_stop: Callable[[], bool],
    ) -> None:
        self.sender = sender
        self.retry_seconds = max(0.05, float(retry_seconds))
        self.should_stop = should_stop
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._pending: dict[str, tuple[int, dict[str, Any]]] = {}
        self._version = 0
        self._thread: threading.Thread | None = None

    def submit(self, ops: list[dict[str, Any]]) -> None:
        with self._lock:
            for op in ops:
                node_id = str(op.get("id") or "")
                if op.get("type") != "update_node" or not node_id:
                    raise ValueError(
                        "status projection outbox accepts update_node ops only"
                    )
                self._version += 1
                self._pending[node_id] = (self._version, dict(op))
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._run,
                    name="workflow-status-projection",
                    daemon=True,
                )
                self._thread.start()
        self._wake.set()

    def _run(self) -> None:
        while not self.should_stop():
            with self._lock:
                if not self._pending:
                    self._thread = None
                    return
                snapshot = dict(self._pending)
            self._wake.clear()
            try:
                self.sender([item[1] for item in snapshot.values()])
            except ic_client.CanvasAgentError:
                self._wake.wait(self.retry_seconds)
                continue
            with self._lock:
                for node_id, (version, _op) in snapshot.items():
                    current = self._pending.get(node_id)
                    if current is not None and current[0] == version:
                        self._pending.pop(node_id, None)
        with self._lock:
            self._thread = None


class WorkflowProductionService:
    def __init__(
        self,
        repository_root: Path,
        *,
        client: Any = ic_client,
        executor_builder: Callable[[str, Mapping[str, Any], Path, Callable[[WorkflowProductionArtifact], None]], Executor] | None = None,
        route_reader: Callable[[Path], dict[str, Any]] = state_reader.read_batch_route,
        integrity_reader: Callable[[dict[str, Any]], dict[str, Any]] = state_reader.integrity_report_status,
        artifact_reader: Callable[[Mapping[str, Any]], tuple[WorkflowProductionArtifact, ...]] | None = None,
        render_artifact_reader: Callable[[Mapping[str, Any]], tuple[WorkflowProductionArtifact, ...]] | None = None,
        repaired_artifact_reader: Callable[[Mapping[str, Any]], tuple[WorkflowProductionArtifact, ...]] | None = None,
        clock_ms: Callable[[], int] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        interval: float = 2.0,
        production_base_url: str = "http://127.0.0.1:17373",
        persistence_timeout_ms: int = 12_000,
        environment: Mapping[str, str] | None = None,
        diagnostic_recorder: Callable[[str, str], None] | None = None,
        batch_lock_root: Path | None = None,
        step_auto_retry_limit: int = DEFAULT_STEP_AUTO_RETRY_LIMIT,
    ) -> None:
        if (
            type(step_auto_retry_limit) is not int
            or not 0 <= step_auto_retry_limit <= MAX_STEP_AUTO_RETRY_LIMIT
        ):
            raise ValueError(
                "step_auto_retry_limit must be an integer between 0 and "
                f"{MAX_STEP_AUTO_RETRY_LIMIT}"
            )
        self.repository_root = repository_root.resolve()
        self.client = client
        self._uses_default_executor_builder = executor_builder is None
        self.executor_builder = executor_builder or self._build_executor
        self.route_reader = route_reader
        self.integrity_reader = integrity_reader
        self.artifact_reader = artifact_reader or (
            lambda manifest: discover_accepted_artifacts(
                manifest,
                self.repository_root,
            )
        )
        self.render_artifact_reader = render_artifact_reader or (
            lambda manifest: discover_source_artifacts(
                manifest,
                "renders",
                self.repository_root,
            )
        )
        self.repaired_artifact_reader = repaired_artifact_reader or (
            lambda manifest: discover_source_artifacts(
                manifest,
                "repaired",
                self.repository_root,
            )
        )
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.sleep = sleep
        self.interval = interval
        self.production_base_url = production_base_url.rstrip("/")
        self.persistence_timeout_ms = max(0, int(persistence_timeout_ms))
        self.environment = environment if environment is not None else os.environ
        self.diagnostic_recorder = diagnostic_recorder
        self.batch_lock_root = batch_lock_root
        self.step_auto_retry_limit = step_auto_retry_limit
        self.consumed_content: dict[str, str] = {}
        self.stopping = False
        self._qc_heartbeat_workers: set[_QcHeartbeatWorker] = set()
        self._status_projection_outbox = _StatusProjectionOutbox(
            self._send_status_projection_once,
            retry_seconds=self.interval,
            should_stop=lambda: self.stopping,
        )

    @staticmethod
    def _production_state(node: Mapping[str, Any]) -> dict[str, Any]:
        metadata = node.get("metadata") if isinstance(node.get("metadata"), Mapping) else {}
        value = metadata.get("workflowProduction") if isinstance(metadata.get("workflowProduction"), Mapping) else {}
        return dict(value)

    @staticmethod
    def _repaired_projection_state(node: Mapping[str, Any]) -> dict[str, Any]:
        metadata = node.get("metadata") if isinstance(node.get("metadata"), Mapping) else {}
        value = (
            metadata.get("workflowRepairedProjection")
            if isinstance(metadata.get("workflowRepairedProjection"), Mapping)
            else {}
        )
        return dict(value)

    def _manifest_path(self, batch_id: str) -> Path:
        if not batch_id or Path(batch_id).name != batch_id or any(char in batch_id for char in ("/", "\\", "\0")):
            raise ProductionGateError("批次号无效，真实制作没有开始。")
        path = self.repository_root / "manifests" / f"{batch_id}.batch_manifest.json"
        if not path.is_file():
            raise ProductionGateError("找不到这张信息卡对应的批次清单。")
        return path

    def _load_manifest(self, path: Path, batch_id: str) -> dict[str, Any]:
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ProductionGateError("批次清单无法读取，真实制作没有开始。") from exc
        if not isinstance(manifest, dict) or manifest.get("product_id") != batch_id:
            raise ProductionGateError("批次清单与信息卡不一致，真实制作没有开始。")
        try:
            load_manifest_category(self.repository_root, manifest)
            self._expected_ids(manifest)
        except CategoryRecipeError as exc:
            raise ProductionGateError(
                f"产品品类配方不可用，真实制作没有开始：{exc}"
            ) from None
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

    def _expected_ids(self, manifest: Mapping[str, Any]) -> tuple[str, ...]:
        try:
            return manifest_config_ids(manifest, self.repository_root)
        except ExecutorExecutionError:
            raise ProductionGateError(
                "批次图片张数无效，真实制作没有开始。"
            ) from None

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
    def _journal_has_event(
        path: Path,
        event_name: str,
        config_id: str | None = None,
    ) -> bool:
        if not path.is_file():
            return False
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (
                isinstance(entry, dict)
                and entry.get("event") == event_name
                and (config_id is None or entry.get("config_id") == config_id)
            ):
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
        total_count: int | None = None,
        expected_ids: tuple[str, ...] | None = None,
        error_message: str | None = None,
        message: str | None = None,
        recovery: Mapping[str, Any] | None = None,
        failure_source: str | None = None,
    ) -> dict[str, Any]:
        state = self._production_state(node)
        state["status"] = status
        state["updatedAt"] = self.clock_ms()
        if step:
            state["step"] = step
            state["message"] = message or human_step_message(
                step,
                produced_count=produced_count or 0,
                total_count=total_count,
            )
        else:
            state.pop("step", None)
            if message:
                state["message"] = message
        if produced_count is not None:
            state["producedCount"] = produced_count
        if (total_count is None) != (expected_ids is None):
            raise ProductionGateError("批次图片总数与编号清单必须同时写入。")
        if total_count is not None and expected_ids is not None:
            if total_count != len(expected_ids):
                raise ProductionGateError("批次图片总数与编号清单不一致。")
            state["totalCount"] = total_count
            state["expectedConfigIds"] = list(expected_ids)
        elif status == "idle":
            state.pop("totalCount", None)
            state.pop("expectedConfigIds", None)
        if error_message:
            state["errorMessage"] = error_message
        else:
            state.pop("errorMessage", None)
        if recovery is not None:
            state["recovery"] = dict(recovery)
        else:
            state.pop("recovery", None)
        if failure_source is not None:
            if failure_source != _IMAGE_SERVICE_FAILURE_SOURCE:
                raise ProductionGateError("生产失败来源无效。")
            state["failureSource"] = failure_source
        else:
            state.pop("failureSource", None)
        return {
            "type": "update_node",
            "id": str(node.get("id") or ""),
            "metadata": {"content": content, "workflowProduction": state},
        }

    def _call_with_reconnect(
        self,
        operation: Callable[..., Any],
        *args: Any,
    ) -> Any:
        while not self.stopping:
            try:
                return operation(*args)
            except ic_client.CanvasAgentError:
                self.sleep(max(self.interval, 2.0))
        raise ExecutorExecutionError("真实工作流服务已停止")

    def _apply_with_reconnect(self, ops: list[dict[str, Any]]) -> None:
        self._call_with_reconnect(self.client.apply_ops, ops)

    def _send_status_projection_once(self, ops: list[dict[str, Any]]) -> None:
        if self.client is ic_client:
            ic_client.call_tool(
                "canvas_apply_ops",
                {"ops": ops},
                timeout=_STATUS_PROJECTION_HTTP_TIMEOUT_SECONDS,
            )
            return
        self.client.apply_ops(ops)

    def _apply_status_projection(self, ops: list[dict[str, Any]]) -> None:
        if self.stopping:
            raise ExecutorExecutionError("真实工作流服务已停止")
        try:
            self._send_status_projection_once(ops)
        except ic_client.CanvasAgentError:
            self._status_projection_outbox.submit(ops)

    def _send_qc_heartbeat_once(self, ops: list[dict[str, Any]]) -> None:
        if self.client is ic_client:
            ic_client.call_tool(
                "canvas_apply_ops",
                {"ops": ops},
                timeout=_QC_HEARTBEAT_HTTP_TIMEOUT_SECONDS,
            )
            return
        self.client.apply_ops(ops)

    def _start_qc_heartbeat_worker(self, request_id: str) -> _QcHeartbeatWorker:
        worker = _QcHeartbeatWorker(request_id, self._send_qc_heartbeat_once)
        self._qc_heartbeat_workers.add(worker)
        return worker

    def _close_qc_heartbeat_worker(
        self,
        worker: _QcHeartbeatWorker,
        *,
        drain: bool,
    ) -> None:
        try:
            worker.close(drain=drain)
        finally:
            if not worker.alive:
                self._qc_heartbeat_workers.discard(worker)

    def _qc_progress_callback(
        self,
        worker: _QcHeartbeatWorker,
        machine: Mapping[str, Any],
        accepted_content: str,
        *,
        total_count: int,
        expected_ids: tuple[str, ...],
        chunk_count: int,
    ) -> Callable[[int, int], None]:
        def report(completed: int, total: int) -> None:
            if total != chunk_count or completed not in range(1, total + 1):
                return
            if completed < total:
                message = (
                    f"正在逐张质检 {total_count} 张成图…"
                    f"（第 {completed}/{total} 组完成）"
                )
            else:
                message = (
                    f"{total_count} 张成图质检汇总已完成，正在生成 QC 报告…"
                    f"（第 {total}/{total} 组完成）"
                )
            worker.submit(
                [
                    self._machine_update(
                        machine,
                        status="running",
                        content=accepted_content,
                        step="qc",
                        produced_count=total_count,
                        total_count=total_count,
                        expected_ids=expected_ids,
                        message=message,
                    )
                ]
            )

        return report

    def _turn_progress_callback(
        self,
        worker: _QcHeartbeatWorker,
        machine: Mapping[str, Any],
        accepted_content: str,
        *,
        step: str,
        produced_count: int,
        total_count: int,
        expected_ids: tuple[str, ...],
    ) -> Callable[[], None]:
        def report() -> None:
            worker.submit(
                [
                    self._machine_update(
                        machine,
                        status="running",
                        content=accepted_content,
                        step=step,
                        produced_count=produced_count,
                        total_count=total_count,
                        expected_ids=expected_ids,
                    )
                ]
            )

        return report

    def _persisted(self, node_id: str, sha256: str, source: str | None = None) -> bool:
        state = self.client.call_tool("canvas_get_state")
        for node in state.get("nodes") or []:
            if node.get("id") != node_id:
                continue
            metadata = node.get("metadata") if isinstance(node.get("metadata"), Mapping) else {}
            output = metadata.get("workflowProductionOutput") if isinstance(metadata.get("workflowProductionOutput"), Mapping) else {}
            return (
                bool(metadata.get("storageKey"))
                and output.get("sha256") == sha256
                and (source is None or output.get("source", "renders") == source)
            )
        return False

    def _wait_for_persistence(self, artifact: WorkflowProductionArtifact) -> None:
        node_id = output_node_id(artifact.batch_id, artifact.config_id, artifact.source)
        deadline = time.monotonic() + self.persistence_timeout_ms / 1000
        while True:
            try:
                persisted = self._persisted(node_id, artifact.sha256, artifact.source)
            except ic_client.CanvasAgentError:
                persisted = False
            if persisted:
                return
            if time.monotonic() >= deadline or self.stopping:
                raise ExecutorExecutionError(_PERSISTENCE_TIMEOUT_DETAIL)
            self.sleep(0.1)

    def _project_artifact(
        self,
        machine: Mapping[str, Any],
        artifact: WorkflowProductionArtifact,
        journal: Path,
        request_id: str,
        *,
        expected_ids: tuple[str, ...] | None = None,
        event_name: str = "image_persisted",
    ) -> None:
        if expected_ids is not None and artifact.config_id not in expected_ids:
            raise ExecutorExecutionError("正式图片不在当前批次登记图位中。")
        main_count = (
            sum(config_id.startswith("main_") for config_id in expected_ids)
            if expected_ids is not None
            else None
        )
        state = self._call_with_reconnect(
            self.client.call_tool,
            "canvas_get_state",
        )
        current_machine = next(
            (item for item in state.get("nodes") or [] if item.get("id") == machine.get("id")),
            dict(machine),
        )
        ops, _projected = build_output_projection_ops(
            dict(current_machine),
            list(state.get("nodes") or []),
            artifact,
            self.production_base_url,
            main_count=main_count,
        )
        self._apply_with_reconnect(ops)
        self._wait_for_persistence(artifact)
        run_controller.append_event(
            journal,
            event_name,
            request_id=request_id,
            config_id=artifact.config_id,
            source=artifact.source,
            sha256=artifact.sha256,
            byte_count=artifact.byte_count,
            width=artifact.width,
            height=artifact.height,
        )

    def _backfill_persisted_event(
        self,
        journal: Path,
        artifact: WorkflowProductionArtifact,
        request_id: str,
        *,
        event_name: str = "image_persisted",
    ) -> None:
        if self._journal_has_event(
            journal,
            event_name,
            config_id=artifact.config_id,
        ):
            return
        run_controller.append_event(
            journal,
            event_name,
            request_id=request_id,
            config_id=artifact.config_id,
            source=artifact.source,
            sha256=artifact.sha256,
            byte_count=artifact.byte_count,
            width=artifact.width,
            height=artifact.height,
            backfilled=True,
        )

    def _sync_existing(
        self,
        machine: Mapping[str, Any],
        manifest: Mapping[str, Any],
        journal: Path,
        request_id: str,
    ) -> tuple[WorkflowProductionArtifact, ...]:
        expected_ids = self._expected_ids(manifest)
        artifacts = self.artifact_reader(manifest)
        artifact_ids = [artifact.config_id for artifact in artifacts]
        if (
            len(artifact_ids) != len(set(artifact_ids))
            or any(config_id not in expected_ids for config_id in artifact_ids)
        ):
            raise ProductionGateError("磁盘成图与批次登记图位不一致。")
        for artifact in artifacts:
            if not self._call_with_reconnect(
                self._persisted,
                output_node_id(artifact.batch_id, artifact.config_id, artifact.source),
                artifact.sha256,
                artifact.source,
            ):
                self._project_artifact(
                    machine,
                    artifact,
                    journal,
                    request_id,
                    expected_ids=expected_ids,
                )
            else:
                self._backfill_persisted_event(journal, artifact, request_id)
        return artifacts

    @_guard_batch_side_effect(quiet=True)
    def _backfill_render_sources(
        self,
        machine: Mapping[str, Any],
        canvas_state: Mapping[str, Any],
    ) -> None:
        try:
            selection = resolve_production_selection(
                str(machine.get("id") or ""),
                canvas_state,
            )
            manifest_path = self._manifest_path(selection.batch_id)
            manifest = self._load_manifest(manifest_path, selection.batch_id)
        except ProductionGateError:
            return
        journal = self._journal_path(manifest_path, selection.batch_id)
        if self._journal_has_event(journal, "batch_acceptance_closed"):
            return
        by_id = {
            output_node_id(artifact.batch_id, artifact.config_id): artifact
            for artifact in self.render_artifact_reader(manifest)
        }
        expected_ids = self._expected_ids(manifest)
        main_count = sum(
            config_id.startswith("main_") for config_id in expected_ids
        )
        ops: list[dict[str, Any]] = []
        for node in canvas_state.get("nodes") or []:
            artifact = by_id.get(str(node.get("id") or ""))
            if artifact is None:
                continue
            metadata = node.get("metadata") if isinstance(node.get("metadata"), Mapping) else {}
            proof = (
                metadata.get("workflowProductionOutput")
                if isinstance(metadata.get("workflowProductionOutput"), Mapping)
                else {}
            )
            if proof.get("source") == "renders" or proof.get("sourceBackfillCode"):
                continue
            ops.append(
                build_render_source_backfill_op(
                    dict(node),
                    artifact,
                    self.production_base_url,
                    main_count=main_count,
                )
            )
        if ops:
            self._apply_with_reconnect(ops)

    def _repaired_projection_update(
        self,
        node: Mapping[str, Any],
        *,
        state: Mapping[str, Any],
        status: str,
        message: str,
        projected_count: int,
    ) -> dict[str, Any]:
        updated = dict(state)
        updated.update(
            {
                "status": status,
                "message": message,
                "projectedCount": projected_count,
                "updatedAt": self.clock_ms(),
            }
        )
        return {
            "type": "update_node",
            "id": str(node.get("id") or ""),
            "metadata": {"workflowRepairedProjection": updated},
        }

    @_guard_batch_side_effect()
    def _process_repaired_projection(
        self,
        machine: Mapping[str, Any],
        canvas_state: Mapping[str, Any],
    ) -> None:
        projection_state = self._repaired_projection_state(machine)
        request_id = str(projection_state.get("requestId") or "")
        requested_at = projection_state.get("requestedAt")
        if not request_id or not isinstance(requested_at, (int, float)):
            raise ProductionGateError("返修图上桌请求格式不正确，没有执行。")
        if self.clock_ms() - int(requested_at) >= COMMAND_MAX_AGE_MS:
            raise ProductionGateError("本机工作台没有及时接到返修图上桌请求，请重新点击一次。")
        selection = resolve_production_selection(
            str(machine.get("id") or ""),
            canvas_state,
        )
        if projection_state.get("batchId") != selection.batch_id:
            raise ProductionGateError("返修图上桌请求与信息卡批次不一致。")
        manifest_path = self._manifest_path(selection.batch_id)
        manifest = self._load_manifest(manifest_path, selection.batch_id)
        journal = self._journal_path(manifest_path, selection.batch_id)
        if self._journal_has_event(journal, "batch_acceptance_closed"):
            raise BatchClosedGateError(BATCH_CLOSED_MESSAGE)
        if self._journal_seen(journal, request_id):
            raise ProductionGateError("这次返修图上桌请求已经处理，不会重复投影。")
        route = self.route_reader(manifest_path)
        if route.get("current_stage") != "ready":
            raise ProductionGateError("本批次尚未完成质检，暂不能上桌返修图。")
        artifacts = self.repaired_artifact_reader(manifest)
        self._apply_status_projection(
            [
                self._repaired_projection_update(
                    machine,
                    state=projection_state,
                    status="running",
                    message="正在把返修图放到画布上…",
                    projected_count=0,
                )
            ]
        )
        run_controller.append_event(
            journal,
            "repaired_projection_requested",
            request_id=request_id,
            count=len(artifacts),
        )
        projected_count = 0
        for artifact in artifacts:
            node_id = output_node_id(
                artifact.batch_id,
                artifact.config_id,
                artifact.source,
            )
            if not self._call_with_reconnect(
                self._persisted,
                node_id,
                artifact.sha256,
                artifact.source,
            ):
                self._project_artifact(
                    machine,
                    artifact,
                    journal,
                    request_id,
                    event_name="repaired_image_persisted",
                )
            else:
                self._backfill_persisted_event(
                    journal,
                    artifact,
                    request_id,
                    event_name="repaired_image_persisted",
                )
            projected_count += 1
        run_controller.append_event(
            journal,
            "repaired_projection_completed",
            request_id=request_id,
            count=projected_count,
        )
        self._apply_status_projection(
            [
                self._repaired_projection_update(
                    machine,
                    state=projection_state,
                    status="completed",
                    message=f"{projected_count} 张返修图已上桌。",
                    projected_count=projected_count,
                )
            ]
        )

    def _build_executor(
        self,
        step: str,
        manifest: Mapping[str, Any],
        manifest_path: Path,
        on_output: Callable[[WorkflowProductionArtifact], None],
    ) -> Executor:
        if step in UPSTREAM_STEPS or step == "qc":
            return executor_factory.build_executor("codex-dev", manifest, manifest_path)
        context = ExecutorContext(manifest=manifest, manifest_path=manifest_path, environment=self.environment)
        if step == "integrity":
            return ImageProductionExecutor(context)
        if step == "renders":
            expected_ids = self._expected_ids(manifest)
            workspace = Path(str((manifest.get("workspace") or {}).get("root") or ""))
            outputs = manifest.get("outputs") if isinstance(manifest.get("outputs"), Mapping) else {}
            render_roots = _path_values(outputs.get("renders"))
            if not render_roots or not _inside(render_roots[0], workspace):
                raise ProductionGateError("正式渲染目录不在批次工作区内。")

            image_observer = ProductionRenderObserverExecutor(
                OpenAIImageExecutor(context),
                batch_id=str(manifest.get("product_id") or ""),
                on_output=on_output,
                expected_ids=expected_ids,
            )

            def image_factory(_inner_context: ExecutorContext) -> Executor:
                return image_observer

            return ImageProductionExecutor(context, image_executor_factory=image_factory)
        raise ProductionGateError(_M2C_BOUNDARY_MESSAGE)

    @staticmethod
    def _record_content_correction(
        journal: Path,
        request_id: str,
        step: str,
        chunk_index: int,
        code: str,
        config_id: str,
    ) -> None:
        run_controller.append_event(
            journal,
            "content_correction",
            request_id=request_id,
            step=step,
            chunk_index=chunk_index,
            code=code,
            config_id=config_id,
        )

    @staticmethod
    def _controlled_failure(exc: BaseException) -> tuple[str, str] | None:
        if not isinstance(exc, ExecutorExecutionError):
            return None
        code = getattr(exc, "code", None)
        if type(code) is not str:
            code = ""
        if code == _INTEGRITY_FAILURE_CODE:
            count = getattr(exc, "blocking_issue_count", None)
            if type(count) is not int or not 1 <= count <= 9_999:
                return None
            event_detail = f"完整性检查未通过：{count} 项阻塞，报告已写入 qc_reports"
            return event_detail, f"{event_detail}。机器已停下，未自动重试。"
        if code == _REAL_EXECUTION_DISABLED_CODE:
            return (
                _REAL_EXECUTION_DISABLED_EVENT_DETAIL,
                _REAL_EXECUTION_DISABLED_WORKBENCH_MESSAGE,
            )
        detail = str(exc)
        if (
            not detail
            or len(detail) > 200
            or "\n" in detail
            or "\r" in detail
            or _UNSAFE_FAILURE_DETAIL_PATTERN.search(detail)
        ):
            return None
        if detail == _PERSISTENCE_TIMEOUT_DETAIL:
            return detail, f"{detail}。机器已停下，未自动重试。"
        match = re.fullmatch(
            r"codex-dev 收到的(?P<label>主图变量配置|详情图变量配置|"
            r"主图最终提示词|详情图最终提示词)(?P<reason>.+)",
            detail,
        )
        if match is None:
            return None
        label = match.group("label")
        reason = match.group("reason")
        if label not in _CONTROLLED_CODEX_FAILURE_LABELS:
            return None
        if reason not in _CONTROLLED_CODEX_SIMPLE_REASONS:
            claim = re.fullmatch(
                r"包含(?P<categories>未确认参数(?:、未确认商品事实)?|"
                r"未确认商品事实(?:、未确认参数)?)"
                r"（(?P<count>[1-9]\d*) 处：(?P<paths>.+)）",
                reason,
            )
            if claim is None:
                return None
            categories = claim.group("categories").split("、")
            if (
                len(categories) != len(set(categories))
                or not set(categories).issubset(_CONTROLLED_CODEX_CLAIM_CATEGORIES)
            ):
                return None
            paths = claim.group("paths").split("；")
            if not paths or any(
                re.fullmatch(r"等 [1-9]\d* 处", path) is None
                and _CONTROLLED_CODEX_CLAIM_PATH_PATTERN.fullmatch(path) is None
                for path in paths
            ):
                return None
        workbench = f"{label}未通过：{reason}。机器已停下，未自动重试。"
        return detail, workbench

    @classmethod
    def _structured_render_failure(cls, exc: BaseException) -> dict[str, object] | None:
        if not isinstance(exc, ExecutorExecutionError):
            return None
        code = getattr(exc, "code", None)
        if type(code) is not str or code not in _RENDER_FAILURE_CODES:
            return None

        if code == "render_pipeline_error":
            if cls._controlled_failure(exc) is not None:
                return None
            reason = str(exc)
            if not is_disclosable(reason):
                return None
        elif code == "render_response_invalid":
            raw_reason = str(exc)
            reason = raw_reason if is_disclosable(raw_reason) else ""
        else:
            reason = ""

        fields: dict[str, object] = {"code": code}
        if reason:
            fields["reason"] = reason
        if code == "render_inputs_unavailable":
            if getattr(exc, "missing_files", None) not in (None, (), []):
                return None
            if getattr(exc, "missing_count", None) is not None:
                return None
            if getattr(exc, "remaining_count", None) is not None:
                return None
            return fields
        if code == "render_input_missing":
            missing_count = getattr(exc, "missing_count", None)
            if type(missing_count) is not int or not 1 <= missing_count <= 9_999:
                return None
            raw_files = getattr(exc, "missing_files", ())
            if not isinstance(raw_files, (tuple, list)):
                return None
            missing_files = sanitize_filenames(raw_files)
            if missing_files and len(missing_files) != missing_count:
                missing_files = ()
            fields["missing_count"] = missing_count
            fields["missing_files"] = missing_files
            remaining_count = getattr(exc, "remaining_count", None)
            if remaining_count is not None:
                if type(remaining_count) is not int or not 0 <= remaining_count <= 9_999:
                    return None
                fields["remaining_count"] = remaining_count
            return fields

        for name, (minimum, maximum) in _RENDER_FAILURE_INTEGER_RANGES.items():
            value = getattr(exc, name, None)
            if value is None:
                continue
            if type(value) is not int or not minimum <= value <= maximum:
                return None
            fields[name] = value
        for name in _RENDER_FAILURE_TOKEN_FIELDS:
            value = getattr(exc, name, None)
            if value is None:
                continue
            if type(value) is not str:
                return None
            if value == "":
                continue
            if (
                _SAFE_RENDER_FAILURE_TOKEN_PATTERN.fullmatch(value) is None
                or _UNSAFE_FAILURE_DETAIL_PATTERN.search(value)
            ):
                return None
            fields[name] = value
        for name in _RENDER_FAILURE_SHAPE_FIELDS:
            value = getattr(exc, name, None)
            if not isinstance(value, tuple):
                continue
            sanitized = tuple(
                sorted(
                    {
                        item
                        for item in value
                        if type(item) is str
                        and _SAFE_RENDER_FAILURE_TOKEN_PATTERN.fullmatch(item) is not None
                        and _UNSAFE_FAILURE_DETAIL_PATTERN.search(item) is None
                        and not is_sensitive_identifier(item)
                    }
                )[:8]
            )
            if sanitized:
                fields[name] = sanitized
        return fields

    def _render_failure_recovery(
        self,
        fields: Mapping[str, object],
        manifest: Mapping[str, Any],
    ) -> dict[str, object] | None:
        code = fields.get("code")
        if code == "render_inputs_unavailable":
            return {
                "kind": "inputs_unavailable",
                "files": [],
                "recomputeEligible": False,
            }
        if code != "render_input_missing":
            return None
        missing_count = fields.get("missing_count")
        remaining_count = fields.get("remaining_count")
        if type(missing_count) is not int or missing_count < 1:
            return None
        eligible = False
        if type(remaining_count) is int and remaining_count >= 1:
            try:
                eligible = len(self.artifact_reader(manifest)) == 0
            except Exception:
                eligible = False
        return {
            "kind": "missing_reference",
            "files": list(fields.get("missing_files") or ()),
            "recomputeEligible": eligible,
        }

    @classmethod
    def _structured_render_failure_messages(
        cls,
        exc: BaseException,
        *,
        recompute_eligible: bool = False,
    ) -> tuple[str, str, str] | None:
        fields = cls._structured_render_failure(exc)
        if fields is None:
            return None

        code = str(fields["code"])
        if code == "render_input_missing":
            missing_count = int(fields["missing_count"])
            missing_files = tuple(fields.get("missing_files") or ())
            if missing_files:
                introduction = (
                    f"白底图 {'、'.join(str(item) for item in missing_files)} "
                    "已不在批次目录里。"
                )
                event = f"渲染失败：白底图 {'、'.join(str(item) for item in missing_files)} 缺失"
            else:
                introduction = f"有 {missing_count} 张白底图已不在批次目录里。"
                event = f"渲染失败：缺失 {missing_count} 张白底图"
            remaining_count = fields.get("remaining_count")
            if (
                recompute_eligible
                and type(remaining_count) is int
                and remaining_count >= 1
            ):
                workbench = (
                    f"{introduction}可恢复文件后重新开始；或剔除缺失图，"
                    f"用剩余 {remaining_count} 张重新分配角度与绑定"
                    "（重排不产生模型费用，出图前会重新报价并由你确认）。"
                )
            else:
                workbench = f"{introduction}可恢复文件后重新开始。"
            return event[:160], workbench, code
        if code == "render_inputs_unavailable":
            return (
                "渲染失败：白底图目录整体无法访问",
                "白底图目录整体无法访问，本次已停止。请恢复 inputs/white_bg 后再重新开始。",
                code,
            )

        count_labels = (
            ("successful_count", "成功"),
            ("planned_count", "计划"),
            ("skipped_count", "跳过"),
        )
        workbench_counts = [
            f"{label} {fields[name]} 张"
            for name, label in count_labels
            if name in fields
        ]
        event_counts = [
            f"{label} {fields[name]}"
            for name, label in count_labels
            if name in fields
        ]
        count_sentence = f"本轮{'、'.join(workbench_counts)}。" if workbench_counts else ""
        stop_sentence = "机器已停下，未自动重试，已完成的成果都保留了。"

        if code == "render_pipeline_error":
            reason = str(fields["reason"])
            display_reason = reason.rstrip("。") or reason
            event_suffix = f"；{'/'.join(event_counts)}" if event_counts else ""
            event_prefix = "渲染失败："
            available = max(0, 160 - len(event_prefix) - len(event_suffix))
            event = f"{event_prefix}{display_reason[:available]}{event_suffix}"
            workbench = f"{display_reason}。{count_sentence}{stop_sentence}"
            return event, workbench, code
        if code == "render_response_invalid":
            reason = str(fields.get("reason") or "图片服务返回的内容无法使用")
            display_reason = reason.rstrip("。") or reason
            shape_parts: list[str] = []
            if "response_top_keys" in fields:
                shape_parts.append(
                    f"响应字段：{'、'.join(str(item) for item in fields['response_top_keys'])}"
                )
            if "response_data0_keys" in fields:
                shape_parts.append(
                    "data[0] 字段："
                    + "、".join(str(item) for item in fields["response_data0_keys"])
                )
            shape_sentence = f"{'；'.join(shape_parts)}。" if shape_parts else ""
            event_reason = display_reason
            if "reason" not in fields:
                event_reason += "。"
            event_counts_text = "、".join(event_counts)
            event_suffix = (
                ("" if event_reason.endswith("。") else "；") + event_counts_text
                if event_counts_text
                else ""
            )
            event_prefix = "渲染失败："
            available = max(0, 160 - len(event_prefix) - len(event_suffix))
            base_event = f"{event_prefix}{event_reason[:available]}{event_suffix}"
            base_workbench = f"{display_reason}。{count_sentence}{stop_sentence}"
            if shape_sentence:
                shaped_event = f"{event_prefix}{event_reason}"
                shaped_event += (
                    "" if shaped_event.endswith("。") else "；"
                ) + shape_sentence.rstrip("。")
                if event_counts_text:
                    shaped_event += f"；{event_counts_text}"
                shaped_workbench = (
                    f"{display_reason}。{shape_sentence}{count_sentence}{stop_sentence}"
                )
                if (
                    len(shaped_event) <= 160
                    and is_disclosable(shaped_event)
                    and is_disclosable(shaped_workbench)
                ):
                    base_event = shaped_event
                    base_workbench = shaped_workbench
            return base_event, base_workbench, code
        if code == "render_canvas_unavailable":
            title = "画布暂时不可用，真实图片未完成上桌"
            workbench = f"{title}。{count_sentence}{stop_sentence}"
            event_parts = [f"渲染失败：{title}"]
        elif code == "render_http_error":
            description_parts: list[str] = []
            if "http_status" in fields:
                description_parts.append(f"HTTP {fields['http_status']}")
            if "provider_error_type" in fields:
                description_parts.append(f"类型 {fields['provider_error_type']}")
            if "provider_error_code" in fields:
                description_parts.append(f"代码 {fields['provider_error_code']}")
            title = (
                f"图片服务返回错误（{'，'.join(description_parts)}）。"
                if description_parts
                else "图片服务返回错误。"
            )
            request_sentence = (
                f"服务商请求编号：{fields['provider_request_id']}（可凭此联系服务商）。"
                if "provider_request_id" in fields
                else ""
            )
            workbench = f"{title}{count_sentence}{stop_sentence}{request_sentence}"
            event_parts = [
                (
                    f"渲染失败：HTTP {fields['http_status']}"
                    if "http_status" in fields
                    else "渲染失败：图片服务返回错误"
                )
            ]
            if "provider_error_type" in fields:
                event_parts.append(f"类型 {fields['provider_error_type']}")
            if "provider_error_code" in fields:
                event_parts.append(f"代码 {fields['provider_error_code']}")
        elif code == "render_timeout":
            timeout = (
                f"（{fields['timeout_seconds']} 秒）"
                if "timeout_seconds" in fields
                else ""
            )
            workbench = f"图片服务等待超时{timeout}。{count_sentence}{stop_sentence}"
            event_parts = [
                (
                    f"渲染失败：图片服务等待超时 {fields['timeout_seconds']} 秒"
                    if "timeout_seconds" in fields
                    else "渲染失败：图片服务等待超时"
                )
            ]
        elif code == "render_image_download_failed":
            status = (
                f"（HTTP {fields['http_status']}）"
                if "http_status" in fields
                else ""
            )
            shape_parts: list[str] = []
            if "response_top_keys" in fields:
                shape_parts.append(
                    f"响应字段：{'、'.join(str(item) for item in fields['response_top_keys'])}"
                )
            if "response_data0_keys" in fields:
                shape_parts.append(
                    "data[0] 字段："
                    + "、".join(str(item) for item in fields["response_data0_keys"])
                )
            shape_sentence = f"{'；'.join(shape_parts)}。" if shape_parts else ""
            workbench = (
                f"图片服务已返回图片链接，但图片未能取回{status}。"
                f"{shape_sentence}{count_sentence}{stop_sentence}"
            )
            event_parts = [
                (
                    f"渲染失败：图片未能取回 HTTP {fields['http_status']}"
                    if "http_status" in fields
                    else "渲染失败：图片未能取回"
                )
            ]
            if shape_sentence:
                event_parts.append(shape_sentence.rstrip("。"))
        else:
            workbench = f"无法连接图片服务。{count_sentence}{stop_sentence}"
            event_parts = ["渲染失败：无法连接图片服务"]

        if event_counts:
            event_parts.append("/".join(event_counts))
        if code == "render_http_error" and "provider_request_id" in fields:
            event_parts.append(f"请求编号 {fields['provider_request_id']}")
        return "；".join(event_parts)[:160], workbench, code

    @classmethod
    def _safe_event_detail(cls, exc: BaseException) -> str:
        controlled = cls._controlled_failure(exc)
        if controlled is not None:
            return controlled[0][:160]
        structured = cls._structured_render_failure_messages(exc)
        if structured is not None:
            return structured[0]
        raw_detail = str(exc)
        if (
            not raw_detail
            or "/" in raw_detail
            or "\\" in raw_detail
            or _UNSAFE_FAILURE_DETAIL_PATTERN.search(raw_detail)
        ):
            return "执行已停止，未自动重试"
        detail = " ".join(raw_detail.split())
        return detail[:160] if detail else "执行已停止，未自动重试"

    @classmethod
    def _safe_failure(
        cls,
        exc: BaseException,
        *,
        recompute_eligible: bool = False,
    ) -> str:
        if isinstance(exc, ProductionGateError):
            return str(exc)
        code = getattr(exc, "code", None)
        if type(code) is not str:
            code = ""
        if (
            isinstance(exc, ExecutorExecutionError)
            and code == _REAL_EXECUTION_DISABLED_CODE
        ):
            return _REAL_EXECUTION_DISABLED_WORKBENCH_MESSAGE
        structured = cls._structured_render_failure_messages(
            exc,
            recompute_eligible=recompute_eligible,
        )
        if structured is not None:
            return structured[1]
        if isinstance(exc, ExecutorExecutionError) and "2:3" in str(exc):
            return "详情图返回 2:3，原图已保留。机器已停下，等待人工尺寸处理批准。"
        if isinstance(exc, ExecutorExecutionError) and "OPENAI_API_KEY" in str(exc):
            return "前面的成果已保留。本机还没有准备图片服务凭据，当前未出图、未产生新的图片费用。"
        if isinstance(exc, ExecutorExecutionError) and code == "empty_assistant_response":
            return "本地 Codex 本轮没有返回内容，机器已停下，未自动重试。"
        controlled = cls._controlled_failure(exc)
        if controlled is not None:
            return controlled[1]
        return "这一步没做好，机器已停下。已经完成的成果都保留了。"

    def _execute_step_attempt(
        self,
        *,
        step: str,
        manifest: Mapping[str, Any],
        manifest_path: Path,
        on_output: Callable[[WorkflowProductionArtifact], None],
        journal: Path,
        request_id: str,
        machine: Mapping[str, Any],
        turn_progress_machine: Mapping[str, Any] | None,
        accepted_content: str,
        produced_count: int,
        total_count: int,
        expected_ids: tuple[str, ...],
    ) -> ExecutionResult:
        if self._uses_default_executor_builder:
            executor = self._build_executor(
                step,
                manifest,
                manifest_path,
                on_output,
            )
        else:
            executor = self.executor_builder(step, manifest, manifest_path, on_output)

        heartbeat_worker: _QcHeartbeatWorker | None = None
        execution_succeeded = False
        try:
            if step in {"main_vc", "detail_vc"}:
                binder = getattr(
                    executor,
                    "set_content_correction_callback",
                    None,
                )
                if callable(binder):
                    binder(
                        lambda chunk_index, code, config_id: self._record_content_correction(
                            journal,
                            request_id,
                            step,
                            chunk_index,
                            code,
                            config_id,
                        )
                    )
            if turn_progress_machine is not None:
                heartbeat_worker = self._start_qc_heartbeat_worker(request_id)
                binder = getattr(executor, "set_turn_progress_callback", None)
                if callable(binder):
                    binder(
                        self._turn_progress_callback(
                            heartbeat_worker,
                            turn_progress_machine,
                            accepted_content,
                            step=step,
                            produced_count=produced_count,
                            total_count=total_count,
                            expected_ids=expected_ids,
                        )
                    )
            if step == "qc":
                heartbeat_worker = self._start_qc_heartbeat_worker(request_id)
                binder = getattr(executor, "set_qc_progress_callback", None)
                if callable(binder):
                    binder(
                        self._qc_progress_callback(
                            heartbeat_worker,
                            machine,
                            accepted_content,
                            total_count=total_count,
                            expected_ids=expected_ids,
                            chunk_count=qc_chunk_count(total_count),
                        )
                    )
            result = run_controller.execute_step(executor, step)
            execution_succeeded = True
            return result
        except ExecutorExecutionError as exc:
            code = getattr(exc, "code", None)
            if type(code) is not str:
                code = ""
            if code == "empty_assistant_response" and self.diagnostic_recorder is not None:
                self.diagnostic_recorder(step, code)
            if step == "integrity":
                try:
                    integrity_state = self.integrity_reader(self.route_reader(manifest_path))
                    report_path = Path(str(integrity_state.get("path") or ""))
                    if (
                        integrity_state.get("found") is True
                        and integrity_state.get("status") == "fail"
                        and integrity_state.get("render_blocked") is True
                        and report_path.name == "final_prompt_integrity_report.json"
                    ):
                        report = json.loads(report_path.read_text(encoding="utf-8"))
                        count = report.get("blocking_issue_count") if isinstance(report, dict) else None
                        if (
                            isinstance(report, dict)
                            and report.get("status") == "fail"
                            and report.get("render_blocked") is True
                            and type(count) is int
                            and 1 <= count <= 9_999
                        ):
                            exc.code = _INTEGRITY_FAILURE_CODE
                            exc.blocking_issue_count = count
                except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
                    pass
            raise
        finally:
            if heartbeat_worker is not None:
                self._close_qc_heartbeat_worker(
                    heartbeat_worker,
                    drain=execution_succeeded,
                )

    def _reject(
        self,
        node: Mapping[str, Any],
        request_id: str,
        message: str,
        *,
        recovery: Mapping[str, Any] | None = None,
        failure_source: str | None = None,
    ) -> None:
        self._apply_status_projection(
            [
                self._machine_update(
                    node,
                    status="failed",
                    content=f"# request-id: {request_id}\n# failed",
                    error_message=message,
                    recovery=recovery,
                    failure_source=failure_source,
                )
            ]
        )

    @_guard_batch_side_effect()
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
        if self._journal_has_event(journal, "batch_acceptance_closed"):
            raise BatchClosedGateError(BATCH_CLOSED_MESSAGE)
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

        expected_ids = self._expected_ids(manifest)
        total_count = len(expected_ids)
        qc_requested = "qc_reports" in (manifest.get("requested_outputs") or [])
        accepted_content = f"# request-id: {request_id}\n# accepted"
        existing = self._sync_existing(machine, manifest, journal, request_id)
        produced_count = len(existing)
        command_text = content

        while not self.stopping:
            route = self.route_reader(manifest_path)
            integrity = self.integrity_reader(route)
            stage = str(route.get("current_stage") or "")
            if stage == "ready":
                parsed = run_controller.parse_run_content(command_text) if command_text else ("run", "next")
                if parsed != ("run", "next"):
                    raise ProductionGateError(_M2C_BOUNDARY_MESSAGE)
                if (
                    not qc_requested
                    and produced_count >= total_count
                    and not self._journal_has_event(journal, "production_completed")
                ):
                    run_controller.append_event(
                        journal,
                        "production_completed",
                        request_id=request_id,
                        produced_count=total_count,
                    )
                self._apply_status_projection(
                    [
                        self._machine_update(
                            machine,
                            status="completed",
                            content=f"# request-id: {request_id}\n# completed",
                            produced_count=total_count,
                            total_count=total_count,
                            expected_ids=expected_ids,
                            message=(
                                "质检完成，QC 报告已生成。"
                                if qc_requested
                                else f"{total_count} 张真实图片已全部完成。"
                            ),
                        )
                    ]
                )
                return
            if (
                produced_count >= total_count
                and stage == "needs_qc_reports"
                and not self._journal_has_event(journal, "production_completed")
            ):
                run_controller.append_event(
                    journal,
                    "production_completed",
                    request_id=request_id,
                    produced_count=total_count,
                )
            step = resolve_gated_step(command_text, route, integrity)
            if step not in M2C_STEPS:
                raise ProductionGateError(_M2C_BOUNDARY_MESSAGE)
            blocked = set_batch_blocked_message(manifest, step)
            if blocked:
                raise ProductionGateError(blocked)
            turn_progress_machine = (
                copy.deepcopy(machine)
                if step in _TURN_PROGRESS_HEARTBEAT_STEPS
                else None
            )
            # This is a deny-only phase boundary, not an execution route.  The
            # existing run controller has already parsed and resolved the next
            # step above; when the image gate is closed we stop before gate 3,
            # so no integrity/render executor is called or recorded as started.
            if step in {"integrity", "renders"} and self.environment.get("RENDER_ALLOW_REAL_EXECUTION") != "1":
                self._apply_status_projection(
                    [
                        self._machine_update(
                            machine,
                            status="paused",
                            content=f"# request-id: {request_id}\n# gate-paused",
                            step=step,
                            produced_count=produced_count,
                            total_count=total_count,
                            expected_ids=expected_ids,
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
            self._apply_status_projection(
                [
                    self._machine_update(
                        machine,
                        status="running",
                        content=accepted_content,
                        step=step,
                        produced_count=produced_count,
                        total_count=total_count,
                        expected_ids=expected_ids,
                    )
                ]
            )

            def on_output(artifact: WorkflowProductionArtifact) -> None:
                self._project_artifact(
                    machine,
                    artifact,
                    journal,
                    request_id,
                    expected_ids=expected_ids,
                )
                current = len(self.artifact_reader(manifest))
                self._apply_status_projection(
                    [
                        self._machine_update(
                            machine,
                            status="running",
                            content=accepted_content,
                            step="renders",
                            produced_count=current,
                            total_count=total_count,
                            expected_ids=expected_ids,
                        )
                    ]
                )

            attempt = 1
            while True:
                try:
                    result = self._execute_step_attempt(
                        step=step,
                        manifest=manifest,
                        manifest_path=manifest_path,
                        on_output=on_output,
                        journal=journal,
                        request_id=request_id,
                        machine=machine,
                        turn_progress_machine=turn_progress_machine,
                        accepted_content=accepted_content,
                        produced_count=produced_count,
                        total_count=total_count,
                        expected_ids=expected_ids,
                    )
                    break
                except ExecutorExecutionError as exc:
                    if (
                        step not in STEP_AUTO_RETRY_STEPS
                        or attempt > self.step_auto_retry_limit
                        or self.stopping
                    ):
                        raise
                    run_controller.append_event(
                        journal,
                        "step_auto_retry",
                        request_id=request_id,
                        step=step,
                        attempt=attempt,
                        detail=self._safe_event_detail(exc),
                    )
                    attempt += 1
            run_controller.append_event(journal, "step_succeeded", request_id=request_id, step=step, detail=result.detail[:160])
            if step == "renders":
                produced_count = len(self.artifact_reader(manifest))
                route = self.route_reader(manifest_path)
                completed_render_stage = (
                    route.get("current_stage") == "needs_qc_reports"
                    or (
                        not qc_requested
                        and route.get("current_stage") == "ready"
                    )
                )
                if (
                    produced_count < total_count
                    or not completed_render_stage
                ):
                    self._apply_status_projection(
                        [
                            self._machine_update(
                                machine,
                                status="paused",
                                content=f"# request-id: {request_id}\n# paused",
                                produced_count=produced_count,
                                total_count=total_count,
                                expected_ids=expected_ids,
                            )
                        ]
                    )
                    run_controller.append_event(journal, "production_paused", request_id=request_id, produced_count=produced_count)
                    return
            produced_count = len(self.artifact_reader(manifest))
            route = self.route_reader(manifest_path)
            command_text = (
                next_gated_command(
                    route,
                    accepted_render_count=produced_count,
                    total_count=total_count,
                )
                or ""
            )

    def poll_once(self) -> None:
        state = self.client.call_tool("canvas_get_state")
        for node in list(state.get("nodes") or []):
            if node.get("type") != "workflow":
                continue
            self._backfill_render_sources(node, state)
            repaired_projection = self._repaired_projection_state(node)
            if repaired_projection.get("status") == "queued":
                try:
                    self._process_repaired_projection(node, state)
                except ProductionGateError as exc:
                    self._apply_status_projection(
                        [
                            self._repaired_projection_update(
                                node,
                                state=repaired_projection,
                                status="failed",
                                message=str(exc),
                                projected_count=0,
                            )
                        ]
                    )
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
                recovery: dict[str, object] | None = None
                failure_source: str | None = None
                if not isinstance(
                    exc,
                    (
                        BatchClosedGateError,
                        BatchRecycledGateError,
                        BatchOperationBusyGateError,
                        BatchLifecycleGateError,
                    ),
                ):
                    try:
                        manifest_path = self._manifest_path(batch_id)
                        structured_failure = self._structured_render_failure(exc)
                        if structured_failure is not None:
                            if structured_failure["code"] in _IMAGE_SERVICE_FAILURE_CODES:
                                failure_source = _IMAGE_SERVICE_FAILURE_SOURCE
                            try:
                                manifest = self._load_manifest(manifest_path, batch_id)
                                recovery = self._render_failure_recovery(
                                    structured_failure,
                                    manifest,
                                )
                            except ProductionGateError:
                                recovery = None
                        failure_fields = (
                            {"failure_code": structured_failure["code"]}
                            if structured_failure is not None
                            else {}
                        )
                        run_controller.append_event(
                            self._journal_path(manifest_path, batch_id),
                            "step_failed",
                            request_id=request_id,
                            detail=self._safe_event_detail(exc),
                            **failure_fields,
                        )
                    except ProductionGateError:
                        pass
                recompute_eligible = bool(
                    recovery is not None
                    and recovery.get("recomputeEligible") is True
                )
                self._reject(
                    node,
                    request_id,
                    self._safe_failure(
                        exc,
                        recompute_eligible=recompute_eligible,
                    ),
                    recovery=recovery,
                    failure_source=failure_source,
                )

    def serve_forever(self) -> None:
        while not self.stopping:
            try:
                self.poll_once()
            except ic_client.CanvasAgentError:
                pass
            self.sleep(self.interval)
