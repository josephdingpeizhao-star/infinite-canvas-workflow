from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT / "scripts", ROOT / "canvas-bridge"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from codex_dev_executor import (  # noqa: E402
    CanvasAgentCodexTransport,
    CanvasAgentTransportError,
    CodexDevExecutor,
)
from executor_contract import ExecutorContext, ExecutorExecutionError  # noqa: E402
from tests.test_workflow_production_service import FakeCanvasClient  # noqa: E402
import workflow_production_service as production_service  # noqa: E402


class _UnexpectedRunFailureTransport:
    def run_turn(self, prompt, attachments):
        raise RuntimeError("worker exited unexpectedly")

    def continue_turn(self, thread_id, prompt, attachments):
        raise AssertionError("continue_turn should not be called")


class _UnexpectedContinueFailureTransport:
    def run_turn(self, prompt, attachments):
        raise AssertionError("run_turn should not be called")

    def continue_turn(self, thread_id, prompt, attachments):
        raise ValueError(r"C:\private\job.json token=super-secret")


class _JournalFailureExecutor:
    name = "hf04-observability"

    def __init__(self, executed: list[str], repository_root: Path):
        self.executed = executed
        self.delegate = CodexDevExecutor(
            ExecutorContext(manifest={}),
            transport=_UnexpectedRunFailureTransport(),
            repository_root=repository_root,
        )

    def execute(self, request):
        self.executed.append(request.step)
        return self.delegate._run_transport("prompt", ())


