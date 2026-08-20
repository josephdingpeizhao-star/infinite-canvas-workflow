from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
TESTS = ROOT / "tests"
for extra in (BRIDGE, TESTS):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import image_production_executor as ipe  # noqa: E402
from executor_contract import (  # noqa: E402
    ExecutionRequest,
    ExecutionResult,
    ExecutorContext,
    ExecutorExecutionError,
    ImageGenerationTask,
)
from final_prompt_integrity_fixtures import build_final_prompt_bundle  # noqa: E402
from openai_image_executor import (  # noqa: E402
    HttpResponse,
    OpenAIImageExecutor,
    _parse_retry_after_seconds,
)
from qc_repair import prepare_repair_plan  # noqa: E402
from qc_repair_executor import QcRepairExecutor  # noqa: E402
from qc_repair_fixtures import build_qc_repair_fixture  # noqa: E402
from render_task_assembler import RenderTaskPlan  # noqa: E402
from workflow_production_service import WorkflowProductionService  # noqa: E402


def render_failure(
    code: str,
    *,
    http_status: int | None = None,
    retry_after_seconds: int | None = None,
) -> ExecutorExecutionError:
    failure = ExecutorExecutionError(f"fixture {code}")
    failure.code = code
    if http_status is not None:
        failure.http_status = http_status
    if retry_after_seconds is not None:
        failure.retry_after_seconds = retry_after_seconds
    return failure


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


