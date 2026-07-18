"""M1-b daemon: consume workflow-node commands and stream demo PNGs.

Unlike the legacy ``--serve`` route, this service never projects stage, run,
or log nodes.  It only reads existing Workflow nodes and updates the machine
that submitted a command plus ``wfdemo-output:`` result nodes.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

import executor_factory
import ic_client
import run_controller
import state_reader
from executor_contract import Executor
from make_demo_workspace import prepare_workflow_demo
from workflow_demo_executor import (
    ExecutorExecutionError,
    WorkflowDemoArtifact,
    require_demo_workspace,
    require_demo_write_path,
)
from workflow_demo_projection import build_output_projection_ops, clear_workflow_demo_output_ids


COMMAND_MAX_AGE_MS = 8_000
SERVICE_EVENT_NAME = "workflow_demo_service.events.jsonl"


class WorkflowDemoService:
    def __init__(
        self,
        manifest_path: Path,
        *,
        client: Any = ic_client,
        executor: Executor | None = None,
        clock_ms: Callable[[], int] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        interval: float = 2.0,
    ) -> None:
        self.manifest_path = manifest_path.resolve()
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError("demo manifest 必须是 JSON 对象")
        workspace = manifest.get("workspace") or {}
        root_value = workspace.get("root")
        if not root_value:
            raise ValueError("demo manifest 缺少 workspace.root")
        self.workspace_root = require_demo_workspace(Path(str(root_value)))
        if self.workspace_root not in self.manifest_path.parents:
            raise ExecutorExecutionError("demo manifest 路径越界")

        prepare_workflow_demo(self.workspace_root)
        self.manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        self.client = client
        self.executor = executor or executor_factory.build_executor(
            "workflow-demo", self.manifest, self.manifest_path
        )
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.sleep = sleep
        self.interval = interval
        self.consumed_content: dict[str, str] = {}
        self.event_path = require_demo_write_path(
            self.workspace_root,
            self.manifest_path.parent / SERVICE_EVENT_NAME,
        )
        self.seen_request_ids = self._read_seen_request_ids()
        self.stopping = False

    def _read_seen_request_ids(self) -> set[str]:
        if not self.event_path.is_file():
            return set()
        seen: set[str] = set()
        for line in self.event_path.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            request_id = entry.get("request_id") if isinstance(entry, dict) else None
            if isinstance(request_id, str) and request_id:
                seen.add(request_id)
        return seen

    def _append_event(self, event: str, *, request_id: str, **fields: Any) -> None:
        require_demo_write_path(self.workspace_root, self.event_path)
        run_controller.append_event(self.event_path, event, request_id=request_id, **fields)
        self.seen_request_ids.add(request_id)

    @staticmethod
    def _workflow_state(node: dict[str, Any]) -> dict[str, Any]:
        metadata = node.get("metadata") or {}
        state = metadata.get("workflowDemo") or {}
        return dict(state) if isinstance(state, dict) else {}

    def _machine_update(
        self,
        node: dict[str, Any],
        *,
        status: str,
        content: str,
        produced_count: int | None = None,
        completed_runs: int | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        state = self._workflow_state(node)
        state["status"] = status
        state["updatedAt"] = self.clock_ms()
        if produced_count is not None:
            state["producedCount"] = produced_count
        if completed_runs is not None:
            state["completedRuns"] = completed_runs
        if error_message:
            state["errorMessage"] = error_message
        else:
            state.pop("errorMessage", None)
        return {
            "type": "update_node",
            "id": str(node.get("id") or ""),
            "metadata": {"content": content, "workflowDemo": state},
        }

    def _apply_with_reconnect(self, ops: list[dict[str, Any]]) -> None:
        while not self.stopping:
            try:
                self.client.apply_ops(ops)
                return
            except ic_client.CanvasAgentError as exc:
                print(json.dumps({"workflow_demo": "waiting_canvas", "error": str(exc)[:120]}, ensure_ascii=False), flush=True)
                self.sleep(max(self.interval, 2.0))
        raise ExecutorExecutionError("演示服务已停止")

    def _reject(self, node: dict[str, Any], request_id: str, message: str, *, event_detail: str) -> None:
        self._append_event("gate_rejected", request_id=request_id, detail=event_detail)
        self._apply_with_reconnect(
            [
                self._machine_update(
                    node,
                    status="failed",
                    content=f"# request-id: {request_id}\n# rejected",
                    error_message=message,
                )
            ]
        )

    def poll_once(self) -> None:
        state = self.client.call_tool("canvas_get_state")
        nodes = list(state.get("nodes") or [])
        for node in nodes:
            if node.get("type") != "workflow":
                continue
            workflow_state = self._workflow_state(node)
            if workflow_state.get("status") != "queued":
                continue
            metadata = node.get("metadata") or {}
            content = str(metadata.get("content") or "")
            node_id = str(node.get("id") or "")
            if not node_id or self.consumed_content.get(node_id) == content:
                continue
            self.consumed_content[node_id] = content
            request_id = str(workflow_state.get("runId") or "")
            requested_at = workflow_state.get("requestedAt")
            if not request_id:
                self._reject(node, "missing-request", "这次开始请求格式不正确，没有执行。", event_detail="missing request id")
                continue
            if request_id in self.seen_request_ids:
                self._apply_with_reconnect(
                    [
                        self._machine_update(
                            node,
                            status="failed",
                            content=f"# request-id: {request_id}\n# duplicate ignored",
                            error_message="这次请求已经处理，不会重复生成。",
                        )
                    ]
                )
                continue
            if not isinstance(requested_at, (int, float)) or self.clock_ms() - int(requested_at) >= COMMAND_MAX_AGE_MS:
                self._reject(node, request_id, "本机演示服务没有及时接单，请重新开始一次。", event_detail="stale queued command")
                continue

            try:
                command = run_controller.parse_run_content(content)
            except run_controller.RunValidationError as exc:
                self._reject(node, request_id, "这次开始请求格式不正确，没有执行。", event_detail=str(exc))
                continue
            if command is None:
                self._reject(node, request_id, "这次开始请求格式不正确，没有执行。", event_detail="missing command")
                continue

            route = state_reader.read_batch_route(self.manifest_path)
            integrity = state_reader.integrity_report_status(route)
            try:
                step = run_controller.resolve_command(command, route, integrity)
            except run_controller.RunValidationError as exc:
                self._reject(node, request_id, "演示状态暂时不允许开始，请重新开始一次。", event_detail=str(exc))
                continue

            self._append_event("command_received", request_id=request_id, command=f"{command[0]}: {command[1]}")
            self._append_event("step_started", request_id=request_id, step=step)
            accepted_content = f"# request-id: {request_id}\n# accepted"
            self._apply_with_reconnect(
                [self._machine_update(node, status="running", content=accepted_content, produced_count=0)]
            )
            occupied_nodes = list(nodes)

            def on_output(artifact: WorkflowDemoArtifact) -> None:
                projection_ops, projected = build_output_projection_ops(node, occupied_nodes, request_id, artifact)
                projection_ops.append(
                    self._machine_update(
                        node,
                        status="running",
                        content=accepted_content,
                        produced_count=artifact.index,
                    )
                )
                self._apply_with_reconnect(projection_ops)
                occupied_nodes.append(projected)
                self._append_event(
                    "image_projected",
                    request_id=request_id,
                    step=step,
                    index=artifact.index,
                    filename=artifact.path.name,
                )

            try:
                result = run_controller.execute_step_with_metadata(
                    self.executor,
                    step,
                    metadata={
                        "run_id": request_id,
                        "on_output": on_output,
                        "should_cancel": lambda: self.stopping,
                    },
                )
            except run_controller.RunExecutionError as exc:
                message = str(exc)
                self._append_event("step_failed", request_id=request_id, step=step, detail=message)
                self._apply_with_reconnect(
                    [
                        self._machine_update(
                            node,
                            status="failed",
                            content=f"# request-id: {request_id}\n# failed",
                            error_message="演示没有完成，已经完成的图片仍然保留。",
                        )
                    ]
                )
                continue

            completed_runs = int(workflow_state.get("completedRuns") or 0) + 1
            self._append_event("step_succeeded", request_id=request_id, step=step, detail=result.detail)
            self._apply_with_reconnect(
                [
                    self._machine_update(
                        node,
                        status="completed",
                        content=f"# request-id: {request_id}\n# completed",
                        produced_count=14,
                        completed_runs=completed_runs,
                    )
                ]
            )

    def serve_forever(self) -> None:
        while not self.stopping:
            try:
                self.poll_once()
            except ic_client.CanvasAgentError as exc:
                print(json.dumps({"workflow_demo": "waiting_canvas", "error": str(exc)[:120]}, ensure_ascii=False), flush=True)
            self.sleep(self.interval)


class WorkflowDemoServiceLock:
    """A held one-byte OS file lock; released automatically on process exit."""

    def __init__(self, workspace_root: Path):
        self.workspace_root = require_demo_workspace(workspace_root)
        self.path = require_demo_write_path(self.workspace_root, self.workspace_root / ".workflow_demo_service.lock")
        self.handle = None

    def __enter__(self):
        require_demo_write_path(self.workspace_root, self.path)
        self.handle = self.path.open("a+b")
        if self.path.stat().st_size == 0:
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            try:
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            except ImportError:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            self.handle = None
            raise RuntimeError("demo 桥接服务已在运行") from exc
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        if self.handle is None:
            return
        try:
            self.handle.seek(0)
            try:
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            except ImportError:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None


def cmd_serve_workflow_demo(manifest_path: Path, interval: float) -> None:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    root = Path(str((manifest.get("workspace") or {}).get("root") or ""))
    with WorkflowDemoServiceLock(root):
        service = WorkflowDemoService(manifest_path, interval=interval)
        print(
            json.dumps(
                {"workflow_demo": "started", "interval": interval, "manifest": str(manifest_path)},
                ensure_ascii=False,
            ),
            flush=True,
        )
        try:
            service.serve_forever()
        except KeyboardInterrupt:
            service.stopping = True
        print(json.dumps({"workflow_demo": "stopped"}, ensure_ascii=False), flush=True)


def cmd_clear_workflow_demo(machine_id: str) -> None:
    """Manual acceptance cleanup; only wfdemo-output ids are eligible."""
    if not machine_id.strip():
        raise ValueError("清理演示结果必须指定机器节点 id")
    state = ic_client.call_tool("canvas_get_state")
    ids = clear_workflow_demo_output_ids(state.get("nodes") or [], machine_id=machine_id)
    if ids:
        ic_client.apply_ops([{"type": "delete_node", "ids": ids}])
    print(json.dumps({"workflow_demo_clear": "manual", "deleted": len(ids), "ids": ids}, ensure_ascii=False))