class Hf04TransportObservabilityTest(unittest.TestCase):
    def test_run_turn_preserves_unexpected_exception_type_and_safe_summary(self) -> None:
        transport = CanvasAgentCodexTransport(config={})
        with mock.patch.object(
            transport,
            "_run_turn",
            side_effect=RuntimeError("worker exited unexpectedly"),
        ):
            with self.assertRaises(CanvasAgentTransportError) as caught:
                transport.run_turn("prompt", ())

        error = caught.exception
        self.assertEqual("thread", error.code)
        self.assertIn("RuntimeError", error.safe_detail)
        self.assertIn("worker exited unexpectedly", error.safe_detail)
        self.assertLessEqual(len(error.safe_detail), 160)
        self.assertNotIn("\n", error.safe_detail)
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)

    def test_run_turn_redacts_sk_key_before_executor_error_boundary(self) -> None:
        transport = CanvasAgentCodexTransport(config={})
        private_key = "sk-ABC123_PRIVATE_VALUE"
        with tempfile.TemporaryDirectory() as tmp:
            executor = CodexDevExecutor(
                ExecutorContext(manifest={}),
                transport=transport,
                repository_root=Path(tmp),
            )
            with mock.patch.object(
                transport,
                "_run_turn",
                side_effect=RuntimeError(private_key),
            ):
                with self.assertRaises(ExecutorExecutionError) as caught:
                    executor._run_transport("prompt", ())

        error = caught.exception
        detail = str(error)
        self.assertIn("RuntimeError", detail)
        self.assertIn("异常摘要已脱敏", detail)
        self.assertNotIn(private_key, detail)
        self.assertNotIn("sk-ABC123", detail)
        self.assertLessEqual(len(detail), 160)
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)

    def test_continue_turn_replaces_sensitive_summary_but_keeps_exception_type(self) -> None:
        transport = CanvasAgentCodexTransport(config={})
        private_path = r"C:\private\job.json"
        with mock.patch.object(
            transport,
            "_continue_turn",
            side_effect=ValueError(f"{private_path} token=super-secret"),
        ):
            with self.assertRaises(CanvasAgentTransportError) as caught:
                transport.continue_turn("thread-1", "prompt", ())

        detail = caught.exception.safe_detail
        self.assertIn("ValueError", detail)
        self.assertIn("异常摘要已脱敏", detail)
        self.assertNotIn(private_path, detail)
        self.assertNotIn("super-secret", detail)
        self.assertNotIn("token", detail.lower())
        self.assertLessEqual(len(detail), 160)
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)

    def test_executor_propagates_unknown_transport_run_failure_without_chain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            executor = CodexDevExecutor(
                ExecutorContext(manifest={}),
                transport=_UnexpectedRunFailureTransport(),
                repository_root=Path(tmp),
            )

            with self.assertRaises(ExecutorExecutionError) as caught:
                executor._run_transport("prompt", ())

        detail = str(caught.exception)
        self.assertIn("RuntimeError", detail)
        self.assertIn("worker exited unexpectedly", detail)
        self.assertLessEqual(len(detail), 160)
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)

    def test_executor_propagates_unknown_continue_failure_as_redacted_detail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            executor = CodexDevExecutor(
                ExecutorContext(manifest={}),
                transport=_UnexpectedContinueFailureTransport(),
                repository_root=Path(tmp),
            )

            with self.assertRaises(ExecutorExecutionError) as caught:
                executor._continue_transport("thread-1", "prompt", ())

        detail = str(caught.exception)
        self.assertIn("ValueError", detail)
        self.assertIn("异常摘要已脱敏", detail)
        self.assertNotIn(r"C:\private\job.json", detail)
        self.assertNotIn("super-secret", detail)
        self.assertNotIn("token", detail.lower())
        self.assertLessEqual(len(detail), 160)
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)

    def test_private_detail_remains_ignored_and_safe_detail_is_revalidated(self) -> None:
        safe_error = CanvasAgentTransportError(
            "thread",
            "token=private-secret",
            safe_detail="RuntimeError: worker stopped",
        )
        self.assertEqual("RuntimeError: worker stopped", safe_error.safe_detail)
        self.assertNotIn("private-secret", safe_error.safe_detail)

        redacted_error = CanvasAgentTransportError(
            "thread",
            safe_detail=r"ValueError: C:\private\job.json token=super-secret",
        )
        self.assertEqual("异常摘要已脱敏", redacted_error.safe_detail)

    def test_production_journal_records_safe_cause_once_across_two_polls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository_root = root / "repo"
            workspace = root / "workspace"
            (repository_root / "manifests").mkdir(parents=True)
            shutil.copytree(ROOT / "categories", repository_root / "categories")
            (workspace / "inputs" / "style_refs").mkdir(parents=True)
            (workspace / "inputs" / "style_refs" / "style.jpg").write_bytes(b"style")
            (workspace / "outputs" / "renders").mkdir(parents=True)
            (workspace / ".canvas_demo").write_text("safe\n", encoding="utf-8")
            (workspace / ".canvas_batch").write_text(
                json.dumps({"type": "canvas-batch-v1", "product_id": "cup"}),
                encoding="utf-8",
            )
            manifest_path = repository_root / "manifests" / "cup.batch_manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "product_id": "cup",
                        "requested_outputs": [],
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
                            "repaired": [],
                        },
                    }
                ),
                encoding="utf-8",
            )
            client = FakeCanvasClient()
            executed: list[str] = []
            route = {
                "current_stage": "needs_product_identity_archive",
                "next_required_skill": "product-identity-archive",
                "blocked_reasons": [],
                "available_artifacts": [],
                "outputs": {
                    "renders": {"file_count": 0},
                    "repaired": {"file_count": 0},
                },
                "inputs": {"style_reference_images": {"file_count": 1}},
            }
            service = production_service.WorkflowProductionService(
                repository_root,
                client=client,
                executor_builder=lambda _step, _manifest, _path, _on_output: (
                    _JournalFailureExecutor(executed, repository_root)
                ),
                route_reader=lambda _path: route,
                artifact_reader=lambda _manifest: (),
                clock_ms=lambda: 1_100,
            )

            service.poll_once()
            service.poll_once()

            self.assertEqual(["identity"], executed)
            journal_path = repository_root / "manifests" / "cup.events.jsonl"
            events = [
                json.loads(line)
                for line in journal_path.read_text(encoding="utf-8").splitlines()
            ]
            failed_events = [
                event for event in events if event.get("event") == "step_failed"
            ]
            self.assertEqual(1, len(failed_events))
            detail = failed_events[0]["detail"]
            self.assertIn("RuntimeError", detail)
            self.assertIn("worker exited unexpectedly", detail)
            self.assertLessEqual(len(detail), 160)
            machine_state = client.state["nodes"][0]["metadata"]["workflowProduction"]
            self.assertEqual("failed", machine_state["status"])
            self.assertEqual(
                "这一步没做好，机器已停下。已经完成的成果都保留了。",
                machine_state["errorMessage"],
            )
            self.assertNotIn("RuntimeError", machine_state["errorMessage"])
            self.assertNotIn("worker exited unexpectedly", machine_state["errorMessage"])


if __name__ == "__main__":
    unittest.main()
