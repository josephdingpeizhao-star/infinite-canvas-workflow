from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

from executor_contract import ExecutionResult  # noqa: E402
from make_demo_workspace import build_manifest, prepare_workflow_demo  # noqa: E402
from workflow_demo_service import WorkflowDemoService  # noqa: E402


class FakeClient:
    def __init__(self, nodes: list[dict]):
        self.state = {"nodes": nodes, "connections": [], "viewport": {"x": 0, "y": 0, "k": 1}}
        self.applied: list[list[dict]] = []

    def call_tool(self, name: str):
        if name != "canvas_get_state":
            raise AssertionError(name)
        return self.state

    def apply_ops(self, ops: list[dict]):
        self.applied.append(ops)
        return 1


class RecordingExecutor:
    name = "workflow-demo"

    def __init__(self):
        self.requests = []

    def execute(self, request):
        self.requests.append(request)
        return ExecutionResult(detail="演示完成", provider=self.name)


def queued_node(*, content: str = "# request-id: req-001\nrun: renders", requested_at: int = 10_000) -> dict:
    return {
        "id": "workflow-1",
        "type": "workflow",
        "title": "生图工作流",
        "position": {"x": 0, "y": 0},
        "width": 420,
        "height": 300,
        "metadata": {
            "content": content,
            "workflowDemo": {
                "status": "queued",
                "producedCount": 0,
                "completedRuns": 0,
                "runId": "req-001",
                "requestedAt": requested_at,
                "updatedAt": requested_at,
            },
        },
    }


class WorkflowDemoServiceTests(unittest.TestCase):
    def workspace(self, parent: Path) -> tuple[Path, Path]:
        root = parent / "demo"
        root.mkdir()
        (root / ".canvas_demo").write_text("safe\n", encoding="utf-8")
        manifest_path = root / "manifests" / "batch_manifest.json"
        manifest_path.parent.mkdir()
        manifest_path.write_text(json.dumps(build_manifest(root), ensure_ascii=False), encoding="utf-8")
        prepare_workflow_demo(root)
        return root, manifest_path

    def test_prepare_workspace_is_idempotent_and_creates_no_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, manifest_path = self.workspace(Path(tmp))
            before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
            prepare_workflow_demo(root)
            self.assertEqual(before, {path: path.read_bytes() for path in root.rglob("*") if path.is_file()})
            self.assertFalse(list((root / "outputs" / "renders").rglob("*.png")))
            self.assertTrue(manifest_path.is_file())

    def test_prepare_refuses_unmarked_workspace_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "unmarked"
            root.mkdir()
            with self.assertRaisesRegex(SystemExit, "canvas_demo"):
                prepare_workflow_demo(root)
            self.assertEqual([], list(root.iterdir()))

    def test_poll_consumes_one_command_and_updates_machine(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, manifest_path = self.workspace(Path(tmp))
            client = FakeClient([queued_node()])
            executor = RecordingExecutor()
            service = WorkflowDemoService(
                manifest_path,
                client=client,
                executor=executor,
                clock_ms=lambda: 11_000,
                sleep=lambda _seconds: None,
            )
            service.poll_once()
            self.assertEqual(1, len(executor.requests))
            self.assertEqual("renders", executor.requests[0].step)
            self.assertEqual("req-001", executor.requests[0].metadata["run_id"])
            flattened = [op for batch in client.applied for op in batch]
            machine_updates = [op for op in flattened if op.get("type") == "update_node" and op.get("id") == "workflow-1"]
            self.assertTrue(any(op["metadata"]["workflowDemo"]["status"] == "running" for op in machine_updates))
            self.assertTrue(any(op["metadata"]["workflowDemo"]["status"] == "completed" for op in machine_updates))

    def test_cached_echo_and_service_restart_journal_do_not_repeat_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, manifest_path = self.workspace(Path(tmp))
            client = FakeClient([queued_node()])
            first_executor = RecordingExecutor()
            first = WorkflowDemoService(manifest_path, client=client, executor=first_executor, clock_ms=lambda: 11_000, sleep=lambda _seconds: None)
            first.poll_once()
            first.poll_once()
            self.assertEqual(1, len(first_executor.requests))

            second_executor = RecordingExecutor()
            restarted = WorkflowDemoService(manifest_path, client=client, executor=second_executor, clock_ms=lambda: 11_000, sleep=lambda _seconds: None)
            restarted.poll_once()
            self.assertEqual([], second_executor.requests)

    def test_stale_queued_command_is_rejected_without_executor_or_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, manifest_path = self.workspace(Path(tmp))
            client = FakeClient([queued_node(requested_at=1_000)])
            executor = RecordingExecutor()
            service = WorkflowDemoService(manifest_path, client=client, executor=executor, clock_ms=lambda: 20_000, sleep=lambda _seconds: None)
            service.poll_once()
            self.assertEqual([], executor.requests)
            self.assertFalse(list((root / "outputs" / "renders").rglob("*.png")))
            flattened = [op for batch in client.applied for op in batch]
            self.assertTrue(any(op.get("metadata", {}).get("workflowDemo", {}).get("status") == "failed" for op in flattened))

    def test_invalid_verb_is_gate_rejected_with_human_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, manifest_path = self.workspace(Path(tmp))
            client = FakeClient([queued_node(content="# request-id: req-001\nlaunch: renders")])
            executor = RecordingExecutor()
            service = WorkflowDemoService(manifest_path, client=client, executor=executor, clock_ms=lambda: 11_000, sleep=lambda _seconds: None)
            service.poll_once()
            self.assertEqual([], executor.requests)
            flattened = [op for batch in client.applied for op in batch]
            errors = [op.get("metadata", {}).get("workflowDemo", {}).get("errorMessage", "") for op in flattened]
            self.assertTrue(any("格式不正确" in message for message in errors))

    def test_non_workflow_nodes_are_never_touched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, manifest_path = self.workspace(Path(tmp))
            text_node = queued_node()
            text_node["type"] = "text"
            client = FakeClient([text_node])
            executor = RecordingExecutor()
            service = WorkflowDemoService(manifest_path, client=client, executor=executor, clock_ms=lambda: 11_000, sleep=lambda _seconds: None)
            service.poll_once()
            self.assertEqual([], executor.requests)
            self.assertEqual([], client.applied)


if __name__ == "__main__":
    unittest.main()
