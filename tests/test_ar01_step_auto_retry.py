from __future__ import annotations

import copy
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

from executor_contract import ExecutionResult, ExecutorExecutionError  # noqa: E402
import workflow_production_service as production_service  # noqa: E402


STEP_ROUTES = {
    "identity": ("needs_product_identity_archive", "product-identity-archive"),
    "style_master": ("needs_style_master", "style-master-extractor"),
    "angle_inventory": ("needs_angle_inventory", "angle-inventory"),
    "main_vc": ("needs_main_variable_configs", "main-variable-config"),
    "detail_vc": ("needs_detail_variable_configs", "detail-variable-config"),
    "final_prompts": ("needs_final_prompts", "final-prompt-compiler"),
}
FIXED_EVENT_TIME = "2026-08-03T12:00:00"


class FakeCanvasClient:
    def __init__(self, command: str):
        self.state = {
            "nodes": [
                {
                    "id": "machine",
                    "type": "workflow",
                    "position": {"x": 0, "y": 0},
                    "width": 420,
                    "height": 300,
                    "metadata": {
                        "content": f"# workflow-production\n# request-id: req-001\n{command}",
                        "workflowProduction": {
                            "status": "queued",
                            "requestId": "req-001",
                            "batchId": "cup",
                            "requestedAt": 1_000,
                            "producedCount": 0,
                        },
                    },
                },
                {
                    "id": "card",
                    "type": "batch-info",
                    "metadata": {
                        "batchIntake": {
                            "status": "completed",
                            "receipt": {"batchId": "cup", "imageCount": 2},
                        }
                    },
                },
                {
                    "id": "original",
                    "type": "image",
                    "metadata": {
                        "content": "blob:original",
                        "storageKey": "image:original",
                    },
                },
            ],
            "connections": [
                {"id": "card-machine", "fromNodeId": "card", "toNodeId": "machine"},
                {"id": "image-machine", "fromNodeId": "original", "toNodeId": "machine"},
            ],
        }
        self.ops: list[list[dict]] = []

    def call_tool(self, name: str):
        if name != "canvas_get_state":
            raise AssertionError(name)
        return self.state

    def apply_ops(self, ops: list[dict]):
        self.ops.append(copy.deepcopy(ops))
        for op in ops:
            if op.get("type") != "update_node":
                continue
            node = next(item for item in self.state["nodes"] if item["id"] == op["id"])
            node["metadata"] = {
                **node.get("metadata", {}),
                **op.get("metadata", {}),
            }
        return len(ops)


class AttemptExecutor:
    name = "fake-step-attempt"

    def __init__(self, scenario: "Scenario", step: str, should_fail: bool):
        self.scenario = scenario
        self.step = step
        self.should_fail = should_fail
        self.content_correction_callback = None
        self.turn_progress_callback = None

    def set_content_correction_callback(self, callback) -> None:
        self.content_correction_callback = callback

    def set_turn_progress_callback(self, callback) -> None:
        self.turn_progress_callback = callback

    def execute(self, request):
        if request.step != self.step:
            raise AssertionError((request.step, self.step))
        if self.should_fail:
            if self.scenario.stop_on_failure:
                self.scenario.service.stopping = True
            if self.step == "integrity":
                self.scenario.integrity_report_path.parent.mkdir(parents=True, exist_ok=True)
                self.scenario.integrity_report_path.write_text(
                    json.dumps(
                        {
                            "status": "fail",
                            "render_blocked": True,
                            "blocking_issue_count": 7,
                        }
                    ),
                    encoding="utf-8",
                )
            failure = ExecutorExecutionError(self.scenario.failure_message)
            if self.scenario.failure_code is not None:
                failure.code = self.scenario.failure_code
            raise failure
        self.scenario.completed.append(self.step)
        return ExecutionResult(detail=f"{self.step} ok", provider=self.name)


