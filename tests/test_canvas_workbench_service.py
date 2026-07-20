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
from canvas_workbench_service import (  # noqa: E402
    CRITICAL_COMPONENTS,
    ISOLATED_COMPONENTS,
    WORKBENCH_EVENT_NAME,
    CanvasWorkbenchService,
    CriticalWorkerStopped,
    WorkbenchEventLedger,
)
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

    def set_health_provider(self, provider):
        self.health_provider = provider


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
    def test_worker_classification_names_three_critical_workers_and_keeps_demo_isolated(self) -> None:
        self.assertEqual(
            {"batch_intake", "workflow_production", "style_reference_intake"},
            CRITICAL_COMPONENTS,
        )
        self.assertEqual({"workflow_demo"}, ISOLATED_COMPONENTS)
        self.assertFalse(CRITICAL_COMPONENTS & ISOLATED_COMPONENTS)

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

    def test_critical_intake_failure_stops_the_whole_workbench_and_exits_nonzero(self) -> None:
        demo = LoopingService()
        intake = LoopingService(fail=RuntimeError("intake failed"))
        upload = FakeUploadServer()
        workbench = CanvasWorkbenchService(
            demo_service=demo,
            intake_service=intake,
            upload_server=upload,
            sleep=lambda _seconds: time.sleep(0.005),
        )
        failures: list[BaseException] = []

        def run() -> None:
            try:
                workbench.serve_forever()
            except BaseException as exc:
                failures.append(exc)

        supervisor = threading.Thread(target=run)
        supervisor.start()
        self.assertTrue(demo.started.wait(1))
        self.assertTrue(intake.started.wait(1))
        supervisor.join(timeout=1)

        self.assertGreater(demo.ticks, 0)
        self.assertFalse(supervisor.is_alive())
        self.assertEqual(1, len(failures))
        self.assertIsInstance(failures[0], CriticalWorkerStopped)
        self.assertTrue(workbench.stopping)
        self.assertTrue(upload.stopped)
        self.assertTrue(demo.stopping)
        self.assertEqual("stopped", workbench.component_status["workflow_demo"])
        self.assertEqual("stopped", workbench.component_status["batch_intake"])

    def test_health_snapshot_and_append_only_event_ledger_expose_only_status_and_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            marker = state_root / ".canvas_batch_intake_state"
            marker.write_text("canvas-batch-intake-state-v1\n", encoding="utf-8")
            ledger = WorkbenchEventLedger(state_root, clock_ms=lambda: 2_000)
            workbench = CanvasWorkbenchService(
                demo_service=LoopingService(),
                intake_service=LoopingService(),
                upload_server=FakeUploadServer(),
                production_service=LoopingService(),
                style_service=LoopingService(),
                production_http_server=FakeProductionHttpServer(),
                clock_ms=lambda: 1_000,
                event_ledger=ledger,
            )
            workbench.start()
            try:
                healthy, workers = workbench.health_snapshot()
                self.assertTrue(healthy)
                self.assertEqual(
                    {"status", "lastStatusAt"},
                    set(workers["style_reference_intake"]),
                )
                self.assertEqual("running", workers["style_reference_intake"]["status"])
                self.assertEqual(1_000, workers["style_reference_intake"]["lastStatusAt"])
            finally:
                workbench.stop()

            event_path = state_root / WORKBENCH_EVENT_NAME
            entries = [json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines()]
            self.assertGreaterEqual(len(entries), 8)
            for entry in entries:
                self.assertEqual(
                    {"event", "worker", "status", "recorded_at"},
                    set(entry),
                )
            ledger.record_execution_failure("workflow_production", "identity", "empty_assistant_response")
            failure = json.loads(event_path.read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(
                {
                    "event": "execution_failure",
                    "worker": "workflow_production",
                    "step": "identity",
                    "code": "empty_assistant_response",
                    "recorded_at": 2_000,
                },
                failure,
            )
            rejected_before = event_path.read_bytes()
            with self.assertRaises(ValueError):
                ledger.record_execution_failure("workflow_production", "identity", "PRIVATE_PATH_TOKEN")
            self.assertEqual(rejected_before, event_path.read_bytes())
            before = event_path.read_bytes()
            marker.write_text("wrong\n", encoding="utf-8")
            with self.assertRaises(Exception):
                ledger.record("batch_intake", "running")
            self.assertEqual(before, event_path.read_bytes())

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
