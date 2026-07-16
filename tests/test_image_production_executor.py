from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
TESTS = ROOT / "tests"
for extra in (BRIDGE, TESTS):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from executor_contract import (  # noqa: E402
    ExecutionRequest,
    ExecutionResult,
    ExecutorContext,
    ExecutorExecutionError,
)
from executor_factory import build_registry  # noqa: E402
from final_prompt_integrity_fixtures import build_final_prompt_bundle, write_json  # noqa: E402
from image_production_executor import ImageProductionExecutor  # noqa: E402


class RecordingAssembler:
    def __init__(self, result=None):
        self.result = result
        self.calls = []

    def __call__(self, manifest, index_path):
        self.calls.append((manifest, index_path))
        return self.result


class RecordingImageExecutor:
    name = "openai-image"

    def __init__(self, *, fail_at: int | None = None, failure_message: str = "provider failed"):
        self.fail_at = fail_at
        self.failure_message = failure_message
        self.calls = []

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        self.calls.append(request)
        if self.fail_at is not None and len(self.calls) == self.fail_at:
            raise ExecutorExecutionError(self.failure_message + " " + request.payload.prompt)
        request.payload.output_path.write_bytes(f"image-{len(self.calls)}".encode("ascii"))
        return ExecutionResult(
            detail="generated",
            outputs=(request.payload.output_path,),
            provider=self.name,
            model="fixture-model",
        )


class ReportWritingRunner:
    def __init__(self, report_path: Path, *, status: str = "pass", returncode: int = 0):
        self.report_path = report_path
        self.status = status
        self.returncode = returncode
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if self.status != "missing":
            write_json(
                self.report_path,
                {
                    "product_id": "fixture_product",
                    "artifact_type": "final_prompt_integrity_report",
                    "gate_name": "final_prompt_integrity_gate",
                    "status": self.status,
                    "render_blocked": self.status == "fail",
                    "checked_assets": [],
                    "results": [],
                    "blocking_issues": [] if self.status != "fail" else [{"issue_id": "fixture"}],
                    "warnings": [],
                    "image_generation_performed": False,
                    "comfyui_execution_performed": False,
                },
            )
        return subprocess.CompletedProcess(command, self.returncode, stdout="runner output", stderr="runner error")


