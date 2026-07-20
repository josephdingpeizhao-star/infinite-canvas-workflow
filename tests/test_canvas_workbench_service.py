from __future__ import annotations

import contextlib
import io
import sys
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

import spike_canvas_push  # noqa: E402
from canvas_workbench_service import CanvasWorkbenchService  # noqa: E402
from executor_contract import ExecutionResult  # noqa: E402
from make_demo_workspace import build_manifest, prepare_workflow_demo  # noqa: E402
from workflow_demo_service import WorkflowDemoService  # noqa: E402


class LoopingService:
    def __init__(self, *, fail: Exception | None = None):
        self.fail = fail
        self.stopping = False
        self.started = threading.Event()
        self.ticks = 0

    def serve_forever(self):
        self.started.set()
        if self.fail:
            raise self.fail
        while not self.stopping:
            self.ticks += 1
            time.sleep(0.005)


class FakeUploadServer:
    bound_port = 17372

    def __init__(self):
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


class FakeProductionHttpServer(FakeUploadServer):
    bound_port = 17373


class DemoClient:
    def __init__(self):
        self.state = {
            "nodes": [
                {
                    "id": "workflow-1",
                    "type": "workflow",
                    "metadata": {
                        "content": "# request-id: req-m1-01\nrun: renders",
                        "workflowDemo": {
                            "status": "queued",
                            "runId": "req-m1-01",
                            "requestedAt": 10_000,
                            "completedRuns": 0,
                        },
                    },
                }
            ],
            "connections": [],
        }
        self.applied: list[list[dict]] = []
        self.running = threading.Event()
        self.completed = threading.Event()

    def call_tool(self, name: str):
        if name != "canvas_get_state":
            raise AssertionError(name)
        return self.state

    def apply_ops(self, ops: list[dict]):
        self.applied.append(ops)
        for op in ops:
            status = op.get("metadata", {}).get("workflowDemo", {}).get("status")
            if status == "running":
                self.running.set()
            if status == "completed":
                self.completed.set()
        return 1


class DemoExecutor:
    name = "workflow-demo"

    def __init__(self):
        self.requests = []
        self.started = threading.Event()
        self.allow_finish = threading.Event()

    def execute(self, request):
        self.requests.append(request)
        self.started.set()
        self.allow_finish.wait(timeout=2.0)
        return ExecutionResult(detail="旧演示完成", provider=self.name)


