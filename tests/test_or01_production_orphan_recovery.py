from __future__ import annotations

import ast
import collections
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

import production_orphan_recovery  # noqa: E402
from workflow_batch_status import (  # noqa: E402
    WorkflowBatchStatusError,
    build_workflow_batch_status,
    build_workflow_batch_status_from_events,
)


class Or01ProductionOrphanRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.repository_root = Path(self.temp.name) / "repo"
        self.manifests = self.repository_root / "manifests"
        self.manifests.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _event(timestamp: str, event: str, **fields: Any) -> dict[str, Any]:
        return {"ts": timestamp, "event": event, **fields}

    def _ledger(self, batch_id: str) -> Path:
        return self.manifests / f"{batch_id}.events.jsonl"

    def _write_events(self, batch_id: str, events: list[dict[str, Any]]) -> Path:
        ledger = self._ledger(batch_id)
        ledger.write_text(
            "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events),
            encoding="utf-8",
        )
        return ledger

    def _running_events(
        self,
        *,
        request_id: str = "req-orphan",
        step: str = "detail_vc",
    ) -> list[dict[str, Any]]:
        return [
            self._event(
                "2020-01-01T00:00:00",
                "command_received",
                request_id=request_id,
                command="workflow-production",
            ),
            self._event(
                "2020-01-01T00:00:01",
                "step_started",
                request_id=request_id,
                step=step,
            ),
        ]

    def test_running_orphan_is_failed_with_original_request_and_preserved_history(self) -> None:
        batch_id = "杯子_20260822_181856"
        ledger = self._write_events(
            batch_id,
            [
                self._event(
                    "2019-12-31T23:59:57",
                    "command_received",
                    request_id="req-closed",
                    command="workflow-production",
                ),
                self._event(
                    "2019-12-31T23:59:58",
                    "step_started",
                    request_id="req-closed",
                    step="identity",
                ),
                self._event(
                    "2019-12-31T23:59:59",
                    "step_failed",
                    request_id="req-closed",
                    step="identity",
                    detail="旧请求已终结",
                    failure_code="previous_failure",
                ),
                *self._running_events(request_id="req-current"),
            ],
        )
        before = ledger.read_bytes()

        results = production_orphan_recovery.recover_orphaned_productions(
            self.repository_root
        )

        self.assertEqual(
            [
                {
                    "batch_id": batch_id,
                    "recovered": True,
                    "skipped": False,
                    "reason": "recovered",
                }
            ],
            results,
        )
        after = ledger.read_bytes()
        self.assertTrue(after.startswith(before))
        self.assertGreater(len(after), len(before))
        written_events = [
            json.loads(line)
            for line in ledger.read_text(encoding="utf-8").splitlines()
        ]
        terminal = written_events[-1]
        self.assertEqual("step_failed", terminal["event"])
        self.assertEqual("req-current", terminal["request_id"])
        self.assertEqual(
            "workbench_restart_interrupted",
            terminal["failure_code"],
        )
        self.assertIn("已完成成果均已保留", terminal["detail"])
        self.assertIn("可重新开始", terminal["detail"])
        summary = build_workflow_batch_status(self.repository_root, batch_id)
        self.assertEqual("failed", summary["status"])
        self.assertEqual("detail_vc", summary["currentStage"])
        self.assertEqual(
            "workbench_restart_interrupted",
            summary["failureCode"],
        )

    def test_terminal_queued_empty_invalid_and_legacy_running_ledgers_are_unchanged(self) -> None:
        cases: dict[str, bytes] = {}

        completed_id = "completed_batch"
        cases[completed_id] = self._write_events(
            completed_id,
            [
                *self._running_events(request_id="req-completed", step="qc"),
                self._event(
                    "2020-01-01T00:00:02",
                    "step_succeeded",
                    request_id="req-completed",
                    step="qc",
                    detail="完成",
                ),
            ],
        ).read_bytes()

        failed_id = "failed_batch"
        cases[failed_id] = self._write_events(
            failed_id,
            [
                *self._running_events(request_id="req-failed"),
                self._event(
                    "2020-01-01T00:00:02",
                    "step_failed",
                    request_id="req-failed",
                    detail="已失败",
                    failure_code="existing_failure",
                ),
            ],
        ).read_bytes()

        paused_id = "paused_batch"
        cases[paused_id] = self._write_events(
            paused_id,
            [
                *self._running_events(request_id="req-paused", step="final_prompts"),
                self._event(
                    "2020-01-01T00:00:02",
                    "step_succeeded",
                    request_id="req-paused",
                    step="final_prompts",
                    detail="完成",
                ),
                self._event(
                    "2020-01-01T00:00:03",
                    "production_paused",
                    request_id="req-paused",
                    produced_count=0,
                    reason="awaiting_render_gate",
                ),
            ],
        ).read_bytes()

        queued_id = "queued_batch"
        cases[queued_id] = self._write_events(
            queued_id,
            [
                self._event(
                    "2020-01-01T00:00:00",
                    "command_received",
                    request_id="req-queued",
                    command="workflow-production",
                )
            ],
        ).read_bytes()

        legacy_id = "legacy_running_batch"
        cases[legacy_id] = self._write_events(
            legacy_id,
            [
                self._event(
                    "2020-01-01T00:00:00",
                    "step_started",
                    command="run: detail_vc",
                    step="detail_vc",
                )
            ],
        ).read_bytes()

        empty_id = "empty_batch"
        empty_ledger = self._ledger(empty_id)
        empty_ledger.write_bytes(b"")
        cases[empty_id] = b""

        invalid_id = "invalid_batch"
        invalid_ledger = self._ledger(invalid_id)
        invalid_ledger.write_bytes(b"not-json\n")
        cases[invalid_id] = invalid_ledger.read_bytes()

        results = production_orphan_recovery.recover_orphaned_productions(
            self.repository_root
        )
        by_batch = {result["batch_id"]: result for result in results}

        for batch_id, before in cases.items():
            with self.subTest(batch_id=batch_id):
                self.assertEqual(before, self._ledger(batch_id).read_bytes())
                self.assertFalse(by_batch[batch_id]["recovered"])
                self.assertTrue(by_batch[batch_id]["skipped"])
        self.assertEqual("missing_modern_request_id", by_batch[legacy_id]["reason"])
        self.assertEqual("status_error", by_batch[empty_id]["reason"])
        self.assertEqual("status_error", by_batch[invalid_id]["reason"])
        for batch_id in (completed_id, failed_id, paused_id, queued_id):
            self.assertEqual("not_running", by_batch[batch_id]["reason"])

    def test_dry_run_non_failed_and_error_are_fail_closed(self) -> None:
        for dry_run_mode in ("not_failed", "error"):
            with self.subTest(dry_run_mode=dry_run_mode):
                batch_id = f"dry_run_{dry_run_mode}"
                ledger = self._write_events(batch_id, self._running_events())
                before = ledger.read_bytes()
                if dry_run_mode == "not_failed":
                    dry_run = mock.patch.object(
                        production_orphan_recovery,
                        "build_workflow_batch_status_from_events",
                        return_value={"status": "running"},
                    )
                    expected_reason = "dry_run_not_failed"
                else:
                    dry_run = mock.patch.object(
                        production_orphan_recovery,
                        "build_workflow_batch_status_from_events",
                        side_effect=WorkflowBatchStatusError(409, "pathological"),
                    )
                    expected_reason = "dry_run_error"
                with dry_run, self.assertLogs(
                    production_orphan_recovery.LOGGER,
                    level="WARNING",
                ):
                    results = production_orphan_recovery.recover_orphaned_productions(
                        self.repository_root
                    )
                matching = [
                    result for result in results if result["batch_id"] == batch_id
                ]
                self.assertEqual(expected_reason, matching[0]["reason"])
                self.assertEqual(before, ledger.read_bytes())

    def test_recovery_is_idempotent(self) -> None:
        batch_id = "idempotent_batch"
        ledger = self._write_events(batch_id, self._running_events())

        first = production_orphan_recovery.recover_orphaned_productions(
            self.repository_root
        )
        after_first = ledger.read_bytes()
        second = production_orphan_recovery.recover_orphaned_productions(
            self.repository_root
        )

        self.assertTrue(first[0]["recovered"])
        self.assertFalse(second[0]["recovered"])
        self.assertEqual("not_running", second[0]["reason"])
        self.assertEqual(after_first, ledger.read_bytes())

    def test_dry_run_and_append_share_one_captured_timestamp(self) -> None:
        batch_id = "single_timestamp_batch"
        ledger = self._write_events(batch_id, self._running_events())
        observed: dict[str, str] = {}
        real_builder = build_workflow_batch_status_from_events

        def observe_candidate(
            candidate_batch_id: str,
            candidate_events: Any,
        ) -> dict[str, Any]:
            materialized = list(candidate_events)
            observed["candidate_ts"] = materialized[-1]["ts"]
            return real_builder(candidate_batch_id, materialized)

        with (
            mock.patch.object(
                production_orphan_recovery,
                "strftime",
                return_value="2030-01-02T03:04:05",
            ) as clock,
            mock.patch.object(
                production_orphan_recovery,
                "build_workflow_batch_status_from_events",
                side_effect=observe_candidate,
            ),
        ):
            results = production_orphan_recovery.recover_orphaned_productions(
                self.repository_root
            )

        clock.assert_called_once_with("%Y-%m-%dT%H:%M:%S")
        self.assertTrue(results[0]["recovered"])
        terminal = json.loads(ledger.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual("2030-01-02T03:04:05", observed["candidate_ts"])
        self.assertEqual(observed["candidate_ts"], terminal["ts"])

    def test_memory_builder_matches_disk_and_retains_input_guards(self) -> None:
        batch_id = "memory_builder_batch"
        events = self._running_events()
        self._write_events(batch_id, events)

        self.assertEqual(
            build_workflow_batch_status(self.repository_root, batch_id),
            build_workflow_batch_status_from_events(batch_id, events),
        )
        self.assertEqual(
            build_workflow_batch_status(self.repository_root, batch_id),
            build_workflow_batch_status_from_events(
                batch_id,
                (event for event in events),
            ),
        )

        rejected_inputs = (
            ("../unsafe", events),
            (batch_id, []),
            (batch_id, (event for event in ())),
            (batch_id, [collections.UserDict(events[0]), events[1]]),
            (batch_id, [{"ts": "bad", "event": "step_started"}]),
            (batch_id, [{**events[0], "ignored": object()}, events[1]]),
            (batch_id, [{**events[0], "ignored": (1, 2)}, events[1]]),
            (
                batch_id,
                [
                    {**events[0], "ignored": collections.UserDict({"key": "value"})},
                    events[1],
                ],
            ),
            (
                batch_id,
                [
                    self._event("2020-01-01T00:00:01", "ignored"),
                    self._event("2020-01-01T00:00:00", "ignored"),
                ],
            ),
            (
                batch_id,
                [
                    *events,
                    self._event(
                        "2020-01-01T00:00:02",
                        "step_failed",
                        request_id="req-orphan",
                        detail="\ud800",
                        failure_code="invalid\udfff",
                    ),
                ],
            ),
        )
        for rejected_batch_id, rejected_events in rejected_inputs:
            with self.subTest(
                batch_id=rejected_batch_id,
                events=rejected_events,
            ):
                with self.assertRaises(WorkflowBatchStatusError):
                    build_workflow_batch_status_from_events(
                        rejected_batch_id,
                        rejected_events,
                    )

        # A constructed dict cannot express duplicate keys. The disk boundary
        # must therefore retain the unique-key parser that protects this case.
        self._ledger(batch_id).write_text(
            '{"ts":"2020-01-01T00:00:00","event":"command_received",'
            '"request_id":"first","request_id":"second",'
            '"command":"workflow-production"}\n',
            encoding="utf-8",
        )
        with self.assertRaises(WorkflowBatchStatusError):
            build_workflow_batch_status(self.repository_root, batch_id)

    def test_disk_builder_preserves_first_semantic_error_priority(self) -> None:
        batch_id = "error_priority_batch"
        first_semantic_error = '{"ts":"bad","event":"command_received"}'
        second_line_cases = (
            "not-json",
            '{"ts":"2020-01-01T00:00:01","event":"command_received",'
            '"request_id":"first","request_id":"second",'
            '"command":"workflow-production"}',
        )
        expected = "批次状态账本第 1 行损坏，无法确认制作状态。"

        for second_line in second_line_cases:
            with self.subTest(second_line=second_line[:20]):
                self._ledger(batch_id).write_text(
                    first_semantic_error + "\n" + second_line + "\n",
                    encoding="utf-8",
                )
                with self.assertRaises(WorkflowBatchStatusError) as captured:
                    build_workflow_batch_status(self.repository_root, batch_id)
                self.assertEqual(expected, str(captured.exception))

        with self.assertRaises(WorkflowBatchStatusError) as memory_captured:
            build_workflow_batch_status_from_events(
                batch_id,
                [
                    {"ts": "bad", "event": "command_received"},
                    {"ts": "2020-01-01T00:00:01", "event": "ignored"},
                ],
            )
        self.assertEqual(expected, str(memory_captured.exception))

    def test_memory_builder_matches_disk_python_json_codec_domain(self) -> None:
        batch_id = "codec_domain_batch"
        nested: Any = "leaf"
        for _ in range(110):
            nested = [nested]
        ignored_values = (
            float("nan"),
            "\ud800",
            nested,
        )

        for ignored in ignored_values:
            with self.subTest(ignored_type=type(ignored).__name__):
                events = [
                    {**self._running_events()[0], "ignored": ignored},
                    self._running_events()[1],
                ]
                self._ledger(batch_id).write_text(
                    "".join(
                        json.dumps(event, ensure_ascii=True) + "\n"
                        for event in events
                    ),
                    encoding="utf-8",
                )
                disk_summary = build_workflow_batch_status(
                    self.repository_root,
                    batch_id,
                )
                memory_summary = build_workflow_batch_status_from_events(
                    batch_id,
                    events,
                )
                self.assertEqual("running", disk_summary["status"])
                self.assertEqual(disk_summary, memory_summary)

    def test_memory_builder_matches_disk_codec_failures_and_rejects_cycles(self) -> None:
        batch_id = "codec_failure_batch"
        huge_digits = "9" * 5_000
        huge_integer = 10**4_999
        events = self._running_events()
        memory_events = [{**events[0], "ignored": huge_integer}, events[1]]
        with self.assertRaises(WorkflowBatchStatusError):
            build_workflow_batch_status_from_events(batch_id, memory_events)

        self._ledger(batch_id).write_text(
            '{"ts":"2020-01-01T00:00:00","event":"command_received",'
            '"request_id":"req-orphan","command":"workflow-production",'
            f'"ignored":{huge_digits}}}\n'
            + json.dumps(events[1])
            + "\n",
            encoding="utf-8",
        )
        with self.assertRaises(WorkflowBatchStatusError):
            build_workflow_batch_status(self.repository_root, batch_id)

        recursive_list: list[Any] = []
        recursive_list.append(recursive_list)
        recursive_dict: dict[str, Any] = {}
        recursive_dict["self"] = recursive_dict
        for recursive_value in (recursive_list, recursive_dict):
            with self.subTest(recursive_type=type(recursive_value).__name__):
                with self.assertRaises(WorkflowBatchStatusError):
                    build_workflow_batch_status_from_events(
                        batch_id,
                        [{**events[0], "ignored": recursive_value}, events[1]],
                    )

    def test_failure_code_literal_and_mount_order_are_anchored(self) -> None:
        recovery_source = (BRIDGE / "production_orphan_recovery.py").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            'WORKBENCH_RESTART_FAILURE_CODE = "workbench_restart_interrupted"',
            recovery_source,
        )

        workbench_source = (BRIDGE / "canvas_workbench_service.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(workbench_source)
        command = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "cmd_serve_canvas_workbench"
        )
        startup_with = next(
            node for node in command.body if isinstance(node, ast.With)
        )
        first_statement = startup_with.body[0]
        self.assertIsInstance(first_statement, ast.Expr)
        self.assertEqual(
            "production_orphan_recovery.recover_orphaned_productions(repo_root)",
            ast.unparse(first_statement.value),
        )
        constructor_indices = [
            index
            for index, statement in enumerate(startup_with.body)
            if any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr.endswith(("Service", "Server"))
                for node in ast.walk(statement)
            )
        ]
        self.assertTrue(constructor_indices)
        self.assertTrue(all(index > 0 for index in constructor_indices))


if __name__ == "__main__":
    unittest.main()