class ImageProductionExecutorTest(unittest.TestCase):
    def _context(self, bundle, **environment):
        return ExecutorContext(
            manifest=bundle.manifest,
            manifest_path=bundle.manifest_path,
            environment=environment,
        )

    def test_only_integrity_and_renders_are_accepted_before_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = build_final_prompt_bundle(Path(tmp))
            runner = ReportWritingRunner(bundle.qc_dir / "final_prompt_integrity_report.json")
            assembler = RecordingAssembler()
            factory_calls = []
            executor = ImageProductionExecutor(
                self._context(bundle),
                subprocess_runner=runner,
                task_assembler=assembler,
                image_executor_factory=lambda context: factory_calls.append(context),
                repo_report_dir=Path(tmp) / "reports",
            )

            with self.assertRaises(ExecutorExecutionError):
                executor.execute(ExecutionRequest(step="qc"))

            self.assertEqual([], runner.calls)
            self.assertEqual([], assembler.calls)
            self.assertEqual([], factory_calls)

    def test_render_switch_fails_before_assembly_or_transport(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = build_final_prompt_bundle(Path(tmp))
            assembler = RecordingAssembler()
            factory_calls = []
            executor = ImageProductionExecutor(
                self._context(bundle, OPENAI_API_KEY="server-secret"),
                task_assembler=assembler,
                image_executor_factory=lambda context: factory_calls.append(context),
            )

            with self.assertRaises(ExecutorExecutionError) as ctx:
                executor.execute(ExecutionRequest(step="renders"))

            self.assertIn("RENDER_ALLOW_REAL_EXECUTION", str(ctx.exception))
            self.assertEqual([], assembler.calls)
            self.assertEqual([], factory_calls)

    def test_api_key_fails_before_assembly_or_transport(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = build_final_prompt_bundle(Path(tmp))
            assembler = RecordingAssembler()
            executor = ImageProductionExecutor(
                self._context(bundle, RENDER_ALLOW_REAL_EXECUTION="1"),
                task_assembler=assembler,
            )

            with self.assertRaises(ExecutorExecutionError) as ctx:
                executor.execute(ExecutionRequest(step="renders"))

            self.assertIn("OPENAI_API_KEY", str(ctx.exception))
            self.assertEqual([], assembler.calls)

    def test_third_image_failure_preserves_first_two_and_sanitizes_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = build_final_prompt_bundle(Path(tmp))
            image_executor = RecordingImageExecutor(
                fail_at=3,
                failure_message="backend rejected server-secret",
            )
            executor = ImageProductionExecutor(
                self._context(
                    bundle,
                    RENDER_ALLOW_REAL_EXECUTION="1",
                    OPENAI_API_KEY="server-secret",
                ),
                image_executor_factory=lambda _context: image_executor,
            )

            with self.assertRaises(ExecutorExecutionError) as ctx:
                executor.execute(ExecutionRequest(step="renders"))

            message = str(ctx.exception)
            self.assertIn("成功 2/计划 14（跳过 0）", message)
            self.assertNotIn("server-secret", message)
            self.assertNotIn("测试电商图", message)
            self.assertTrue((bundle.renders_dir / "main_01.png").is_file())
            self.assertTrue((bundle.renders_dir / "main_02.png").is_file())
            self.assertFalse((bundle.renders_dir / "main_03.png").exists())

    def test_render_max_images_one_executes_first_missing_task_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = build_final_prompt_bundle(Path(tmp))
            image_executor = RecordingImageExecutor()
            executor = ImageProductionExecutor(
                self._context(
                    bundle,
                    RENDER_ALLOW_REAL_EXECUTION="1",
                    OPENAI_API_KEY="server-secret",
                    RENDER_MAX_IMAGES="1",
                ),
                image_executor_factory=lambda _context: image_executor,
            )

            result = executor.execute(ExecutionRequest(step="renders"))

            self.assertEqual(1, len(image_executor.calls))
            self.assertEqual("main_01.png", image_executor.calls[0].payload.output_path.name)
            self.assertIn("成功 1/计划 1（跳过 0）", result.detail)
            self.assertEqual(14, result.metadata["full_missing_count"])
            self.assertEqual(13, result.metadata["remaining_count"])

    def test_integrity_subprocess_normalizes_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = build_final_prompt_bundle(Path(tmp))
            report_path = bundle.qc_dir / "final_prompt_integrity_report.json"
            runner = ReportWritingRunner(report_path, status="pass", returncode=0)
            executor = ImageProductionExecutor(
                self._context(bundle),
                subprocess_runner=runner,
                repo_report_dir=Path(tmp) / "reports",
            )

            result = executor.execute(ExecutionRequest(step="integrity"))

            command = runner.calls[0][0]
            self.assertIn("--prompts-only", command)
            self.assertIn("--batch-manifest", command)
            self.assertEqual("pass", result.metadata["status"])
            self.assertEqual((report_path,), result.outputs)

    def test_integrity_subprocess_normalizes_failed_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = build_final_prompt_bundle(Path(tmp))
            runner = ReportWritingRunner(
                bundle.qc_dir / "final_prompt_integrity_report.json",
                status="fail",
                returncode=1,
            )
            executor = ImageProductionExecutor(self._context(bundle), subprocess_runner=runner)

            with self.assertRaises(ExecutorExecutionError) as ctx:
                executor.execute(ExecutionRequest(step="integrity"))

            self.assertIn("完整性门禁未通过", str(ctx.exception))
            self.assertNotIn("runner error", str(ctx.exception))

    def test_integrity_subprocess_process_error_or_missing_report_is_sanitized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = build_final_prompt_bundle(Path(tmp))
            runner = ReportWritingRunner(
                bundle.qc_dir / "final_prompt_integrity_report.json",
                status="missing",
                returncode=2,
            )
            executor = ImageProductionExecutor(self._context(bundle), subprocess_runner=runner)

            with self.assertRaises(ExecutorExecutionError) as ctx:
                executor.execute(ExecutionRequest(step="integrity"))

            self.assertIn("未生成有效报告", str(ctx.exception))
            self.assertNotIn("runner error", str(ctx.exception))

    def test_invalid_render_max_images_fails_before_assembly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = build_final_prompt_bundle(Path(tmp))
            assembler = RecordingAssembler()
            executor = ImageProductionExecutor(
                self._context(
                    bundle,
                    RENDER_ALLOW_REAL_EXECUTION="1",
                    OPENAI_API_KEY="server-secret",
                    RENDER_MAX_IMAGES="zero",
                ),
                task_assembler=assembler,
            )

            with self.assertRaises(ExecutorExecutionError):
                executor.execute(ExecutionRequest(step="renders"))
            self.assertEqual([], assembler.calls)

    def test_all_existing_outputs_use_zero_transport(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = build_final_prompt_bundle(Path(tmp))
            for mode, count in (("main", 6), ("detail", 8)):
                for number in range(1, count + 1):
                    (bundle.renders_dir / f"{mode}_{number:02d}.png").write_bytes(b"existing")
            factory_calls = []
            executor = ImageProductionExecutor(
                self._context(
                    bundle,
                    RENDER_ALLOW_REAL_EXECUTION="1",
                    OPENAI_API_KEY="server-secret",
                ),
                image_executor_factory=lambda context: factory_calls.append(context),
            )

            result = executor.execute(ExecutionRequest(step="renders"))

            self.assertEqual([], factory_calls)
            self.assertIn("成功 0/计划 0（跳过 14）", result.detail)
            self.assertEqual(14, result.metadata["skipped_count"])

    def test_registry_adds_image_production_without_changing_existing_adapters(self) -> None:
        self.assertEqual(
            ("codex-dev", "demo", "image-production", "openai-image"),
            build_registry().names(),
        )


if __name__ == "__main__":
    unittest.main()
