from __future__ import annotations

import contextlib
import errno
import io
import json
import shutil
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT / "canvas-bridge", ROOT / "tests"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import batch_creator  # noqa: E402
import batch_editor  # noqa: E402
import batch_recycle_lock  # noqa: E402
import canvas_readonly_assistant  # noqa: E402
import delivery_cli  # noqa: E402
import qc_repair_cli  # noqa: E402
import spike_canvas_push  # noqa: E402
import workflow_batch_acceptance  # noqa: E402
import workflow_production_service  # noqa: E402
import workflow_style_reference_intake  # noqa: E402
from batch_intake_controller import (  # noqa: E402
    BatchIntakeRequest,
    ConfirmedFacts,
)
from test_workflow_demo_service import (  # noqa: E402
    FakeClient as DemoClient,
    RecordingExecutor,
    queued_node,
)
from test_workflow_production_service import FakeCanvasClient  # noqa: E402
from make_demo_workspace import build_manifest, prepare_workflow_demo  # noqa: E402
from workflow_demo_service import WorkflowDemoService  # noqa: E402


CONFIG_IDS = tuple(
    [f"main_{index:02d}" for index in range(1, 7)]
    + [f"detail_{index:02d}" for index in range(1, 9)]
)


class BatchRecycleGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.manifests = self.repo / "manifests"
        self.manifests.mkdir(parents=True)
        shutil.copytree(ROOT / "categories", self.repo / "categories")
        self.lock_root = self.root / "locks"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _manifest(
        self,
        *,
        workspace: Path | None = None,
        requested_outputs: list[str] | None = None,
    ) -> Path:
        workspace = workspace or (self.root / "missing-workspace")
        manifest = self.manifests / "cup.batch_manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "product_id": "cup",
                    "batch_id": "cup",
                    "requested_outputs": (
                        requested_outputs if requested_outputs is not None else []
                    ),
                    "workspace": {"root": str(workspace)},
                    "inputs": {
                        "style_reference_images": [
                            str(workspace / "inputs" / "style_refs")
                        ]
                    },
                    "drafts": {},
                    "artifacts": {},
                    "outputs": {
                        "renders": [str(workspace / "outputs" / "renders")],
                        "repaired": [str(workspace / "outputs" / "repaired")],
                    },
                }
            ),
            encoding="utf-8",
        )
        return manifest

    def _journal(self, events: list[dict]) -> Path:
        journal = self.manifests / "cup.events.jsonl"
        journal.write_text(
            "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in events),
            encoding="utf-8",
        )
        return journal

    @staticmethod
    def _acceptance_payload() -> dict:
        return {
            "requestId": "accept-001",
            "machineId": "machine",
            "selections": [
                {
                    "configId": config_id,
                    "source": "renders",
                    "sha256": "0" * 64,
                }
                for config_id in CONFIG_IDS
            ],
        }

    def test_production_recycled_gate_precedes_workspace_and_appends_zero_events(self) -> None:
        self._manifest()
        journal = self._journal([{"event": "batch_recycled"}])
        before = journal.read_bytes()
        client = FakeCanvasClient()
        service = workflow_production_service.WorkflowProductionService(
            self.repo,
            client=client,
            clock_ms=lambda: 1_100,
            batch_lock_root=self.lock_root,
        )
        service.poll_once()
        machine = client.state["nodes"][0]
        self.assertIn(
            "已回收",
            machine["metadata"]["workflowProduction"]["errorMessage"],
        )
        self.assertEqual(before, journal.read_bytes())

        unreadable_client = FakeCanvasClient()
        unreadable_service = workflow_production_service.WorkflowProductionService(
            self.repo,
            client=unreadable_client,
            clock_ms=lambda: 1_100,
            batch_lock_root=self.lock_root,
        )
        with mock.patch.object(
            workflow_production_service,
            "read_batch_lifecycle",
            side_effect=workflow_production_service.BatchLifecycleReadError(
                "unavailable"
            ),
        ):
            unreadable_service.poll_once()
        unreadable_machine = unreadable_client.state["nodes"][0]
        self.assertIn(
            "账本暂时无法读取",
            unreadable_machine["metadata"]["workflowProduction"]["errorMessage"],
        )
        self.assertEqual(before, journal.read_bytes())

    def test_production_continues_when_lock_infrastructure_is_unavailable(self) -> None:
        workspace = self.root / "workspace"
        (workspace / "inputs" / "style_refs").mkdir(parents=True)
        (workspace / "inputs" / "style_refs" / "style.jpg").write_bytes(b"style")
        (workspace / "outputs" / "renders").mkdir(parents=True)
        (workspace / "outputs" / "repaired").mkdir(parents=True)
        (workspace / ".canvas_batch").write_text(
            json.dumps({"type": "canvas-batch-v1", "product_id": "cup"}),
            encoding="utf-8",
        )
        self._manifest(
            workspace=workspace,
            requested_outputs=["main", "detail", "final_prompts", "qc_reports"],
        )
        client = FakeCanvasClient()
        route = {
            "current_stage": "ready",
            "blocked_reasons": [],
            "inputs": {"style_reference_images": {"file_count": 1}},
            "outputs": {
                "renders": {"file_count": 0},
                "repaired": {"file_count": 0},
            },
        }
        service = workflow_production_service.WorkflowProductionService(
            self.repo,
            client=client,
            route_reader=lambda _path: route,
            integrity_reader=lambda _route: {},
            artifact_reader=lambda _manifest: (),
            render_artifact_reader=lambda _manifest: (),
            clock_ms=lambda: 1_100,
            batch_lock_root=self.lock_root,
        )
        access_denied = PermissionError(errno.EACCES, "access denied")
        access_denied.winerror = 5
        with mock.patch.object(
            batch_recycle_lock,
            "_lock_one_byte",
            side_effect=access_denied,
        ):
            service.poll_once()
        events = [
            json.loads(line)
            for line in (self.manifests / "cup.events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertIn("command_received", [item["event"] for item in events])

    def test_acceptance_recycled_gate_precedes_workspace_and_appends_zero_events(self) -> None:
        self._manifest()
        journal = self._journal([{"event": "batch_recycled"}])
        before = journal.read_text(encoding="utf-8")
        service = workflow_batch_acceptance.BatchAcceptanceService(
            self.repo,
            batch_lock_root=self.lock_root,
        )
        for payload in ({}, self._acceptance_payload()):
            with self.subTest(payload_kind="invalid" if not payload else "valid"):
                with self.assertRaises(
                    workflow_batch_acceptance.AcceptanceRejected
                ) as ctx:
                    service.close("cup", payload)
                self.assertIn("已回收", str(ctx.exception))
                self.assertEqual(before, journal.read_text(encoding="utf-8"))

    def test_qc_repair_recycled_gate_appends_zero_events(self) -> None:
        manifest = self._manifest()
        journal = self._journal([{"event": "batch_recycled"}])
        before = journal.read_text(encoding="utf-8")
        output = io.StringIO()
        code = qc_repair_cli.run_cli(
            [
                "--batch-manifest",
                str(manifest),
                "--command",
                "invalid",
            ],
            output=output,
            batch_lock_root=self.lock_root,
        )
        self.assertEqual(1, code)
        self.assertIn("已回收", output.getvalue())
        self.assertEqual(before, journal.read_text(encoding="utf-8"))

    def test_qc_repair_closed_gate_appends_zero_events(self) -> None:
        manifest = self._manifest()
        journal = self._journal([{"event": "batch_acceptance_closed"}])
        before = journal.read_text(encoding="utf-8")
        output = io.StringIO()
        code = qc_repair_cli.run_cli(
            [
                "--batch-manifest",
                str(manifest),
                "--command",
                "invalid",
            ],
            output=output,
            batch_lock_root=self.lock_root,
        )
        self.assertEqual(1, code)
        self.assertIn("已关账", output.getvalue())
        self.assertEqual(before, journal.read_text(encoding="utf-8"))

    def test_delivery_recycled_gate_rejects_even_invalid_command_without_append(self) -> None:
        manifest = self._manifest()
        journal = self._journal([{"event": "batch_recycled"}])
        before = journal.read_text(encoding="utf-8")
        output = io.StringIO()
        code = delivery_cli.run_cli(
            [
                "--batch-manifest",
                str(manifest),
                "--command",
                "invalid",
            ],
            output=output,
            batch_lock_root=self.lock_root,
        )
        self.assertEqual(1, code)
        self.assertIn("已回收", output.getvalue())
        self.assertEqual(before, journal.read_text(encoding="utf-8"))

    def test_legacy_daemons_recheck_recycled_state_before_every_projection(self) -> None:
        manifest = self._manifest()
        journal = self._journal([{"event": "batch_recycled"}])
        before = journal.read_text(encoding="utf-8")
        with (
            mock.patch.object(
                batch_recycle_lock,
                "DEFAULT_BATCH_LOCK_ROOT",
                self.lock_root,
            ),
            mock.patch.object(
                spike_canvas_push,
                "build_full_projection",
            ) as projection,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            with self.assertRaises(SystemExit):
                spike_canvas_push.cmd_serve(manifest, 0.01)
        projection.assert_not_called()
        self.assertEqual(before, journal.read_text(encoding="utf-8"))

        journal.write_text("", encoding="utf-8")
        lock_depth = 0

        @contextlib.contextmanager
        def observed_operation(_batch_id: str):
            nonlocal lock_depth
            lock_depth += 1
            try:
                yield True
            finally:
                lock_depth -= 1

        def recycle_while_idle(_seconds: float) -> None:
            self.assertEqual(0, lock_depth)
            journal.write_text(
                json.dumps({"event": "batch_recycled"}) + "\n",
                encoding="utf-8",
            )

        projection_value = (
            "cup",
            {"product_id": "cup"},
            {"current_stage": "ready"},
            {},
            {},
            None,
            None,
            journal,
            [{"type": "create_text", "id": "initial"}],
        )
        startup_calls: list[str] = []

        def build_startup_projection(*_args):
            self.assertEqual(1, lock_depth)
            startup_calls.append("build")
            return projection_value

        def apply_startup_ops(_ops):
            self.assertEqual(1, lock_depth)
            startup_calls.append("apply")

        apply_ops = mock.Mock(side_effect=apply_startup_ops)
        parse_content = mock.Mock()
        mirror_view = mock.Mock()
        with (
            mock.patch.object(
                spike_canvas_push,
                "existing_batch_operation",
                side_effect=observed_operation,
            ),
            mock.patch.object(
                spike_canvas_push,
                "build_full_projection",
                side_effect=build_startup_projection,
            ),
            mock.patch.object(
                spike_canvas_push,
                "build_live_view",
                mirror_view,
            ),
            mock.patch.object(
                spike_canvas_push.executor_factory,
                "build_executor",
                return_value=object(),
            ),
            mock.patch.object(
                spike_canvas_push.ic_client,
                "apply_ops",
                apply_ops,
            ),
            mock.patch.object(
                spike_canvas_push.ic_client,
                "call_tool",
                return_value={
                    "nodes": [
                        {
                            "id": spike_canvas_push.run_controller.run_node_id("cup"),
                            "metadata": {"content": ""},
                        }
                    ]
                },
            ),
            mock.patch.object(
                spike_canvas_push.run_controller,
                "parse_run_content",
                parse_content,
            ),
            mock.patch.object(
                spike_canvas_push.time,
                "sleep",
                side_effect=recycle_while_idle,
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            with self.assertRaises(SystemExit):
                spike_canvas_push.cmd_serve(manifest, 0.01)
        self.assertEqual(0, lock_depth)
        self.assertEqual(["build", "apply"], startup_calls)
        apply_ops.assert_called_once_with(projection_value[-1])
        mirror_view.assert_not_called()
        parse_content.assert_not_called()
        self.assertEqual(
            [{"event": "batch_recycled"}],
            [
                json.loads(line)
                for line in journal.read_text(encoding="utf-8").splitlines()
            ],
        )

        journal.write_text("", encoding="utf-8")
        watch_calls: list[str] = []

        def build_watch_projection(*_args):
            self.assertEqual(1, lock_depth)
            watch_calls.append("build")
            return projection_value

        def apply_watch_ops(_ops):
            self.assertEqual(1, lock_depth)
            watch_calls.append("apply")

        watch_apply = mock.Mock(side_effect=apply_watch_ops)
        watch_tick = mock.Mock()
        with (
            mock.patch.object(
                spike_canvas_push,
                "existing_batch_operation",
                side_effect=observed_operation,
            ),
            mock.patch.object(
                spike_canvas_push,
                "build_full_projection",
                side_effect=build_watch_projection,
            ),
            mock.patch.object(
                spike_canvas_push,
                "build_live_view",
                watch_tick,
            ),
            mock.patch.object(
                spike_canvas_push.ic_client,
                "apply_ops",
                watch_apply,
            ),
            mock.patch.object(
                spike_canvas_push.time,
                "sleep",
                side_effect=recycle_while_idle,
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            with self.assertRaises(SystemExit):
                spike_canvas_push.cmd_watch(manifest, 0.01)
        self.assertEqual(0, lock_depth)
        self.assertEqual(["build", "apply"], watch_calls)
        watch_apply.assert_called_once_with(projection_value[-1])
        watch_tick.assert_not_called()
        self.assertEqual(
            [{"event": "batch_recycled"}],
            [
                json.loads(line)
                for line in journal.read_text(encoding="utf-8").splitlines()
            ],
        )

        journal.write_text("", encoding="utf-8")
        operation_attempts = 0

        @contextlib.contextmanager
        def unavailable_then_available(_batch_id: str):
            nonlocal operation_attempts
            operation_attempts += 1
            yield operation_attempts > 1

        retry_tick = mock.Mock(
            return_value=(
                {},
                {"product_id": "cup"},
                {"current_stage": "ready"},
                {},
            )
        )
        retry_apply = mock.Mock()
        with (
            mock.patch.object(
                spike_canvas_push,
                "existing_batch_operation",
                side_effect=unavailable_then_available,
            ),
            mock.patch.object(
                spike_canvas_push,
                "build_full_projection",
                return_value=projection_value,
            ),
            mock.patch.object(
                spike_canvas_push,
                "build_live_view",
                retry_tick,
            ),
            mock.patch.object(
                spike_canvas_push.projector,
                "runtime_update_ops",
                return_value=[],
            ),
            mock.patch.object(
                spike_canvas_push.ic_client,
                "apply_ops",
                retry_apply,
            ),
            mock.patch.object(
                spike_canvas_push.time,
                "sleep",
                side_effect=[None, KeyboardInterrupt()],
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            spike_canvas_push.cmd_watch(manifest, 0.01)
        self.assertEqual(2, operation_attempts)
        retry_tick.assert_called_once_with(manifest)
        retry_apply.assert_called_once_with(projection_value[-1])

    def test_batch_editor_recycled_gate_keeps_manifest_byte_identical(self) -> None:
        manifest = self._manifest()
        self._journal([{"event": "batch_recycled"}])
        before = manifest.read_bytes()
        with self.assertRaises(batch_editor.EditValidationError) as ctx:
            batch_editor.apply_edits(
                manifest,
                {"notes": "changed"},
                batch_lock_root=self.lock_root,
            )
        self.assertIn("已回收", str(ctx.exception))
        self.assertEqual(before, manifest.read_bytes())

    def test_style_publish_recycled_gate_is_human_and_writes_no_workspace(self) -> None:
        manifest = self._manifest()
        self._journal([{"event": "batch_recycled"}])
        with self.assertRaises(
            workflow_style_reference_intake.StyleReferenceIntakeError
        ) as ctx:
            workflow_style_reference_intake.publish_style_references(
                manifest,
                "request-001",
                (),
                batch_lock_root=self.lock_root,
            )
        self.assertIn("已回收", str(ctx.exception))
        self.assertFalse((self.root / "missing-workspace").exists())

    def test_readonly_assistant_reports_recycled_without_opening_workspace(self) -> None:
        self._manifest()
        self._journal(
            [
                {
                    "event": "batch_recycled",
                    "workspace_source": "sensitive-source",
                    "workspace_target": "sensitive-target",
                }
            ]
        )
        context = canvas_readonly_assistant.ReadonlyContextAssembler(
            self.repo
        ).assemble("cup 现在什么状态？")
        detail = context["batch_detail"]
        self.assertEqual("recycled", detail["lifecycle_status"])
        self.assertFalse(detail["qc"]["available"])
        self.assertNotIn("workspace_source", json.dumps(detail, ensure_ascii=False))

    def test_manifest_only_duplicate_keeps_batch_id_permanently_unavailable(self) -> None:
        test_root = self.root / "test-root"
        test_root.mkdir()
        (test_root / ".canvas_intake_test_root").write_text(
            "canvas-intake-test-root-v1\n",
            encoding="utf-8",
        )
        state_root = self.root / "state"
        batch_creator.prepare_state_root(state_root)
        expected_manifest = self.manifests / "餐具_20260726.batch_manifest.json"
        expected_manifest.write_bytes(b'{"audit":"keep"}\n')
        creator = batch_creator.BatchCreator(
            self.repo,
            state_root,
            test_root=test_root,
            today=lambda: date(2026, 7, 26),
        )
        facts = ConfirmedFacts(
            product_type="餐具",
            height_cm=None,
            main_image_count=6,
            detail_image_count=8,
            handheld_main=0,
            handheld_detail=0,
            allow_clear_water=False,
            forbid_pouring_and_heating=True,
            missing_d_no_retake=True,
        )
        request = BatchIntakeRequest(
            request_id="request-001",
            requested_at=1,
            info_node_id="card",
            workflow_node_id="machine",
            facts=facts,
            source_images=(),
        )
        with self.assertRaises(batch_creator.BatchCreationError) as ctx:
            creator.create(request, ())
        self.assertEqual("batch_exists", ctx.exception.code)
        self.assertEqual(b'{"audit":"keep"}\n', expected_manifest.read_bytes())
        self.assertFalse((test_root / "餐具_20260726").exists())

    def test_demo_service_remains_marker_isolated_and_unchanged(self) -> None:
        demo_root = self.root / "demo"
        demo_root.mkdir()
        (demo_root / ".canvas_demo").write_text("safe\n", encoding="utf-8")
        manifest = demo_root / "manifests" / "batch_manifest.json"
        manifest.parent.mkdir()
        manifest.write_text(
            json.dumps(build_manifest(demo_root), ensure_ascii=False),
            encoding="utf-8",
        )
        prepare_workflow_demo(demo_root)
        client = DemoClient([queued_node()])
        executor = RecordingExecutor()
        service = WorkflowDemoService(
            manifest,
            client=client,
            executor=executor,
            clock_ms=lambda: 11_000,
            sleep=lambda _seconds: None,
        )
        service.poll_once()
        self.assertEqual(1, len(executor.requests))
        events = (
            demo_root / "manifests" / "workflow_demo_service.events.jsonl"
        ).read_text(encoding="utf-8")
        self.assertIn("command_received", events)


if __name__ == "__main__":
    unittest.main()
