from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
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
    ImageGenerationTask,
)
from executor_factory import build_registry  # noqa: E402
from final_prompt_integrity_fixtures import build_final_prompt_bundle, write_json  # noqa: E402
from image_production_executor import ImageProductionExecutor  # noqa: E402
from render_task_assembler import RenderTaskPlan  # noqa: E402


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


def render_plan(output_dir: Path, count: int) -> RenderTaskPlan:
    tasks = tuple(
        ImageGenerationTask(
            prompt=f"safe prompt {index}",
            output_path=output_dir / f"main_{index:02d}.png",
        )
        for index in range(1, count + 1)
    )
    return RenderTaskPlan(
        tasks=tasks,
        planned=tuple(task.output_path.stem for task in tasks),
        skipped=(),
    )


def successful_render(request: ExecutionRequest) -> ExecutionResult:
    request.payload.output_path.write_bytes(b"image")
    return ExecutionResult(
        detail="generated",
        outputs=(request.payload.output_path,),
        provider="fixture-image",
        model="fixture-model",
    )


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
                    RENDER_MAX_CONCURRENCY="1",
                ),
                image_executor_factory=lambda _context: image_executor,
            )

            with self.assertRaises(ExecutorExecutionError) as ctx:
                executor.execute(ExecutionRequest(step="renders"))

            message = str(ctx.exception)
            self.assertEqual(ctx.exception.successful_count, 2)
            self.assertEqual(ctx.exception.planned_count, 14)
            self.assertEqual(ctx.exception.skipped_count, 0)
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

    def test_render_concurrency_unset_or_empty_means_follow_task_count(self) -> None:
        for environment in (
            {},
            {"RENDER_MAX_CONCURRENCY": ""},
            {"RENDER_MAX_CONCURRENCY": "   "},
        ):
            with self.subTest(environment=environment):
                executor = ImageProductionExecutor(
                    ExecutorContext(manifest={}, environment=environment)
                )

                self.assertIsNone(executor._render_concurrency())

    def test_invalid_render_concurrency_values_fail_closed_with_exact_message(self) -> None:
        message = "RENDER_MAX_CONCURRENCY 必须是 1 到 60 的整数"
        for raw in ("0", "abc", "03", "61", "060"):
            with self.subTest(raw=raw):
                executor = ImageProductionExecutor(
                    ExecutorContext(
                        manifest={},
                        environment={"RENDER_MAX_CONCURRENCY": raw},
                    )
                )

                with self.assertRaisesRegex(ExecutorExecutionError, f"^{message}$"):
                    executor._render_concurrency()

    def test_render_concurrency_accepts_nine_and_sixty(self) -> None:
        for raw, expected in (("9", 9), ("60", 60)):
            with self.subTest(raw=raw):
                executor = ImageProductionExecutor(
                    ExecutorContext(
                        manifest={},
                        environment={"RENDER_MAX_CONCURRENCY": raw},
                    )
                )

                self.assertEqual(expected, executor._render_concurrency())

    def test_five_renders_without_explicit_limit_reach_full_batch_concurrency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = build_final_prompt_bundle(Path(tmp))
            plan = render_plan(bundle.renders_dir, 5)
            lock = threading.Lock()
            full_batch_started = threading.Event()
            active = 0
            peak = 0

            class PeakRecordingExecutor:
                name = "full-batch-peak-recording"

                def execute(inner_self, request: ExecutionRequest) -> ExecutionResult:
                    nonlocal active, peak
                    with lock:
                        active += 1
                        peak = max(peak, active)
                        if active == 5:
                            full_batch_started.set()
                    try:
                        if not full_batch_started.wait(timeout=2):
                            raise AssertionError("full batch did not overlap")
                        return successful_render(request)
                    finally:
                        with lock:
                            active -= 1

            executor = ImageProductionExecutor(
                self._context(
                    bundle,
                    RENDER_ALLOW_REAL_EXECUTION="1",
                    OPENAI_API_KEY="server-secret",
                ),
                image_executor_factory=lambda _context: PeakRecordingExecutor(),
                task_assembler=lambda _manifest, _index: plan,
            )

            result = executor.execute(ExecutionRequest(step="renders"))

            self.assertEqual(5, peak)
            self.assertEqual(5, result.metadata["successful_count"])

    def test_single_render_without_explicit_limit_preserves_serial_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = build_final_prompt_bundle(Path(tmp))
            plan = render_plan(bundle.renders_dir, 1)
            observed: list[str] = []
            callback_threads: list[int] = []
            main_thread = threading.get_ident()

            class SingleRenderExecutor:
                name = "single-render-serial"

                def execute(inner_self, request: ExecutionRequest) -> ExecutionResult:
                    name = request.payload.output_path.name
                    observed.append(f"start:{name}")
                    result = successful_render(request)
                    observed.append(f"finish:{name}")
                    return result

            def on_task_success(task, _result: ExecutionResult) -> None:
                observed.append(f"callback:{task.output_path.name}")
                callback_threads.append(threading.get_ident())

            executor = ImageProductionExecutor(
                self._context(
                    bundle,
                    RENDER_ALLOW_REAL_EXECUTION="1",
                    OPENAI_API_KEY="server-secret",
                ),
                image_executor_factory=lambda _context: SingleRenderExecutor(),
                task_assembler=lambda _manifest, _index: plan,
                on_task_success=on_task_success,
            )

            result = executor.execute(ExecutionRequest(step="renders"))

            self.assertEqual(
                ["start:main_01.png", "finish:main_01.png", "callback:main_01.png"],
                observed,
            )
            self.assertEqual([main_thread], callback_threads)
            self.assertEqual((plan.tasks[0].output_path,), result.outputs)

    def test_explicit_limit_two_caps_five_renders_at_two_concurrent_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = build_final_prompt_bundle(Path(tmp))
            plan = render_plan(bundle.renders_dir, 5)
            lock = threading.Lock()
            two_workers_started = threading.Event()
            active = 0
            peak = 0

            class PeakRecordingExecutor:
                name = "explicit-limit-peak-recording"

                def execute(inner_self, request: ExecutionRequest) -> ExecutionResult:
                    nonlocal active, peak
                    with lock:
                        active += 1
                        peak = max(peak, active)
                        if active == 2:
                            two_workers_started.set()
                    try:
                        if not two_workers_started.wait(timeout=2):
                            raise AssertionError("two workers did not overlap")
                        return successful_render(request)
                    finally:
                        with lock:
                            active -= 1

            executor = ImageProductionExecutor(
                self._context(
                    bundle,
                    RENDER_ALLOW_REAL_EXECUTION="1",
                    OPENAI_API_KEY="server-secret",
                    RENDER_MAX_CONCURRENCY="2",
                ),
                image_executor_factory=lambda _context: PeakRecordingExecutor(),
                task_assembler=lambda _manifest, _index: plan,
            )

            result = executor.execute(ExecutionRequest(step="renders"))

            self.assertEqual(2, peak)
            self.assertEqual(5, result.metadata["successful_count"])

    def test_five_renders_with_three_workers_reach_exact_concurrency_peak(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = build_final_prompt_bundle(Path(tmp))
            plan = render_plan(bundle.renders_dir, 5)
            lock = threading.Lock()
            all_workers_started = threading.Event()
            active = 0
            peak = 0

            class PeakRecordingExecutor:
                name = "peak-recording"

                def execute(inner_self, request: ExecutionRequest) -> ExecutionResult:
                    nonlocal active, peak
                    with lock:
                        active += 1
                        peak = max(peak, active)
                        if active == 3:
                            all_workers_started.set()
                    try:
                        if not all_workers_started.wait(timeout=2):
                            raise AssertionError("three workers did not overlap")
                        return successful_render(request)
                    finally:
                        with lock:
                            active -= 1

            executor = ImageProductionExecutor(
                self._context(
                    bundle,
                    RENDER_ALLOW_REAL_EXECUTION="1",
                    OPENAI_API_KEY="server-secret",
                    RENDER_MAX_CONCURRENCY="3",
                ),
                image_executor_factory=lambda _context: PeakRecordingExecutor(),
                task_assembler=lambda _manifest, _index: plan,
            )

            result = executor.execute(ExecutionRequest(step="renders"))

            self.assertEqual(3, peak)
            self.assertGreater(peak, 1)
            self.assertEqual(5, result.metadata["successful_count"])

    def test_out_of_order_completions_keep_outputs_and_callbacks_in_task_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = build_final_prompt_bundle(Path(tmp))
            plan = render_plan(bundle.renders_dir, 3)
            barrier = threading.Barrier(3)
            allow_second = threading.Event()
            allow_first = threading.Event()
            lock = threading.Lock()
            completion_order: list[str] = []
            callback_order: list[str] = []
            callback_threads: list[int] = []
            main_thread = threading.get_ident()

            class OutOfOrderExecutor:
                name = "out-of-order"

                def execute(inner_self, request: ExecutionRequest) -> ExecutionResult:
                    name = request.payload.output_path.name
                    barrier.wait(timeout=2)
                    if name == "main_03.png":
                        result = successful_render(request)
                        with lock:
                            completion_order.append(name)
                        allow_second.set()
                        return result
                    if name == "main_02.png":
                        if not allow_second.wait(timeout=2):
                            raise AssertionError("third task did not complete first")
                        result = successful_render(request)
                        with lock:
                            completion_order.append(name)
                        allow_first.set()
                        return result
                    if not allow_first.wait(timeout=2):
                        raise AssertionError("second task did not complete before first")
                    result = successful_render(request)
                    with lock:
                        completion_order.append(name)
                    return result

            def on_task_success(task, _result: ExecutionResult) -> None:
                callback_order.append(task.output_path.name)
                callback_threads.append(threading.get_ident())

            executor = ImageProductionExecutor(
                self._context(
                    bundle,
                    RENDER_ALLOW_REAL_EXECUTION="1",
                    OPENAI_API_KEY="server-secret",
                    RENDER_MAX_CONCURRENCY="3",
                ),
                image_executor_factory=lambda _context: OutOfOrderExecutor(),
                task_assembler=lambda _manifest, _index: plan,
                on_task_success=on_task_success,
            )

            result = executor.execute(ExecutionRequest(step="renders"))

            expected = ["main_01.png", "main_02.png", "main_03.png"]
            self.assertEqual(["main_03.png", "main_02.png", "main_01.png"], completion_order)
            self.assertEqual(expected, [path.name for path in result.outputs])
            self.assertEqual(expected, callback_order)
            self.assertEqual([main_thread] * 3, callback_threads)

    def test_second_task_failure_drains_in_flight_successes_and_cancels_unstarted_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = build_final_prompt_bundle(Path(tmp))
            plan = render_plan(bundle.renders_dir, 5)
            barrier = threading.Barrier(3)
            failure_started = threading.Event()
            lock = threading.Lock()
            calls: list[str] = []
            callback_order: list[str] = []

            class SecondTaskFailureExecutor:
                name = "second-task-failure"

                def execute(inner_self, request: ExecutionRequest) -> ExecutionResult:
                    name = request.payload.output_path.name
                    with lock:
                        calls.append(name)
                    barrier.wait(timeout=2)
                    if name == "main_02.png":
                        failure_started.set()
                        raise ExecutorExecutionError("main_02 task failed")
                    if not failure_started.wait(timeout=2):
                        raise AssertionError("second task did not fail")
                    return successful_render(request)

            executor = ImageProductionExecutor(
                self._context(
                    bundle,
                    RENDER_ALLOW_REAL_EXECUTION="1",
                    OPENAI_API_KEY="server-secret",
                    RENDER_MAX_CONCURRENCY="3",
                ),
                image_executor_factory=lambda _context: SecondTaskFailureExecutor(),
                task_assembler=lambda _manifest, _index: plan,
                on_task_success=lambda task, _result: callback_order.append(
                    task.output_path.name
                ),
            )

            with self.assertRaises(ExecutorExecutionError) as ctx:
                executor.execute(ExecutionRequest(step="renders"))

            self.assertEqual("main_02 task failed", str(ctx.exception))
            self.assertEqual(2, ctx.exception.successful_count)
            self.assertEqual(5, ctx.exception.planned_count)
            self.assertEqual(0, ctx.exception.skipped_count)
            self.assertEqual(
                {"main_01.png", "main_02.png", "main_03.png"},
                set(calls),
            )
            self.assertNotIn("main_04.png", calls)
            self.assertNotIn("main_05.png", calls)
            self.assertEqual(["main_01.png", "main_03.png"], callback_order)

    def test_full_batch_failure_drains_all_in_flight_successes_in_task_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = build_final_prompt_bundle(Path(tmp))
            plan = render_plan(bundle.renders_dir, 5)
            barrier = threading.Barrier(5)
            failure_started = threading.Event()
            lock = threading.Lock()
            calls: list[str] = []
            callback_order: list[str] = []

            class IdentityFailureExecutor:
                name = "full-batch-identity-failure"

                def execute(inner_self, request: ExecutionRequest) -> ExecutionResult:
                    name = request.payload.output_path.name
                    with lock:
                        calls.append(name)
                    barrier.wait(timeout=2)
                    if name == "main_02.png":
                        failure_started.set()
                        raise ExecutorExecutionError("main_02 task failed")
                    if not failure_started.wait(timeout=2):
                        raise AssertionError("identified task did not fail")
                    return successful_render(request)

            executor = ImageProductionExecutor(
                self._context(
                    bundle,
                    RENDER_ALLOW_REAL_EXECUTION="1",
                    OPENAI_API_KEY="server-secret",
                ),
                image_executor_factory=lambda _context: IdentityFailureExecutor(),
                task_assembler=lambda _manifest, _index: plan,
                on_task_success=lambda task, _result: callback_order.append(
                    task.output_path.name
                ),
            )

            with self.assertRaises(ExecutorExecutionError) as ctx:
                executor.execute(ExecutionRequest(step="renders"))

            self.assertEqual("main_02 task failed", str(ctx.exception))
            self.assertEqual(4, ctx.exception.successful_count)
            self.assertEqual(5, ctx.exception.planned_count)
            self.assertEqual(0, ctx.exception.skipped_count)
            self.assertEqual(
                {f"main_{index:02d}.png" for index in range(1, 6)},
                set(calls),
            )
            self.assertEqual(
                ["main_01.png", "main_03.png", "main_04.png", "main_05.png"],
                callback_order,
            )

    def test_task_success_callback_failure_uses_wrapped_render_failure_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = build_final_prompt_bundle(Path(tmp))
            plan = render_plan(bundle.renders_dir, 2)
            lock = threading.Lock()
            calls: list[str] = []

            class CallbackFixtureExecutor:
                name = "callback-fixture"

                def execute(inner_self, request: ExecutionRequest) -> ExecutionResult:
                    with lock:
                        calls.append(request.payload.output_path.name)
                    return successful_render(request)

            def reject_output(_task, _result: ExecutionResult) -> None:
                raise ExecutorExecutionError("渲染结果不在当前批次登记图位中。")

            executor = ImageProductionExecutor(
                self._context(
                    bundle,
                    RENDER_ALLOW_REAL_EXECUTION="1",
                    OPENAI_API_KEY="server-secret",
                    RENDER_MAX_CONCURRENCY="1",
                ),
                image_executor_factory=lambda _context: CallbackFixtureExecutor(),
                task_assembler=lambda _manifest, _index: plan,
                on_task_success=reject_output,
            )

            with self.assertRaises(ExecutorExecutionError) as ctx:
                executor.execute(ExecutionRequest(step="renders"))

            self.assertEqual("渲染结果不在当前批次登记图位中。", str(ctx.exception))
            self.assertEqual(0, ctx.exception.successful_count)
            self.assertEqual(2, ctx.exception.planned_count)
            self.assertEqual(["main_01.png"], calls)

    def test_single_render_reuses_one_factory_instance_and_preserves_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = build_final_prompt_bundle(Path(tmp))
            plan = render_plan(bundle.renders_dir, 1)
            factory_calls: list[ExecutorContext] = []
            execute_calls: list[ExecutionRequest] = []

            class SingleRenderExecutor:
                name = "single-render"

                def execute(inner_self, request: ExecutionRequest) -> ExecutionResult:
                    execute_calls.append(request)
                    return successful_render(request)

            image_executor = SingleRenderExecutor()

            def image_factory(context: ExecutorContext) -> SingleRenderExecutor:
                factory_calls.append(context)
                return image_executor

            executor = ImageProductionExecutor(
                self._context(
                    bundle,
                    RENDER_ALLOW_REAL_EXECUTION="1",
                    OPENAI_API_KEY="server-secret",
                ),
                image_executor_factory=image_factory,
                task_assembler=lambda _manifest, _index: plan,
            )

            result = executor.execute(
                ExecutionRequest(step="renders", metadata={"fixture": "single"})
            )

            self.assertEqual(1, len(factory_calls))
            self.assertEqual(1, len(execute_calls))
            self.assertEqual({"fixture": "single"}, execute_calls[0].metadata)
            self.assertEqual((plan.tasks[0].output_path,), result.outputs)
            self.assertEqual("fixture-model", result.model)

    def test_concurrent_unconditional_failures_start_only_worker_limit_and_cancel_rest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = build_final_prompt_bundle(Path(tmp))
            plan = render_plan(bundle.renders_dir, 7)
            barrier = threading.Barrier(3)
            lock = threading.Lock()
            calls: list[str] = []

            class AlwaysFailingExecutor:
                name = "always-failing"

                def execute(inner_self, request: ExecutionRequest) -> ExecutionResult:
                    name = request.payload.output_path.name
                    with lock:
                        calls.append(name)
                    barrier.wait(timeout=2)
                    raise ExecutorExecutionError(f"failure from {name}")

            executor = ImageProductionExecutor(
                self._context(
                    bundle,
                    RENDER_ALLOW_REAL_EXECUTION="1",
                    OPENAI_API_KEY="server-secret",
                    RENDER_MAX_CONCURRENCY="3",
                ),
                image_executor_factory=lambda _context: AlwaysFailingExecutor(),
                task_assembler=lambda _manifest, _index: plan,
            )

            with self.assertRaises(ExecutorExecutionError) as ctx:
                executor.execute(ExecutionRequest(step="renders"))

            self.assertEqual(3, len(calls))
            self.assertEqual(
                {"main_01.png", "main_02.png", "main_03.png"},
                set(calls),
            )
            self.assertNotIn("main_04.png", calls)
            self.assertNotIn("main_05.png", calls)
            self.assertNotIn("main_06.png", calls)
            self.assertNotIn("main_07.png", calls)
            self.assertEqual(0, ctx.exception.successful_count)
            self.assertEqual(7, ctx.exception.planned_count)

    def test_multiple_concurrent_failures_report_first_failed_task_in_selected_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = build_final_prompt_bundle(Path(tmp))
            plan = render_plan(bundle.renders_dir, 3)
            barrier = threading.Barrier(3)

            class IdentityFailureExecutor:
                name = "identity-failure"

                def execute(inner_self, request: ExecutionRequest) -> ExecutionResult:
                    name = request.payload.output_path.name
                    barrier.wait(timeout=2)
                    raise ExecutorExecutionError(f"failure from {name}")

            executor = ImageProductionExecutor(
                self._context(
                    bundle,
                    RENDER_ALLOW_REAL_EXECUTION="1",
                    OPENAI_API_KEY="server-secret",
                    RENDER_MAX_CONCURRENCY="3",
                ),
                image_executor_factory=lambda _context: IdentityFailureExecutor(),
                task_assembler=lambda _manifest, _index: plan,
            )

            with self.assertRaises(ExecutorExecutionError) as ctx:
                executor.execute(ExecutionRequest(step="renders"))

            self.assertEqual("failure from main_01.png", str(ctx.exception))
            self.assertEqual(0, ctx.exception.successful_count)
            self.assertEqual(3, ctx.exception.planned_count)

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
