from __future__ import annotations

import copy
import json
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
TESTS = ROOT / "tests"
for extra in (BRIDGE, TESTS):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from codex_dev_executor import (  # noqa: E402
    CodexAttachment,
    CodexDevExecutor,
    CodexTurnResult,
)
from executor_contract import (  # noqa: E402
    ExecutionRequest,
    ExecutionResult,
    ExecutorExecutionError,
)
import workflow_production_service as production_service  # noqa: E402
from test_codex_dev_executor import (  # noqa: E402
    CodexDevFixture,
    FakeTransport,
    detail_chunk_turns,
    valid_detail_chunk_responses,
    valid_final_prompt_response,
    valid_main_variable_response,
)
from test_workflow_production_service import FakeCanvasClient  # noqa: E402


FIXED_EVENT_TIME = "2026-08-02T12:00:00"
SERVICE_ARTIFACT_BYTES = b"vd01-stable-artifact\n"
TARGET_STAGE = {
    "main_vc": ("needs_main_variable_configs", "main-variable-config"),
    "detail_vc": ("needs_detail_variable_configs", "detail-variable-config"),
    "final_prompts": ("needs_final_prompts", "final-prompt-compiler"),
}


def _module05_handheld_chunk(chunk: dict[str, object]) -> dict[str, object]:
    result = copy.deepcopy(chunk)
    config = result["configs"][0]  # type: ignore[index]
    overrides = config["per_image_overrides"]  # type: ignore[index]
    overrides["手持交互声明"] = (  # type: ignore[index]
        "本张图启用手持场景。手持子场景类型：静态握持。"
        "单手自然握住把手，不离桌，不倾倒"
    )
    overrides["动态手持样式参考图调用"] = "无，仅动态拿起场景可调用"  # type: ignore[index]
    return result


def _paraphrased_final_prompt_response(mode: str) -> dict[str, object]:
    response = copy.deepcopy(valid_final_prompt_response(mode))
    ratio = "1:1" if mode == "main" else "3:4"
    response["prompts"][0]["final_prompt"] = response["prompts"][0][  # type: ignore[index]
        "final_prompt"
    ].replace(
        f"画布比例固定为 {ratio}",
        f"输出画布比例：{ratio}",
    )
    return response


class _FailOnceTransport(FakeTransport):
    def __init__(self, result: CodexTurnResult) -> None:
        super().__init__(result)
        self.failed = False

    def run_turn(
        self,
        prompt: str,
        attachments: tuple[CodexAttachment, ...],
    ) -> CodexTurnResult:
        self.calls.append((prompt, attachments))
        if not self.failed:
            self.failed = True
            raise RuntimeError("simulated transport failure")
        assert self.results
        return self.results.pop(0)


class _LegacyVcExecutor:
    name = "vd01-fake"

    def __init__(
        self,
        step: str,
        executed: list[str],
        output_path: Path,
        *,
        failure: bool = False,
    ) -> None:
        self.step = step
        self.executed = executed
        self.output_path = output_path
        self.failure = failure

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        self.executed.append(request.step)
        if request.step != self.step:
            raise AssertionError(request.step)
        if self.failure:
            raise ExecutorExecutionError("vd01 stable executor failure")
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_path.write_bytes(SERVICE_ARTIFACT_BYTES)
        return ExecutionResult(
            detail="vd01 stable success",
            outputs=(self.output_path,),
            provider=self.name,
        )


class _HeartbeatVcExecutor(_LegacyVcExecutor):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.turn_progress_callback = None

    def set_turn_progress_callback(self, callback) -> None:
        self.turn_progress_callback = callback

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        if self.turn_progress_callback is None:
            raise AssertionError("turn progress callback was not bound")
        self.turn_progress_callback()
        self.turn_progress_callback()
        return super().execute(request)


