from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT / "scripts", ROOT / "canvas-bridge"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import run_controller  # noqa: E402
from demo_executor import DemoWorkspaceExecutor  # noqa: E402
from executor_contract import ExecutionRequest, ExecutionResult  # noqa: E402
from executor_factory import build_executor  # noqa: E402
from executor_registry import UnknownExecutorError  # noqa: E402


def make_route(
    *,
    stage: str = "ready",
    next_skill: str | None = None,
    blocked: list[str] | None = None,
    available: list[str] | None = None,
    renders: int = 0,
) -> dict:
    return {
        "current_stage": stage,
        "next_required_skill": next_skill,
        "blocked_reasons": blocked or [],
        "available_artifacts": available or [],
        "outputs": {"renders": {"file_count": renders}, "repaired": {"file_count": 0}},
    }


NO_INTEGRITY = {"found": False, "status": "", "render_blocked": False}
PASS_INTEGRITY = {"found": True, "status": "pass", "render_blocked": False}
FAIL_INTEGRITY = {"found": True, "status": "fail", "render_blocked": True}


class ParseRunContentTest(unittest.TestCase):
    def test_template_without_command_is_none(self) -> None:
        text = run_controller.render_run_content(make_route(), NO_INTEGRITY)
        self.assertIsNone(run_controller.parse_run_content(text))

    def test_run_next(self) -> None:
        self.assertEqual(("run", "next"), run_controller.parse_run_content("# 注释\nrun: next"))

    def test_retry_step_with_chinese_colon(self) -> None:
        self.assertEqual(("retry", "identity"), run_controller.parse_run_content("retry：identity"))

    def test_unknown_verb_rejected(self) -> None:
        with self.assertRaises(run_controller.RunValidationError) as ctx:
            run_controller.parse_run_content("launch: next")
        self.assertIn("launch", str(ctx.exception))

    def test_two_commands_rejected(self) -> None:
        with self.assertRaises(run_controller.RunValidationError):
            run_controller.parse_run_content("run: next\nretry: identity")

    def test_missing_target_rejected(self) -> None:
        with self.assertRaises(run_controller.RunValidationError):
            run_controller.parse_run_content("run:")

    def test_line_without_colon_rejected(self) -> None:
        with self.assertRaises(run_controller.RunValidationError):
            run_controller.parse_run_content("run next please")


class RunnableStepsTest(unittest.TestCase):
    def test_skill_ladder_maps_to_step(self) -> None:
        route = make_route(stage="needs_final_prompts", next_skill="final-prompt-compiler")
        self.assertEqual(["final_prompts"], run_controller.runnable_steps(route, NO_INTEGRITY))

    def test_blocked_route_has_nothing_runnable(self) -> None:
        route = make_route(stage="needs_style_master", next_skill="style-master-extractor", blocked=["no refs"])
        self.assertEqual([], run_controller.runnable_steps(route, NO_INTEGRITY))

    def test_pre_qc_without_integrity_report_offers_integrity(self) -> None:
        route = make_route(stage="needs_generated_images_before_qc", blocked=["QC is post-generation only"])
        self.assertEqual(["integrity"], run_controller.runnable_steps(route, NO_INTEGRITY))

    def test_pre_qc_with_failed_gate_offers_integrity_again(self) -> None:
        route = make_route(stage="needs_generated_images_before_qc", blocked=["QC is post-generation only"])
        self.assertEqual(["integrity"], run_controller.runnable_steps(route, FAIL_INTEGRITY))

    def test_pre_qc_with_passing_gate_offers_renders(self) -> None:
        route = make_route(stage="needs_generated_images_before_qc", blocked=["QC is post-generation only"])
        self.assertEqual(["renders"], run_controller.runnable_steps(route, PASS_INTEGRITY))

    def test_ready_offers_nothing(self) -> None:
        self.assertEqual([], run_controller.runnable_steps(make_route(), PASS_INTEGRITY))


class RetryableStepsTest(unittest.TestCase):
    def test_done_artifacts_map_to_steps_in_pipeline_order(self) -> None:
        route = make_route(available=["style_master", "product_identity_archive"])
        self.assertEqual(["identity", "style_master"], run_controller.retryable_steps(route, NO_INTEGRITY))

    def test_integrity_and_renders_detected(self) -> None:
        route = make_route(renders=2)
        self.assertEqual(["integrity", "renders"], run_controller.retryable_steps(route, PASS_INTEGRITY))


class ResolveCommandTest(unittest.TestCase):
    def test_run_next_picks_first_runnable(self) -> None:
        route = make_route(stage="needs_product_identity_archive", next_skill="product-identity-archive")
        self.assertEqual("identity", run_controller.resolve_command(("run", "next"), route, NO_INTEGRITY))

    def test_run_next_with_nothing_runnable_reports_stage(self) -> None:
        with self.assertRaises(run_controller.RunValidationError) as ctx:
            run_controller.resolve_command(("run", "next"), make_route(stage="ready"), PASS_INTEGRITY)
        self.assertIn("ready", str(ctx.exception))

    def test_run_explicit_step_must_be_runnable(self) -> None:
        route = make_route(stage="needs_style_master", next_skill="style-master-extractor",
                           available=["product_identity_archive"])
        with self.assertRaises(run_controller.RunValidationError) as ctx:
            run_controller.resolve_command(("run", "identity"), route, NO_INTEGRITY)
        self.assertIn("retry: identity", str(ctx.exception))

    def test_run_unknown_step_rejected(self) -> None:
        with self.assertRaises(run_controller.RunValidationError):
            run_controller.resolve_command(("run", "banana"), make_route(), NO_INTEGRITY)

    def test_retry_requires_completed_step(self) -> None:
        with self.assertRaises(run_controller.RunValidationError):
            run_controller.resolve_command(("retry", "qc"), make_route(), NO_INTEGRITY)

    def test_retry_next_rejected(self) -> None:
        with self.assertRaises(run_controller.RunValidationError):
            run_controller.resolve_command(("retry", "next"), make_route(), NO_INTEGRITY)

    def test_retry_completed_step_allowed(self) -> None:
        route = make_route(available=["product_identity_archive"])
        self.assertEqual("identity", run_controller.resolve_command(("retry", "identity"), route, NO_INTEGRITY))