class CanvasWorkbenchServiceTests(unittest.TestCase):
    def test_m2b_services_and_17373_listener_share_workbench_lifecycle(self) -> None:
        demo = LoopingService()
        intake = LoopingService()
        production = LoopingService()
        style = LoopingService()
        upload = FakeUploadServer()
        production_http = FakeProductionHttpServer()
        workbench = CanvasWorkbenchService(
            demo_service=demo,
            intake_service=intake,
            upload_server=upload,
            production_service=production,
            style_service=style,
            production_http_server=production_http,
        )
        workbench.start()
        for component in (demo, intake, production, style):
            self.assertTrue(component.started.wait(1))
        self.assertTrue(upload.started)
        self.assertTrue(production_http.started)
        self.assertEqual("running", workbench.component_status["workflow_production"])
        self.assertEqual("running", workbench.component_status["style_reference_intake"])

        workbench.stop()
        self.assertTrue(production_http.stopped)
        self.assertTrue(production.stopping)
        self.assertTrue(style.stopping)

    def test_real_m1_demo_service_executes_inside_workbench_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            root.mkdir()
            (root / ".canvas_demo").write_text("safe\n", encoding="utf-8")
            manifest_path = root / "manifests" / "batch_manifest.json"
            manifest_path.parent.mkdir()
            manifest_path.write_text(json.dumps(build_manifest(root), ensure_ascii=False), encoding="utf-8")
            prepare_workflow_demo(root)
            client = DemoClient()
            executor = DemoExecutor()
            demo = WorkflowDemoService(
                manifest_path,
                client=client,
                executor=executor,
                clock_ms=lambda: 11_000,
                interval=0.005,
            )
            intake = LoopingService()
            upload = FakeUploadServer()
            workbench = CanvasWorkbenchService(
                demo_service=demo,
                intake_service=intake,
                upload_server=upload,
            )
            workbench.start()
            try:
                self.assertTrue(executor.started.wait(timeout=1.0))
                self.assertTrue(client.running.wait(timeout=1.0))
                self.assertFalse(client.completed.is_set())
                executor.allow_finish.set()
                self.assertTrue(client.completed.wait(timeout=1.0))
            finally:
                executor.allow_finish.set()
                workbench.stop()

            self.assertEqual(1, len(executor.requests))
            self.assertEqual("renders", executor.requests[0].step)
            statuses = [
                op.get("metadata", {}).get("workflowDemo", {}).get("status")
                for group in client.applied
                for op in group
                if op.get("id") == "workflow-1"
            ]
            self.assertIn("completed", statuses)

    def test_demo_failure_is_isolated_while_intake_and_upload_remain_running(self) -> None:
        demo = LoopingService(fail=RuntimeError("demo exploded with secret payload"))
        intake = LoopingService()
        upload = FakeUploadServer()
        workbench = CanvasWorkbenchService(
            demo_service=demo,
            intake_service=intake,
            upload_server=upload,
            sleep=lambda _seconds: time.sleep(0.005),
        )
        workbench.start()
        self.assertTrue(demo.started.wait(1))
        self.assertTrue(intake.started.wait(1))
        time.sleep(0.03)

        self.assertTrue(upload.started)
        self.assertGreater(intake.ticks, 0)
        self.assertEqual("stopped", workbench.component_status["workflow_demo"])
        self.assertEqual("running", workbench.component_status["batch_intake"])
        self.assertNotIn("secret payload", str(workbench.component_status))

        workbench.stop()
        self.assertTrue(upload.stopped)
        self.assertTrue(demo.stopping)
        self.assertTrue(intake.stopping)

    def test_intake_failure_does_not_stop_existing_m1_demo(self) -> None:
        demo = LoopingService()
        intake = LoopingService(fail=RuntimeError("intake failed"))
        upload = FakeUploadServer()
        workbench = CanvasWorkbenchService(demo_service=demo, intake_service=intake, upload_server=upload)
        workbench.start()
        self.assertTrue(demo.started.wait(1))
        self.assertTrue(intake.started.wait(1))
        time.sleep(0.03)

        self.assertGreater(demo.ticks, 0)
        self.assertEqual("running", workbench.component_status["workflow_demo"])
        self.assertEqual("stopped", workbench.component_status["batch_intake"])
        workbench.stop()

    def test_old_demo_cli_dispatch_stays_on_exact_existing_entry(self) -> None:
        manifest = Path("D:/dev/canvas-demo-workspace/manifests/batch_manifest.json")
        with (
            mock.patch.object(spike_canvas_push.workflow_demo_service, "cmd_serve_workflow_demo") as old_entry,
            mock.patch.object(spike_canvas_push.canvas_workbench_service, "cmd_serve_canvas_workbench") as new_entry,
            mock.patch.object(sys, "argv", ["spike_canvas_push.py", "--serve-workflow-demo", str(manifest), "--interval", "1.25"]),
        ):
            self.assertEqual(0, spike_canvas_push.main())

        old_entry.assert_called_once_with(manifest, 1.25)
        new_entry.assert_not_called()

    def test_new_workbench_cli_dispatches_port_and_isolated_test_root(self) -> None:
        manifest = Path("D:/dev/canvas-demo-workspace/manifests/batch_manifest.json")
        test_root = Path("D:/dev/canvas-intake-test-workspace")
        with (
            mock.patch.object(spike_canvas_push.workflow_demo_service, "cmd_serve_workflow_demo") as old_entry,
            mock.patch.object(spike_canvas_push.canvas_workbench_service, "cmd_serve_canvas_workbench") as new_entry,
            mock.patch.object(
                sys,
                "argv",
                [
                    "spike_canvas_push.py",
                    "--serve-canvas-workbench",
                    str(manifest),
                    "--interval",
                    "0.5",
                    "--batch-intake-test-root",
                    str(test_root),
                ],
            ),
        ):
            self.assertEqual(0, spike_canvas_push.main())

        old_entry.assert_not_called()
        new_entry.assert_called_once_with(
            manifest,
            0.5,
            test_workspace_root=test_root,
        )

    def test_workbench_cli_does_not_expose_a_configurable_upload_port(self) -> None:
        manifest = Path("D:/dev/canvas-demo-workspace/manifests/batch_manifest.json")
        with (
            mock.patch.object(spike_canvas_push.canvas_workbench_service, "cmd_serve_canvas_workbench") as entry,
            mock.patch.object(
                sys,
                "argv",
                [
                    "spike_canvas_push.py",
                    "--serve-canvas-workbench",
                    str(manifest),
                    "--batch-upload-port",
                    "18000",
                ],
            ),
        ):
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    spike_canvas_push.main()
        entry.assert_not_called()


if __name__ == "__main__":
    unittest.main()