def read_events(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


class ScriptedImageExecutor:
    name = "fixture-image"

    def __init__(
        self,
        scripts: dict[str, list[ExecutorExecutionError | None]],
        *,
        include_output: bool = True,
    ) -> None:
        self.scripts = scripts
        self.include_output = include_output
        self.calls: list[str] = []
        self.counts: dict[str, int] = {}
        self.lock = threading.Lock()

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        config_id = request.payload.output_path.stem
        with self.lock:
            index = self.counts.get(config_id, 0)
            self.counts[config_id] = index + 1
            self.calls.append(config_id)
            script = self.scripts.get(config_id, [None])
            outcome = script[index] if index < len(script) else script[-1]
        if outcome is not None:
            raise outcome
        outputs: tuple[Path, ...] = ()
        if self.include_output:
            request.payload.output_path.write_bytes(b"fixture-image")
            outputs = (request.payload.output_path,)
        return ExecutionResult(
            detail="generated",
            outputs=outputs,
            provider=self.name,
            model="fixture-model",
        )


class RecordingTransport:
    def __init__(self, response: HttpResponse) -> None:
        self.response = response
        self.calls = 0

    def post(self, *_args, **_kwargs) -> HttpResponse:
        self.calls += 1
        return self.response


class RenderTransientRetryTest(unittest.TestCase):
    def _production_executor(
        self,
        root: Path,
        delegate: ScriptedImageExecutor,
        *,
        count: int = 1,
        concurrency: int | None = None,
        sleep_fn=lambda _delay: None,
        jitter_fn=lambda _minimum, _maximum: 0.0,
        on_task_retry=None,
    ) -> tuple[ipe.ImageProductionExecutor, RenderTaskPlan]:
        bundle = build_final_prompt_bundle(root)
        environment = {
            "RENDER_ALLOW_REAL_EXECUTION": "1",
            "OPENAI_API_KEY": "fixture-secret",
        }
        if concurrency is not None:
            environment["RENDER_MAX_CONCURRENCY"] = str(concurrency)
        context = ExecutorContext(
            manifest=bundle.manifest,
            manifest_path=bundle.manifest_path,
            environment=environment,
        )
        plan = render_plan(bundle.renders_dir, count)
        executor = ipe.ImageProductionExecutor(
            context,
            image_executor_factory=lambda _context: delegate,
            task_assembler=lambda _manifest, _index: plan,
            sleep_fn=sleep_fn,
            jitter_fn=jitter_fn,
            on_task_retry=on_task_retry,
        )
        return executor, plan

    def test_transient_classifier_and_literal_policy_anchors(self) -> None:
        self.assertEqual(
            frozenset({429, 502, 503, 504, 524}),
            ipe.RENDER_TRANSIENT_HTTP_STATUSES,
        )
        self.assertEqual(
            frozenset({"render_timeout", "render_network_error"}),
            ipe.RENDER_TRANSIENT_FAILURE_CODES,
        )
        self.assertEqual(2, ipe.RENDER_TRANSIENT_RETRY_LIMIT)
        self.assertEqual((5.0, 15.0), ipe.RENDER_RETRY_BACKOFF_SECONDS)
        self.assertEqual(3.0, ipe.RENDER_RETRY_JITTER_MAX_SECONDS)
        self.assertEqual(60, ipe.RENDER_RETRY_AFTER_CAP_SECONDS)
        self.assertIn("transient_retry_attempts", ipe._RENDER_FAILURE_FIELDS)
        self.assertNotIn("retry_after_seconds", ipe._RENDER_FAILURE_FIELDS)

        for status in (429, 502, 503, 504, 524):
            with self.subTest(status=status):
                self.assertTrue(
                    ipe._is_transient_render_failure(
                        render_failure("render_http_error", http_status=status)
                    )
                )
        for code in ("render_timeout", "render_network_error"):
            with self.subTest(code=code):
                self.assertTrue(ipe._is_transient_render_failure(render_failure(code)))
        for code in (
            "render_response_invalid",
            "render_image_download_failed",
            "render_input_missing",
            "render_inputs_unavailable",
            "render_pipeline_error",
            "render_canvas_unavailable",
        ):
            with self.subTest(code=code):
                self.assertFalse(ipe._is_transient_render_failure(render_failure(code)))
        for status in (400, 401, 403, 404, 408, 422, 499, 500, 501, 505):
            with self.subTest(status=status):
                self.assertFalse(
                    ipe._is_transient_render_failure(
                        render_failure("render_http_error", http_status=status)
                    )
                )

    def test_524_twice_then_success_retries_exactly_twice_and_records_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            delegate = ScriptedImageExecutor(
                {
                    "main_01": [
                        render_failure("render_http_error", http_status=524),
                        render_failure("render_http_error", http_status=524),
                        None,
                    ]
                }
            )
            journal = root / "render.events.jsonl"
            sleeps: list[float] = []
            executor, _plan = self._production_executor(
                root,
                delegate,
                sleep_fn=sleeps.append,
                on_task_retry=lambda task, attempt, code, status, delay: (
                    WorkflowProductionService._record_render_retry(
                        journal, task, attempt, code, status, delay
                    )
                ),
            )

            result = executor.execute(ExecutionRequest(step="renders"))

            self.assertEqual(1, result.metadata["successful_count"])
            self.assertEqual(["main_01", "main_01", "main_01"], delegate.calls)
            self.assertEqual([5.0, 15.0], sleeps)
            events = read_events(journal)
            self.assertEqual(2, len(events))
            self.assertEqual(["render_retry", "render_retry"], [e["event"] for e in events])
            self.assertEqual([1, 2], [e["attempt"] for e in events])

    def test_524_exhaustion_preserves_failure_fields_and_actual_retry_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            delegate = ScriptedImageExecutor(
                {
                    "main_01": [
                        render_failure("render_http_error", http_status=524),
                        render_failure("render_http_error", http_status=524),
                        render_failure("render_http_error", http_status=524),
                    ]
                }
            )
            executor, _plan = self._production_executor(Path(tmp), delegate)

            with self.assertRaises(ExecutorExecutionError) as caught:
                executor.execute(ExecutionRequest(step="renders"))

            failure = caught.exception
            self.assertEqual(3, len(delegate.calls))
            self.assertEqual("render_http_error", failure.code)
            self.assertEqual(524, failure.http_status)
            self.assertEqual(2, failure.transient_retry_attempts)
            self.assertEqual(0, failure.successful_count)
            self.assertEqual(1, failure.planned_count)

    def test_non_transient_http_failure_is_not_retried(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            delegate = ScriptedImageExecutor(
                {"main_01": [render_failure("render_http_error", http_status=400)]}
            )
            sleeps: list[float] = []
            executor, _plan = self._production_executor(
                Path(tmp), delegate, sleep_fn=sleeps.append
            )

            with self.assertRaises(ExecutorExecutionError) as caught:
                executor.execute(ExecutionRequest(step="renders"))

            self.assertEqual(["main_01"], delegate.calls)
            self.assertEqual([], sleeps)
            self.assertFalse(hasattr(caught.exception, "transient_retry_attempts"))

    def test_terminal_failure_after_one_retry_reports_the_actual_retry_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            delegate = ScriptedImageExecutor(
                {
                    "main_01": [
                        render_failure("render_http_error", http_status=524),
                        render_failure("render_http_error", http_status=400),
                    ]
                }
            )
            executor, _plan = self._production_executor(Path(tmp), delegate)

            with self.assertRaises(ExecutorExecutionError) as caught:
                executor.execute(ExecutionRequest(step="renders"))

            self.assertEqual(2, len(delegate.calls))
            self.assertEqual(400, caught.exception.http_status)
            self.assertEqual(1, caught.exception.transient_retry_attempts)
            self.assertIn(
                "已自动重试 1 次仍失败",
                WorkflowProductionService._safe_failure(caught.exception),
            )

    def test_retry_after_parser_is_429_only_and_ascii_seconds_bounded(self) -> None:
        for raw, expected in (
            ("1", 1),
            ("60", 60),
            ("600", 600),
            (" 15 ", 15),
            ("0", None),
            ("601", None),
            ("1.5", None),
            ("Wed, 21 Oct 2015 07:28:00 GMT", None),
            ("", None),
            ("１２", None),
        ):
            with self.subTest(raw=raw):
                response = HttpResponse(
                    status=429,
                    headers={"Retry-After": raw},
                    body=b"{}",
                )
                self.assertEqual(expected, _parse_retry_after_seconds(response))

        class ExplodingHeaders(dict):
            def items(self):
                raise AssertionError("non-429 response must not inspect Retry-After")

        self.assertIsNone(
            _parse_retry_after_seconds(
                HttpResponse(status=503, headers=ExplodingHeaders(), body=b"{}")
            )
        )

    def test_openai_429_attaches_valid_retry_after_but_other_status_does_not(self) -> None:
        task = ImageGenerationTask(prompt="safe", output_path=Path("unused.png"))
        body = json.dumps({"error": {"code": "rate_limit", "message": "busy"}}).encode()
        for status, expected in ((429, 45), (503, None)):
            with self.subTest(status=status):
                response = HttpResponse(
                    status=status,
                    headers={"Retry-After": "45"},
                    body=body,
                )
                transport = RecordingTransport(response)
                executor = OpenAIImageExecutor(
                    ExecutorContext(
                        manifest={}, environment={"OPENAI_API_KEY": "fixture-secret"}
                    ),
                    transport=transport,
                )
                with self.assertRaises(ExecutorExecutionError) as caught:
                    executor.execute(ExecutionRequest(step="renders", payload=task))
                if expected is None:
                    self.assertFalse(hasattr(caught.exception, "retry_after_seconds"))
                else:
                    self.assertEqual(expected, caught.exception.retry_after_seconds)

    def test_oversized_retry_after_is_invalid_and_falls_back_to_backoff(self) -> None:
        response = HttpResponse(
            status=429,
            headers={"Retry-After": "9" * 5_000},
            body=json.dumps(
                {"error": {"code": "rate_limit", "message": "busy"}}
            ).encode(),
        )
        executor = OpenAIImageExecutor(
            ExecutorContext(
                manifest={}, environment={"OPENAI_API_KEY": "fixture-secret"}
            ),
            transport=RecordingTransport(response),
        )
        task = ImageGenerationTask(prompt="safe", output_path=Path("unused.png"))

        with self.assertRaises(ExecutorExecutionError) as caught:
            executor.execute(ExecutionRequest(step="renders", payload=task))

        self.assertEqual("render_http_error", caught.exception.code)
        self.assertEqual(429, caught.exception.http_status)
        self.assertFalse(hasattr(caught.exception, "retry_after_seconds"))
        retry_executor = ipe.ImageProductionExecutor(
            ExecutorContext(manifest={}),
            sleep_fn=lambda _delay: None,
            jitter_fn=lambda _minimum, _maximum: 0.0,
        )
        self.assertEqual(
            5.0,
            retry_executor._retry_delay_seconds(caught.exception, 1),
        )

    def test_retry_after_delay_uses_backoff_max_jitter_and_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            delegate = ScriptedImageExecutor({"main_01": [None]})
            executor, _plan = self._production_executor(
                Path(tmp), delegate, jitter_fn=lambda _minimum, _maximum: 2.0
            )
            self.assertEqual(
                22.0,
                executor._retry_delay_seconds(
                    render_failure(
                        "render_http_error",
                        http_status=429,
                        retry_after_seconds=20,
                    ),
                    1,
                ),
            )
            self.assertEqual(
                60.0,
                executor._retry_delay_seconds(
                    render_failure(
                        "render_http_error",
                        http_status=429,
                        retry_after_seconds=600,
                    ),
                    2,
                ),
            )
            self.assertEqual(
                7.0,
                executor._retry_delay_seconds(
                    render_failure("render_http_error", http_status=429), 1
                ),
            )

    def test_stop_starting_prevents_sibling_retry_after_backoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backoff_started = threading.Event()
            real_event_factory = threading.Event
            stop_events: list[threading.Event] = []

            class StopFixtureExecutor:
                name = "fixture-image"

                def __init__(self) -> None:
                    self.calls: list[str] = []
                    self.lock = threading.Lock()

                def execute(self, request: ExecutionRequest) -> ExecutionResult:
                    config_id = request.payload.output_path.stem
                    with self.lock:
                        self.calls.append(config_id)
                    if config_id == "main_01":
                        self.assert_backoff_started()
                        raise render_failure("render_http_error", http_status=400)
                    raise render_failure("render_http_error", http_status=524)

                @staticmethod
                def assert_backoff_started() -> None:
                    if not backoff_started.wait(timeout=2):
                        raise AssertionError("sibling did not enter backoff")

            delegate = StopFixtureExecutor()

            def capture_event() -> threading.Event:
                event = real_event_factory()
                stop_events.append(event)
                return event

            def fake_sleep(_delay: float) -> None:
                if not stop_events[0].wait(timeout=2):
                    raise AssertionError("terminal sibling did not stop the batch")

            def on_retry(task, *_args) -> None:
                if task.output_path.stem == "main_02":
                    backoff_started.set()

            executor, _plan = self._production_executor(
                root,
                delegate,  # type: ignore[arg-type]
                count=2,
                concurrency=2,
                sleep_fn=fake_sleep,
                on_task_retry=on_retry,
            )
            with mock.patch.object(ipe.threading, "Event", side_effect=capture_event):
                with self.assertRaises(ExecutorExecutionError) as caught:
                    executor.execute(ExecutionRequest(step="renders"))

            self.assertEqual("render_http_error", caught.exception.code)
            self.assertEqual(400, caught.exception.http_status)
            self.assertEqual(1, delegate.calls.count("main_01"))
            self.assertEqual(1, delegate.calls.count("main_02"))

    def test_concurrent_tasks_keep_independent_retry_budgets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scripts = {
                config_id: [
                    render_failure("render_network_error"),
                    render_failure("render_network_error"),
                    None,
                ]
                for config_id in ("main_01", "main_02", "main_03")
            }
            barrier = threading.Barrier(3)

            class ConcurrentScriptedImageExecutor(ScriptedImageExecutor):
                def __init__(self) -> None:
                    super().__init__(scripts)
                    self.initial_seen: set[str] = set()
                    self.initial_lock = threading.Lock()

                def execute(self, request: ExecutionRequest) -> ExecutionResult:
                    config_id = request.payload.output_path.stem
                    with self.initial_lock:
                        first_call = config_id not in self.initial_seen
                        self.initial_seen.add(config_id)
                    if first_call:
                        barrier.wait(timeout=2)
                    return super().execute(request)

            delegate = ConcurrentScriptedImageExecutor()
            executor, _plan = self._production_executor(
                Path(tmp), delegate, count=3, concurrency=3
            )

            result = executor.execute(ExecutionRequest(step="renders"))

            self.assertEqual(3, result.metadata["successful_count"])
            self.assertEqual(
                {"main_01": 3, "main_02": 3, "main_03": 3},
                delegate.counts,
            )

    def test_serial_retry_does_not_reorder_task_start_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            delegate = ScriptedImageExecutor(
                {
                    "main_01": [render_failure("render_timeout"), None],
                    "main_02": [None],
                    "main_03": [None],
                }
            )
            executor, _plan = self._production_executor(
                Path(tmp), delegate, count=3, concurrency=1
            )

            executor.execute(ExecutionRequest(step="renders"))

            self.assertEqual(
                ["main_01", "main_01", "main_02", "main_03"],
                delegate.calls,
            )

    def test_sleep_and_jitter_are_fully_injected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            delegate = ScriptedImageExecutor(
                {
                    "main_01": [
                        render_failure("render_timeout"),
                        render_failure("render_timeout"),
                        None,
                    ]
                }
            )
            jitter_values = iter((1.0, 2.0))
            jitter_calls: list[tuple[float, float]] = []
            sleeps: list[float] = []

            def fake_jitter(minimum: float, maximum: float) -> float:
                jitter_calls.append((minimum, maximum))
                return next(jitter_values)

            executor, _plan = self._production_executor(
                Path(tmp),
                delegate,
                sleep_fn=sleeps.append,
                jitter_fn=fake_jitter,
            )

            executor.execute(ExecutionRequest(step="renders"))

            self.assertEqual([(0.0, 3.0), (0.0, 3.0)], jitter_calls)
            self.assertEqual([6.0, 17.0], sleeps)

    def test_retry_callback_failure_never_changes_render_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            delegate = ScriptedImageExecutor(
                {"main_01": [render_failure("render_network_error"), None]}
            )

            def broken_callback(*_args) -> None:
                raise RuntimeError("journal unavailable")

            executor, _plan = self._production_executor(
                Path(tmp), delegate, on_task_retry=broken_callback
            )
            result = executor.execute(ExecutionRequest(step="renders"))

            self.assertEqual(1, result.metadata["successful_count"])
            self.assertEqual(2, len(delegate.calls))

    def test_render_retry_event_has_closed_fields_and_rejects_invalid_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            journal = root / "events.jsonl"
            task = ImageGenerationTask(
                prompt="safe", output_path=root / "detail_02.png"
            )
            WorkflowProductionService._record_render_retry(
                journal,
                task,
                2,
                "render_http_error",
                429,
                15.6,
            )
            event = read_events(journal)[0]
            self.assertEqual(
                {
                    "event": "render_retry",
                    "config_id": "detail_02",
                    "attempt": 2,
                    "failure_code": "render_http_error",
                    "http_status": 429,
                    "delay_seconds": 16,
                },
                {key: value for key, value in event.items() if key != "ts"},
            )

            invalid_cases = (
                (ImageGenerationTask("safe", root / "bad id.png"), 1, "render_timeout", None, 5.0),
                (task, 0, "render_timeout", None, 5.0),
                (task, 3, "render_timeout", None, 5.0),
                (task, 1, "render_pipeline_error", None, 5.0),
                (task, 1, "render_http_error", 99, 5.0),
                (task, 1, "render_http_error", 600, 5.0),
                (task, 1, "render_timeout", None, -1.0),
                (task, 1, "render_timeout", None, 601.0),
                (task, 1, "render_timeout", None, float("nan")),
            )
            for args in invalid_cases:
                with self.subTest(args=args[1:]):
                    WorkflowProductionService._record_render_retry(journal, *args)
            self.assertEqual(1, len(read_events(journal)))

    def test_default_service_builder_wires_retry_event_without_changing_signature(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = build_final_prompt_bundle(root)
            service = WorkflowProductionService(
                root,
                environment={
                    "RENDER_ALLOW_REAL_EXECUTION": "1",
                    "OPENAI_API_KEY": "fixture-secret",
                },
            )
            with mock.patch.object(
                service, "_expected_ids", return_value=("main_01",)
            ):
                executor = service._build_executor(
                    "renders",
                    bundle.manifest,
                    bundle.manifest_path,
                    lambda _artifact: None,
                )
            self.assertIsNotNone(executor.on_task_retry)
            task = ImageGenerationTask(
                prompt="safe", output_path=bundle.renders_dir / "main_01.png"
            )
            executor.on_task_retry(
                task, 1, "render_network_error", None, 5.0
            )

            journal = (
                bundle.manifest_path.parent
                / f"{bundle.manifest['product_id']}.events.jsonl"
            )
            event = read_events(journal)[0]
            self.assertEqual("render_retry", event["event"])
            self.assertEqual("main_01", event["config_id"])
            self.assertNotIn("http_status", event)

    def test_failure_card_distinguishes_exhausted_retry_from_zero_retry(self) -> None:
        exhausted = render_failure("render_http_error", http_status=524)
        exhausted.transient_retry_attempts = 2
        exhausted.successful_count = 0
        exhausted.planned_count = 1
        exhausted.skipped_count = 0
        retried_message = WorkflowProductionService._safe_failure(exhausted)
        self.assertIn("已自动重试 2 次仍失败", retried_message)
        self.assertIn("HTTP 524", retried_message)
        self.assertNotIn("未自动重试", retried_message)

        zero_retry = render_failure("render_http_error", http_status=400)
        zero_retry_message = WorkflowProductionService._safe_failure(zero_retry)
        self.assertIn(
            "机器已停下，未自动重试，已完成的成果都保留了。",
            zero_retry_message,
        )
        self.assertFalse(hasattr(zero_retry, "transient_retry_attempts"))

    def test_retry_attempt_field_out_of_range_rejects_whole_structured_failure(self) -> None:
        for value in (0, 10):
            with self.subTest(value=value):
                failure = render_failure("render_http_error", http_status=524)
                failure.transient_retry_attempts = value
                self.assertIsNone(
                    WorkflowProductionService._structured_render_failure(failure)
                )

    def test_manual_er02_style_failure_without_retry_field_keeps_original_sentence(self) -> None:
        failure = render_failure("render_http_error", http_status=503)
        failure.provider_error_type = "server_error"
        message = WorkflowProductionService._safe_failure(failure)

        self.assertIn("图片服务返回错误（HTTP 503，类型 server_error）。", message)
        self.assertIn(
            "机器已停下，未自动重试，已完成的成果都保留了。",
            message,
        )
        self.assertNotIn("已自动重试", message)

    def test_step_failed_detail_does_not_duplicate_retry_information(self) -> None:
        without_retry = render_failure("render_http_error", http_status=524)
        with_retry = render_failure("render_http_error", http_status=524)
        with_retry.transient_retry_attempts = 2

        self.assertEqual(
            WorkflowProductionService._safe_event_detail(without_retry),
            WorkflowProductionService._safe_event_detail(with_retry),
        )
        self.assertNotIn(
            "重试", WorkflowProductionService._safe_event_detail(with_retry)
        )

    def test_qc_repair_single_item_uses_the_same_retry_layer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = build_qc_repair_fixture(root)
            prepared = prepare_repair_plan(
                fixture.bundle.manifest,
                fixture.bundle.manifest_path,
                repo_reports_dir=fixture.repo_reports_dir,
            )
            self.assertIsNotNone(prepared.plan)
            order = prepared.plan.work_orders[0]
            single_order_plan = replace(prepared.plan, work_orders=(order,))

            class TransientThenPngExecutor:
                name = "fixture-image"

                def __init__(self) -> None:
                    self.calls: list[str] = []

                def execute(self, request: ExecutionRequest) -> ExecutionResult:
                    config_id = request.payload.output_path.stem
                    self.calls.append(config_id)
                    if len(self.calls) == 1:
                        raise render_failure("render_network_error")
                    from PIL import Image

                    request.payload.output_path.parent.mkdir(
                        parents=True, exist_ok=True
                    )
                    Image.new("RGB", (10, 10), color=(245, 245, 245)).save(
                        request.payload.output_path, format="PNG"
                    )
                    return ExecutionResult(
                        detail="generated",
                        outputs=(request.payload.output_path,),
                        provider=self.name,
                        model="fixture-model",
                    )

            delegate = TransientThenPngExecutor()
            context = ExecutorContext(
                manifest=fixture.bundle.manifest,
                manifest_path=fixture.bundle.manifest_path,
                environment={
                    "RENDER_ALLOW_REAL_EXECUTION": "1",
                    "OPENAI_API_KEY": "fixture-secret",
                },
            )
            repair = QcRepairExecutor(
                context,
                plan=single_order_plan,
                journal_path=root / "repair.events.jsonl",
                request_id="fixture-request",
                image_executor_factory=lambda _context: delegate,
            )
            sleeps: list[float] = []
            built_orders = []
            real_builder = repair._image_production

            def instrumented_builder(current_order):
                built_orders.append(current_order)
                image_production = real_builder(current_order)
                image_production.sleep_fn = sleeps.append
                image_production.jitter_fn = lambda _minimum, _maximum: 0.0
                return image_production

            with mock.patch.object(
                repair,
                "_image_production",
                side_effect=instrumented_builder,
            ):
                result = repair.execute(ExecutionRequest(step="repair"))

            self.assertEqual([order], built_orders)
            self.assertEqual([order.config_id, order.config_id], delegate.calls)
            self.assertEqual([5.0], sleeps)
            self.assertEqual("succeeded", result.metadata["status"])
            self.assertEqual((order.config_id,), result.metadata["succeeded"])
            self.assertEqual((), result.metadata["failed"])
            self.assertEqual((), result.metadata["skipped"])
            self.assertEqual(
                (order.config_id,), tuple(path.stem for path in result.outputs)
            )


if __name__ == "__main__":
    unittest.main()