class JournalTest(unittest.TestCase):
    def test_append_and_tail_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "demo.events.jsonl"
            run_controller.append_event(path, "step_started", step="identity")
            run_controller.append_event(path, "step_succeeded", step="identity", detail="ok")
            events = run_controller.read_journal_tail(path)
            self.assertEqual(["step_started", "step_succeeded"], [item["event"] for item in events])

    def test_tail_limit_and_corrupted_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "demo.events.jsonl"
            for index in range(12):
                run_controller.append_event(path, "step_succeeded", step=f"s{index}")
            with path.open("a", encoding="utf-8") as handle:
                handle.write("not-json\n")
            events = run_controller.read_journal_tail(path, limit=5)
            self.assertEqual(4, len(events))  # 5 tail lines minus the corrupted one
            self.assertEqual("s11", events[-1]["step"])

    def test_missing_journal_is_empty(self) -> None:
        self.assertEqual([], run_controller.read_journal_tail(Path("Z:/no/such/file.jsonl")))


class ControlNodeOpsTest(unittest.TestCase):
    def test_run_node_op_shape(self) -> None:
        route = make_route(stage="needs_product_identity_archive", next_skill="product-identity-archive")
        op = run_controller.run_node_op("demo_live", route, NO_INTEGRITY, note="✔ identity 完成")
        self.assertEqual("wfrun:demo_live:batch", op["id"])
        content = op["metadata"]["content"]
        self.assertIn("可运行：identity", content)
        self.assertIn("上次结果：✔ identity 完成", content)

    def test_log_node_op_empty_and_filled(self) -> None:
        empty = run_controller.log_node_op("demo_live", [])
        self.assertIn("暂无事件", empty["metadata"]["content"])
        filled = run_controller.render_log_content(
            [
                {"ts": "2026-07-11T20:00:01", "event": "step_started", "step": "identity"},
                {"ts": "2026-07-11T20:00:02", "event": "step_succeeded", "step": "identity", "detail": "ok"},
            ]
        )
        first_line = filled.splitlines()[0]
        self.assertIn("step_succeeded", first_line)  # newest first
        self.assertIn("✔", first_line)


class DemoExecutorTest(unittest.TestCase):
    def _init_workspace(self, root: Path) -> None:
        result = subprocess.run(
            [sys.executable, str(ROOT / "canvas-bridge" / "make_demo_workspace.py"), "--init", "--root", str(root)],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_advance_writes_typed_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "ws"
            self._init_workspace(root)
            executor = DemoWorkspaceExecutor(root)
            result = executor.execute(ExecutionRequest(step="identity"))
            self.assertIn("advanced", result.detail)
            self.assertEqual("demo", result.provider)
            artifact = root / "artifacts" / "identity" / "product_identity_archive.json"
            data = json.loads(artifact.read_text(encoding="utf-8"))
            self.assertEqual("product_identity_archive", data["artifact_type"])

    def test_root_without_marker_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            executor = DemoWorkspaceExecutor(Path(tmp))
            with self.assertRaises(run_controller.RunExecutionError):
                executor.execute(ExecutionRequest(step="identity"))

    def test_unknown_step_refused_without_subprocess(self) -> None:
        executor = DemoWorkspaceExecutor(Path("Z:/nowhere"))
        with self.assertRaises(run_controller.RunExecutionError):
            executor.execute(ExecutionRequest(step="banana"))


class UpperLayerExecutorBoundaryTest(unittest.TestCase):
    def test_controller_uses_protocol_without_provider_knowledge(self) -> None:
        class FakeExecutor:
            name = "future-provider"

            def execute(self, request: ExecutionRequest) -> ExecutionResult:
                return ExecutionResult(detail=f"future handled {request.step}", provider=self.name)

        result = run_controller.execute_step(FakeExecutor(), "identity")

        self.assertEqual("future handled identity", result.detail)
        self.assertEqual("future-provider", result.provider)


class BuildExecutorTest(unittest.TestCase):
    def test_unregistered_executor_rejected(self) -> None:
        with self.assertRaises(UnknownExecutorError):
            build_executor("codex", {"workspace": {"root": "D:/x"}})

    def test_missing_workspace_root_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_executor("demo", {})

    def test_demo_executor_built(self) -> None:
        executor = build_executor("demo", {"workspace": {"root": "D:/dev/canvas-demo-workspace"}})
        self.assertEqual(Path("D:/dev/canvas-demo-workspace"), executor.workspace_root)


if __name__ == "__main__":
    unittest.main()