class _PendingHeartbeatWorker:
    def __init__(self) -> None:
        self.pending: list[list[dict[str, object]]] = []
        self.delivered: list[list[dict[str, object]]] = []
        self.submitted_before_close = 0
        self.close_calls: list[bool] = []
        self.alive = True

    def submit(self, ops: list[dict[str, object]]) -> None:
        self.pending.append(ops)

    def close(self, *, drain: bool) -> None:
        self.submitted_before_close = len(self.pending)
        self.close_calls.append(drain)
        if drain:
            self.delivered.extend(self.pending)
        self.pending.clear()
        self.alive = False


class TurnProgressExecutorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = CodexDevFixture()

    def test_detail_recovery_and_content_correction_report_every_return_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context, output_path, _ = self.fixture.make_detail_fixture(root)
            chunks = valid_detail_chunk_responses()
            invalid_chunk = _module05_handheld_chunk(chunks[2])
            thread_id = "thread-vd01-detail"
            transport = FakeTransport(
                [
                    CodexTurnResult(text='{"chunk_index": 1', thread_id=thread_id),
                    *detail_chunk_turns(
                        [chunks[0], chunks[1], invalid_chunk, chunks[2], chunks[3]],
                        thread_id=thread_id,
                    ),
                ]
            )
            callback_boundaries: list[tuple[int, int]] = []
            executor = CodexDevExecutor(
                context,
                transport=transport,
                repository_root=root,
            )
            executor.set_content_correction_callback(lambda *_: None)
            executor.set_turn_progress_callback(
                lambda: callback_boundaries.append(
                    (len(transport.calls), len(transport.continuation_calls))
                )
            )

            executor.execute(ExecutionRequest(step="detail_vc"))

            self.assertEqual(
                [(1, 0), (1, 1), (1, 2), (1, 3), (1, 4), (1, 5)],
                callback_boundaries,
            )
            self.assertEqual(6, len(callback_boundaries))
            self.assertTrue(output_path.exists())

    def test_main_content_correction_reports_initial_and_corrected_returns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context, output_path = self.fixture.make_downstream_fixture(root)
            invalid = valid_main_variable_response()
            invalid["configs"][0]["per_image_overrides"]["输出画布比例"] = "3:4"  # type: ignore[index]
            transport = FakeTransport(
                [
                    CodexTurnResult(
                        text=json.dumps(invalid, ensure_ascii=False),
                        thread_id="thread-vd01-main",
                    ),
                    CodexTurnResult(
                        text=json.dumps(valid_main_variable_response(), ensure_ascii=False),
                        thread_id="thread-vd01-main",
                    ),
                ]
            )
            boundaries: list[int] = []
            executor = CodexDevExecutor(context, transport=transport, repository_root=root)
            executor.set_content_correction_callback(lambda *_: None)
            executor.set_turn_progress_callback(
                lambda: boundaries.append(
                    len(transport.calls) + len(transport.continuation_calls)
                )
            )

            executor.execute(ExecutionRequest(step="main_vc"))

            self.assertEqual([1, 2], boundaries)
            self.assertTrue(output_path.exists())

    def test_final_prompt_batches_and_corrections_report_all_four_returns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context, final_dir, _main_path, _detail_path = (
                self.fixture.make_final_prompt_fixture(root)
            )
            transport = FakeTransport(
                [
                    CodexTurnResult(
                        text=json.dumps(
                            _paraphrased_final_prompt_response("main"),
                            ensure_ascii=False,
                        ),
                        thread_id="thread-vd01-final-main",
                    ),
                    CodexTurnResult(
                        text=json.dumps(valid_final_prompt_response("main"), ensure_ascii=False),
                        thread_id="thread-vd01-final-main",
                    ),
                    CodexTurnResult(
                        text=json.dumps(
                            _paraphrased_final_prompt_response("detail"),
                            ensure_ascii=False,
                        ),
                        thread_id="thread-vd01-final-detail",
                    ),
                    CodexTurnResult(
                        text=json.dumps(valid_final_prompt_response("detail"), ensure_ascii=False),
                        thread_id="thread-vd01-final-detail",
                    ),
                ]
            )
            boundaries: list[int] = []
            executor = CodexDevExecutor(context, transport=transport, repository_root=root)
            executor.set_turn_progress_callback(
                lambda: boundaries.append(
                    len(transport.calls) + len(transport.continuation_calls)
                )
            )

            executor.execute(ExecutionRequest(step="final_prompts"))

            self.assertEqual([1, 2, 3, 4], boundaries)
            self.assertTrue((final_dir / "final_prompt_index.json").exists())

    def test_callback_exception_does_not_interrupt_successful_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context, output_path = self.fixture.make_downstream_fixture(root)
            transport = FakeTransport(
                CodexTurnResult(
                    text=json.dumps(valid_main_variable_response(), ensure_ascii=False),
                    thread_id="thread-vd01-callback-failure",
                )
            )
            callback_calls = 0

            def fail_callback() -> None:
                nonlocal callback_calls
                callback_calls += 1
                raise RuntimeError("heartbeat callback stopped")

            executor = CodexDevExecutor(context, transport=transport, repository_root=root)
            executor.set_turn_progress_callback(fail_callback)

            result = executor.execute(ExecutionRequest(step="main_vc"))

            self.assertEqual(1, callback_calls)
            self.assertEqual("主图变量配置已生成", result.detail)
            self.assertTrue(output_path.exists())

    def test_transport_exception_has_no_heartbeat_until_next_successful_return(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context, output_path = self.fixture.make_downstream_fixture(root)
            transport = _FailOnceTransport(
                CodexTurnResult(
                    text=json.dumps(valid_main_variable_response(), ensure_ascii=False),
                    thread_id="thread-vd01-transport-recovery",
                )
            )
            heartbeat_calls: list[str] = []
            executor = CodexDevExecutor(context, transport=transport, repository_root=root)
            executor.set_turn_progress_callback(lambda: heartbeat_calls.append("returned"))

            with self.assertRaises(ExecutorExecutionError) as caught:
                executor.execute(ExecutionRequest(step="main_vc"))

            self.assertEqual(
                "codex-dev 的 Codex 线程执行失败：RuntimeError: simulated transport failure",
                str(caught.exception),
            )
            self.assertEqual([], heartbeat_calls)
            self.assertFalse(output_path.exists())

            executor.execute(ExecutionRequest(step="main_vc"))

            self.assertEqual(["returned"], heartbeat_calls)
            self.assertTrue(output_path.exists())

    def test_bound_and_unbound_executor_paths_match_bytes_results_and_failures(self) -> None:
        def run_success(
            root: Path,
            context,
            output_path: Path,
            bound: bool,
        ) -> tuple[bytes, tuple[object, ...], int]:
            transport = FakeTransport(
                CodexTurnResult(
                    text=json.dumps(valid_main_variable_response(), ensure_ascii=False),
                    thread_id="thread-vd01-equivalence",
                )
            )
            callbacks: list[None] = []
            executor = CodexDevExecutor(context, transport=transport, repository_root=root)
            if bound:
                executor.set_turn_progress_callback(lambda: callbacks.append(None))
            result = executor.execute(ExecutionRequest(step="main_vc"))
            normalized_result = (
                result.detail,
                result.provider,
                result.metadata,
                tuple(path.name for path in result.outputs),
            )
            return output_path.read_bytes(), normalized_result, len(callbacks)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context, output_path = self.fixture.make_downstream_fixture(root)
            unbound_bytes, unbound_result, unbound_callbacks = run_success(
                root,
                context,
                output_path,
                False,
            )
            output_path.unlink()
            bound_bytes, bound_result, bound_callbacks = run_success(
                root,
                context,
                output_path,
                True,
            )

        self.assertEqual(unbound_bytes, bound_bytes)
        self.assertEqual(unbound_result, bound_result)
        self.assertEqual((0, 1), (unbound_callbacks, bound_callbacks))

        def run_failure(bound: bool) -> tuple[str, str, int]:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                context, _output_path = self.fixture.make_downstream_fixture(root)
                transport = FakeTransport(
                    error=RuntimeError("stable transport exception")
                )
                callbacks: list[None] = []
                executor = CodexDevExecutor(context, transport=transport, repository_root=root)
                if bound:
                    executor.set_turn_progress_callback(lambda: callbacks.append(None))
                with self.assertRaises(ExecutorExecutionError) as caught:
                    executor.execute(ExecutionRequest(step="main_vc"))
                return type(caught.exception).__name__, str(caught.exception), len(callbacks)

        unbound_failure = run_failure(False)
        bound_failure = run_failure(True)
        self.assertEqual(unbound_failure[:2], bound_failure[:2])
        self.assertEqual((0, 0), (unbound_failure[2], bound_failure[2]))


class TurnProgressServiceTest(unittest.TestCase):
    def _make_service_fixture(self, root: Path) -> tuple[Path, Path]:
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
        return repository_root, workspace

    @staticmethod
    def _route_reader(step: str, executed: list[str]):
        stage, skill = TARGET_STAGE[step]

        def read(_path: Path) -> dict[str, object]:
            if not executed:
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

        return read

    def _run_service_case(
        self,
        *,
        step: str = "main_vc",
        bound: bool = True,
        failure: bool = False,
        sender_failure: bool = False,
        pending_worker: _PendingHeartbeatWorker | None = None,
    ) -> dict[str, object]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository_root, workspace = self._make_service_fixture(root)
            output_path = workspace / "artifacts" / f"{step}.json"
            client = FakeCanvasClient()
            executed: list[str] = []
            executor_type = _HeartbeatVcExecutor if bound else _LegacyVcExecutor
            executor = executor_type(
                step,
                executed,
                output_path,
                failure=failure,
            )
            ticks = iter(range(1_001, 2_000))
            service = production_service.WorkflowProductionService(
                repository_root,
                client=client,
                executor_builder=lambda _step, _manifest, _path, _on_output: executor,
                route_reader=self._route_reader(step, executed),
                integrity_reader=lambda _route: {
                    "found": False,
                    "status": "",
                    "render_blocked": False,
                },
                artifact_reader=lambda _manifest: (),
                clock_ms=lambda: next(ticks),
                sleep=lambda _seconds: None,
                environment={},
                persistence_timeout_ms=0,
                batch_lock_root=root / "locks",
            )
            sender_calls: list[list[dict[str, object]]] = []
            if sender_failure:
                def fail_sender(ops):
                    sender_calls.append(ops)
                    raise RuntimeError("simulated heartbeat sender failure")

                service._send_qc_heartbeat_once = fail_sender
            if pending_worker is not None:
                def start_pending_worker(_request_id: str):
                    service._qc_heartbeat_workers.add(pending_worker)  # type: ignore[arg-type]
                    return pending_worker

                service._start_qc_heartbeat_worker = start_pending_worker  # type: ignore[method-assign]

            with mock.patch.object(
                production_service.run_controller.time,
                "strftime",
                return_value=FIXED_EVENT_TIME,
            ):
                service.poll_once()

            journal_path = repository_root / "manifests" / "cup.events.jsonl"
            machine_state = copy.deepcopy(
                client.state["nodes"][0]["metadata"]["workflowProduction"]
            )
            result = {
                "artifact": output_path.read_bytes() if output_path.exists() else None,
                "journal": journal_path.read_bytes(),
                "machine_state": machine_state,
                "ops": copy.deepcopy(client.ops),
                "worker_count": len(service._qc_heartbeat_workers),
                "sender_calls": sender_calls,
                "live_threads": tuple(
                    thread.name
                    for thread in threading.enumerate()
                    if thread.is_alive() and thread.name.startswith("qc-heartbeat-req-001")
                ),
            }
            return result

    @staticmethod
    def _running_step_states(result: dict[str, object], step: str) -> list[dict]:
        return [
            op["metadata"]["workflowProduction"]
            for operation_batch in result["ops"]
            for op in operation_batch
            if op.get("type") == "update_node"
            and (op.get("metadata") or {}).get("workflowProduction", {}).get("status")
            == "running"
            and (op.get("metadata") or {}).get("workflowProduction", {}).get("step")
            == step
        ]

    def test_each_target_step_refreshes_only_updated_at_and_drains_on_success(self) -> None:
        for step in TARGET_STAGE:
            with self.subTest(step=step):
                result = self._run_service_case(step=step)
                states = self._running_step_states(result, step)
                self.assertEqual(3, len(states))
                self.assertEqual(
                    sorted(state["updatedAt"] for state in states),
                    [state["updatedAt"] for state in states],
                )
                self.assertEqual(
                    len(states),
                    len({state["updatedAt"] for state in states}),
                )
                stable_states = []
                for state in states:
                    stable = copy.deepcopy(state)
                    stable.pop("updatedAt")
                    stable_states.append(stable)
                self.assertEqual([stable_states[0]] * 3, stable_states)
                self.assertEqual(["running"] * 3, [state["status"] for state in states])
                self.assertEqual([0] * 3, [state["producedCount"] for state in states])
                self.assertTrue(all(state["message"] for state in states))
                self.assertEqual(SERVICE_ARTIFACT_BYTES, result["artifact"])
                self.assertEqual(0, result["worker_count"])
                self.assertEqual((), result["live_threads"])

    def test_failure_discards_pending_heartbeats_before_failed_state(self) -> None:
        worker = _PendingHeartbeatWorker()

        result = self._run_service_case(failure=True, pending_worker=worker)

        self.assertEqual(2, worker.submitted_before_close)
        self.assertEqual([False], worker.close_calls)
        self.assertEqual([], worker.pending)
        self.assertEqual([], worker.delivered)
        self.assertEqual(0, result["worker_count"])
        self.assertEqual("failed", result["machine_state"]["status"])
        self.assertEqual([], self._running_step_states(result, "main_vc")[1:])

    def test_sender_exception_is_silent_and_main_flow_completes(self) -> None:
        result = self._run_service_case(sender_failure=True)

        self.assertEqual(2, len(result["sender_calls"]))
        self.assertEqual(SERVICE_ARTIFACT_BYTES, result["artifact"])
        self.assertEqual("completed", result["machine_state"]["status"])
        self.assertEqual(0, result["worker_count"])
        self.assertEqual((), result["live_threads"])

    def test_bound_and_unbound_service_paths_match_artifact_events_and_failure_text(self) -> None:
        unbound_success = self._run_service_case(bound=False)
        bound_success = self._run_service_case(bound=True)
        self.assertEqual(unbound_success["artifact"], bound_success["artifact"])
        self.assertEqual(unbound_success["journal"], bound_success["journal"])

        unbound_failure = self._run_service_case(bound=False, failure=True)
        bound_failure = self._run_service_case(bound=True, failure=True)
        self.assertEqual(unbound_failure["journal"], bound_failure["journal"])
        self.assertEqual(
            unbound_failure["machine_state"]["errorMessage"],
            bound_failure["machine_state"]["errorMessage"],
        )
        unbound_events = [
            json.loads(line)
            for line in unbound_failure["journal"].decode("utf-8").splitlines()
        ]
        bound_events = [
            json.loads(line)
            for line in bound_failure["journal"].decode("utf-8").splitlines()
        ]
        self.assertEqual(unbound_events, bound_events)
        self.assertEqual(
            "vd01 stable executor failure",
            unbound_events[-1]["detail"],
        )


if __name__ == "__main__":
    unittest.main()