class Scenario:
    def __init__(
        self,
        steps: tuple[str, ...],
        failures: dict[str, tuple[bool, ...]],
        integrity_report_path: Path,
        *,
        failure_message: str = "temporary model format failure",
        failure_code: str | None = None,
        builder_failure_count: int = 0,
        stop_on_failure: bool = False,
    ) -> None:
        self.steps = steps
        self.failures = failures
        self.integrity_report_path = integrity_report_path
        self.failure_message = failure_message
        self.failure_code = failure_code
        self.builder_failure_count = builder_failure_count
        self.stop_on_failure = stop_on_failure
        self.completed: list[str] = []
        self.executors: list[AttemptExecutor] = []
        self.build_count_by_step: dict[str, int] = {}
        self.service: production_service.WorkflowProductionService

    def build(self, step, _manifest, _path, _on_output):
        index = self.build_count_by_step.get(step, 0)
        self.build_count_by_step[step] = index + 1
        if index < self.builder_failure_count:
            failure = ExecutorExecutionError(self.failure_message)
            if self.failure_code is not None:
                failure.code = self.failure_code
            raise failure
        outcomes = self.failures.get(step, ())
        should_fail = outcomes[index] if index < len(outcomes) else False
        executor = AttemptExecutor(self, step, should_fail)
        self.executors.append(executor)
        return executor

    def route(self, _path):
        pending = next((step for step in self.steps if step not in self.completed), None)
        if pending is None:
            return {
                "current_stage": "ready",
                "next_required_skill": None,
                "blocked_reasons": [],
                "available_artifacts": [],
                "outputs": {
                    "renders": {"file_count": 0},
                    "repaired": {"file_count": 0},
                },
                "inputs": {"style_reference_images": {"file_count": 1}},
            }
        if pending in {"integrity", "renders"}:
            return {
                "current_stage": "needs_generated_images_before_qc",
                "next_required_skill": None,
                "blocked_reasons": ["QC is post-generation only"],
                "available_artifacts": ["final_prompts"],
                "outputs": {
                    "renders": {"file_count": 0},
                    "repaired": {"file_count": 0},
                },
                "inputs": {"style_reference_images": {"file_count": 1}},
            }
        stage, skill = STEP_ROUTES[pending]
        return {
            "current_stage": stage,
            "next_required_skill": skill,
            "blocked_reasons": [],
            "available_artifacts": [],
            "outputs": {
                "renders": {"file_count": 0},
                "repaired": {"file_count": 0},
            },
            "inputs": {"style_reference_images": {"file_count": 1}},
        }

    def integrity(self, _route):
        if self.integrity_report_path.is_file():
            return {
                "found": True,
                "path": str(self.integrity_report_path),
                "status": "fail",
                "render_blocked": True,
            }
        if self.steps and self.steps[0] == "renders":
            return {"found": True, "path": "", "status": "pass", "render_blocked": False}
        return {"found": False, "path": "", "status": "", "render_blocked": False}


class StepAutoRetryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.case_index = 0

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _run_case(
        self,
        steps: tuple[str, ...],
        failures: dict[str, tuple[bool, ...]],
        *,
        retry_limit: int = 2,
        failure_message: str = "temporary model format failure",
        failure_code: str | None = None,
        builder_failure_count: int = 0,
        stop_on_failure: bool = False,
    ) -> dict[str, object]:
        self.case_index += 1
        root = self.root / f"case-{self.case_index}"
        repository_root = root / "repo"
        workspace = root / "workspace"
        (repository_root / "manifests").mkdir(parents=True)
        shutil.copytree(ROOT / "categories", repository_root / "categories")
        (workspace / "inputs" / "style_refs").mkdir(parents=True)
        (workspace / "inputs" / "style_refs" / "style.jpg").write_bytes(b"style")
        (workspace / "outputs" / "renders").mkdir(parents=True)
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
        report_path = (
            workspace
            / "artifacts"
            / "qc_reports"
            / "final_prompt_integrity_report.json"
        )
        scenario = Scenario(
            steps,
            failures,
            report_path,
            failure_message=failure_message,
            failure_code=failure_code,
            builder_failure_count=builder_failure_count,
            stop_on_failure=stop_on_failure,
        )
        command = "run: renders" if steps and steps[0] == "renders" else "run: next"
        client = FakeCanvasClient(command)
        diagnostics: list[tuple[str, str]] = []
        service = production_service.WorkflowProductionService(
            repository_root,
            client=client,
            executor_builder=scenario.build,
            route_reader=scenario.route,
            integrity_reader=scenario.integrity,
            artifact_reader=lambda _manifest: (),
            clock_ms=lambda: 1_100,
            environment={"RENDER_ALLOW_REAL_EXECUTION": "1"},
            diagnostic_recorder=lambda step, code: diagnostics.append((step, code)),
            batch_lock_root=root / "locks",
            step_auto_retry_limit=retry_limit,
        )
        scenario.service = service
        poll_error = None
        with mock.patch.object(
            production_service.run_controller.time,
            "strftime",
            return_value=FIXED_EVENT_TIME,
        ):
            try:
                service.poll_once()
            except ExecutorExecutionError as exc:
                if not stop_on_failure:
                    raise
                poll_error = exc
        journal_path = repository_root / "manifests" / "cup.events.jsonl"
        events = [
            json.loads(line)
            for line in journal_path.read_text(encoding="utf-8").splitlines()
        ]
        return {
            "scenario": scenario,
            "client": client,
            "events": events,
            "diagnostics": diagnostics,
            "poll_error": poll_error,
            "production": copy.deepcopy(
                client.state["nodes"][0]["metadata"]["workflowProduction"]
            ),
        }

    @staticmethod
    def _events(result: dict[str, object], event_type: str) -> list[dict]:
        return [
            event
            for event in result["events"]
            if event.get("event") == event_type
        ]

    def test_retryable_step_rebuilds_executor_and_rebinds_callbacks(self) -> None:
        result = self._run_case(("main_vc",), {"main_vc": (True, False)})
        scenario = result["scenario"]

        self.assertEqual(2, scenario.build_count_by_step["main_vc"])
        self.assertIsNot(scenario.executors[0], scenario.executors[1])
        self.assertIsNotNone(scenario.executors[1].content_correction_callback)
        self.assertIsNotNone(scenario.executors[1].turn_progress_callback)
        retries = self._events(result, "step_auto_retry")
        self.assertEqual(1, len(retries))
        self.assertEqual(
            {
                "ts",
                "event",
                "request_id",
                "step",
                "attempt",
                "detail",
            },
            set(retries[0]),
        )
        self.assertEqual("req-001", retries[0]["request_id"])
        self.assertEqual("main_vc", retries[0]["step"])
        self.assertEqual(1, retries[0]["attempt"])
        self.assertEqual("temporary model format failure", retries[0]["detail"])
        self.assertEqual(1, len(self._events(result, "step_succeeded")))
        self.assertEqual([], self._events(result, "step_failed"))

    def test_builder_failure_retries_without_execution_diagnostic(self) -> None:
        result = self._run_case(
            ("identity",),
            {"identity": (False,)},
            failure_code="empty_assistant_response",
            builder_failure_count=1,
        )

        scenario = result["scenario"]
        self.assertEqual(2, scenario.build_count_by_step["identity"])
        self.assertEqual(1, len(scenario.executors))
        retries = self._events(result, "step_auto_retry")
        self.assertEqual(1, len(retries))
        self.assertEqual(1, retries[0]["attempt"])
        self.assertEqual([], result["diagnostics"])
        self.assertEqual(1, len(self._events(result, "step_succeeded")))
        self.assertEqual([], self._events(result, "step_failed"))

    def test_retry_budget_exhaustion_preserves_terminal_failure_path(self) -> None:
        retried = self._run_case(
            ("identity",),
            {"identity": (True, True, True)},
            failure_code="empty_assistant_response",
        )
        no_retry = self._run_case(
            ("identity",),
            {"identity": (True,)},
            retry_limit=0,
            failure_code="empty_assistant_response",
        )

        self.assertEqual([1, 2], [event["attempt"] for event in self._events(retried, "step_auto_retry")])
        self.assertEqual(3, retried["scenario"].build_count_by_step["identity"])
        self.assertEqual(1, no_retry["scenario"].build_count_by_step["identity"])
        self.assertEqual(
            [("identity", "empty_assistant_response")] * 3,
            retried["diagnostics"],
        )
        self.assertEqual(
            [("identity", "empty_assistant_response")],
            no_retry["diagnostics"],
        )
        self.assertEqual(
            self._events(no_retry, "step_failed"),
            self._events(retried, "step_failed"),
        )
        self.assertEqual(no_retry["production"], retried["production"])

    def test_renders_failure_never_retries(self) -> None:
        result = self._run_case(("renders",), {"renders": (True,)})

        self.assertEqual(1, result["scenario"].build_count_by_step["renders"])
        self.assertEqual([], self._events(result, "step_auto_retry"))
        self.assertEqual(1, len(self._events(result, "step_failed")))

    def test_integrity_failure_never_retries_and_keeps_enrichment(self) -> None:
        result = self._run_case(("integrity",), {"integrity": (True,)})

        self.assertEqual(1, result["scenario"].build_count_by_step["integrity"])
        self.assertEqual([], self._events(result, "step_auto_retry"))
        failed = self._events(result, "step_failed")
        self.assertEqual(1, len(failed))
        self.assertEqual(
            "完整性检查未通过：7 项阻塞，报告已写入 reports",
            failed[0]["detail"],
        )
        self.assertEqual(
            "完整性检查未通过：7 项阻塞，报告已写入 reports。机器已停下，未自动重试。",
            result["production"]["errorMessage"],
        )

    def test_retry_budget_is_independent_for_each_step(self) -> None:
        result = self._run_case(
            ("identity", "style_master"),
            {
                "identity": (True, True, False),
                "style_master": (True, True, False),
            },
        )
        scenario = result["scenario"]

        self.assertEqual({"identity": 3, "style_master": 3}, scenario.build_count_by_step)
        retries_by_step = {
            step: [
                event["attempt"]
                for event in self._events(result, "step_auto_retry")
                if event["step"] == step
            ]
            for step in ("identity", "style_master")
        }
        self.assertEqual(
            {"identity": [1, 2], "style_master": [1, 2]},
            retries_by_step,
        )
        self.assertEqual(2, len(self._events(result, "step_succeeded")))
        self.assertEqual([], self._events(result, "step_failed"))

    def test_stopping_prevents_a_new_attempt(self) -> None:
        result = self._run_case(
            ("identity",),
            {"identity": (True,)},
            stop_on_failure=True,
        )

        self.assertEqual(1, result["scenario"].build_count_by_step["identity"])
        self.assertEqual([], self._events(result, "step_auto_retry"))
        self.assertEqual(1, len(self._events(result, "step_failed")))
        self.assertEqual("真实工作流服务已停止", str(result["poll_error"]))

    def test_retry_detail_uses_safe_event_redaction(self) -> None:
        unsafe = r"temporary failure at C:\private\reply.json https://example.test/secret"
        result = self._run_case(
            ("identity",),
            {"identity": (True, False)},
            failure_message=unsafe,
        )

        retries = self._events(result, "step_auto_retry")
        self.assertEqual(1, len(retries))
        self.assertEqual("执行已停止，未自动重试", retries[0]["detail"])
        self.assertNotIn("private", retries[0]["detail"])
        self.assertNotIn("https", retries[0]["detail"])

    def test_retry_limit_zero_and_invalid_values_fail_closed(self) -> None:
        result = self._run_case(
            ("identity",),
            {"identity": (True,)},
            retry_limit=0,
        )
        self.assertEqual(1, result["scenario"].build_count_by_step["identity"])
        self.assertEqual([], self._events(result, "step_auto_retry"))

        for invalid in (-1, 3, True, False, 1.5, "2"):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(
                    ValueError,
                    "step_auto_retry_limit must be an integer between 0 and 2",
                ):
                    production_service.WorkflowProductionService(
                        self.root,
                        step_auto_retry_limit=invalid,
                    )


if __name__ == "__main__":
    unittest.main()
