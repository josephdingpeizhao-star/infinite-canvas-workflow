from __future__ import annotations

import email.message
import io
import json
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from typing import Any
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

from workflow_batch_status import (  # noqa: E402
    WorkflowBatchStatusError,
    build_workflow_batch_status,
)
from workflow_production_http_server import WorkflowProductionHttpServer  # noqa: E402


class Dc01BatchStatusEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.repository_root = Path(self.temp.name) / "repo"
        self.manifests_root = self.repository_root / "manifests"
        self.manifests_root.mkdir(parents=True)
        self.batch_id = "杯子_20260821_112812"
        self.ledger = self.manifests_root / f"{self.batch_id}.events.jsonl"
        # Constructing the server does not bind a port; handler tests below are
        # in-memory so the DC-01 suite remains offline and service-free.
        self.server = WorkflowProductionHttpServer(
            repository_root=self.repository_root,
            token="canvas-token",
            host="127.0.0.1",
            port=0,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _event(timestamp: str, event: str, **fields: Any) -> dict[str, Any]:
        return {"ts": timestamp, "event": event, **fields}

    def _persisted(
        self,
        timestamp: str,
        request_id: str,
        config_id: str,
        *,
        backfilled: bool = False,
    ) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "request_id": request_id,
            "config_id": config_id,
            "source": "renders",
            "sha256": "a" * 64,
            "byte_count": 128,
            "width": 1024,
            "height": 1024,
        }
        if backfilled:
            fields["backfilled"] = True
        return self._event(timestamp, "image_persisted", **fields)

    def _write_events(self, events: list[dict[str, Any]]) -> None:
        self.ledger.write_text(
            "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events),
            encoding="utf-8",
        )

    @staticmethod
    def _snapshot(root: Path) -> tuple[tuple[str, ...], tuple[tuple[str, bytes, int], ...]]:
        directories: list[str] = []
        files: list[tuple[str, bytes, int]] = []
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
            relative = path.relative_to(root).as_posix()
            if path.is_dir():
                directories.append(relative)
            elif path.is_file():
                files.append((relative, path.read_bytes(), path.stat().st_mtime_ns))
        return tuple(directories), tuple(files)

    def _get(
        self,
        path: str,
        *,
        token: str | None = "canvas-token",
        origin: str | None = None,
    ) -> tuple[int, dict[str, str], dict[str, Any]]:
        headers = email.message.Message()
        if token is not None:
            headers["x-canvas-agent-token"] = token
        if origin is not None:
            headers["Origin"] = origin

        handler_type = self.server._handler_type()
        handler = handler_type.__new__(handler_type)
        handler.path = path
        handler.headers = headers
        handler.rfile = io.BytesIO()
        handler.wfile = io.BytesIO()
        statuses: list[int] = []
        response_headers: dict[str, str] = {}
        handler.send_response = statuses.append
        handler.send_header = response_headers.__setitem__
        handler.end_headers = lambda: None

        handler.do_GET()

        self.assertEqual(1, len(statuses))
        return (
            statuses[0],
            response_headers,
            json.loads(handler.wfile.getvalue().decode("utf-8")),
        )

    def _status_path(self, batch_id: str | None = None) -> str:
        encoded = urllib.parse.quote(batch_id or self.batch_id, safe="")
        return f"/workflow-production/{encoded}/status"

    def _assert_rejected(
        self,
        events: list[dict[str, Any]],
        *,
        message_contains: str | None = None,
    ) -> None:
        self._write_events(events)
        before = self._snapshot(self.repository_root)
        with self.assertRaises(WorkflowBatchStatusError) as captured:
            build_workflow_batch_status(self.repository_root, self.batch_id)
        self.assertEqual(409, captured.exception.http_status)
        if message_contains is not None:
            self.assertIn(message_contains, str(captured.exception))
        self.assertEqual(before, self._snapshot(self.repository_root))

    def test_each_stage_reports_running_and_ended_timestamps(self) -> None:
        steps = (
            "identity",
            "style_master",
            "angle_inventory",
            "main_vc",
            "detail_vc",
            "final_prompts",
            "integrity",
            "renders",
            "qc",
        )
        for step in steps:
            with self.subTest(step=step, state="running"):
                self._write_events(
                    [
                        self._event(
                            "2026-08-21T11:28:18",
                            "command_received",
                            request_id="req-1",
                            command="workflow-production",
                        ),
                        self._event(
                            "2026-08-21T11:28:19",
                            "step_started",
                            request_id="req-1",
                            step=step,
                        ),
                    ]
                )
                summary = build_workflow_batch_status(
                    self.repository_root,
                    self.batch_id,
                )
                self.assertEqual("running", summary["status"])
                self.assertEqual(step, summary["currentStage"])
                self.assertEqual("2026-08-21T11:28:19", summary["stageStartedAt"])
                self.assertIsNone(summary["stageEndedAt"])
                self.assertEqual(
                    {"completedCount": 0, "plannedCount": None},
                    summary["renders"],
                )

            with self.subTest(step=step, state="ended"):
                self._write_events(
                    [
                        self._event(
                            "2026-08-21T11:28:18",
                            "command_received",
                            request_id="req-1",
                            command="workflow-production",
                        ),
                        self._event(
                            "2026-08-21T11:28:19",
                            "step_started",
                            request_id="req-1",
                            step=step,
                        ),
                        self._event(
                            "2026-08-21T11:29:20",
                            "step_succeeded",
                            request_id="req-1",
                            step=step,
                            detail="完成",
                        ),
                    ]
                )
                summary = build_workflow_batch_status(
                    self.repository_root,
                    self.batch_id,
                )
                self.assertEqual("completed" if step == "qc" else "running", summary["status"])
                self.assertEqual(step, summary["currentStage"])
                self.assertEqual("2026-08-21T11:28:19", summary["stageStartedAt"])
                self.assertEqual("2026-08-21T11:29:20", summary["stageEndedAt"])

    def test_queued_and_paused_states_keep_unknown_plan_explicit(self) -> None:
        self._write_events(
            [
                self._event(
                    "2026-08-21T11:28:18",
                    "command_received",
                    request_id="req-1",
                    command="workflow-production",
                )
            ]
        )
        queued = build_workflow_batch_status(self.repository_root, self.batch_id)
        self.assertEqual("queued", queued["status"])
        self.assertIsNone(queued["currentStage"])
        self.assertIsNone(queued["stageStartedAt"])
        self.assertIsNone(queued["stageEndedAt"])
        self.assertEqual(
            {"completedCount": 0, "plannedCount": None},
            queued["renders"],
        )

        gate_cases = (
            (
                [
                    self._event(
                        "2026-08-21T11:28:19",
                        "step_started",
                        request_id="req-1",
                        step="final_prompts",
                    ),
                    self._event(
                        "2026-08-21T11:29:20",
                        "step_succeeded",
                        request_id="req-1",
                        step="final_prompts",
                        detail="完成",
                    ),
                ],
                "integrity",
            ),
            (
                [
                    self._event(
                        "2026-08-21T11:28:19",
                        "step_started",
                        request_id="req-1",
                        step="final_prompts",
                    ),
                    self._event(
                        "2026-08-21T11:29:20",
                        "step_succeeded",
                        request_id="req-1",
                        step="final_prompts",
                        detail="完成",
                    ),
                    self._event(
                        "2026-08-21T11:29:21",
                        "step_started",
                        request_id="req-1",
                        step="integrity",
                    ),
                    self._event(
                        "2026-08-21T11:29:22",
                        "step_succeeded",
                        request_id="req-1",
                        step="integrity",
                        detail="完成",
                    ),
                ],
                "renders",
            ),
        )
        for preceding_events, expected_stage in gate_cases:
            with self.subTest(awaiting_stage=expected_stage):
                pause_timestamp = "2026-08-21T11:29:23"
                self._write_events(
                    [
                        self._event(
                            "2026-08-21T11:28:18",
                            "command_received",
                            request_id="req-1",
                            command="workflow-production",
                        ),
                        *preceding_events,
                        self._event(
                            pause_timestamp,
                            "production_paused",
                            request_id="req-1",
                            produced_count=0,
                            reason="awaiting_render_gate",
                        ),
                    ]
                )
                paused = build_workflow_batch_status(
                    self.repository_root,
                    self.batch_id,
                )
                self.assertEqual("paused", paused["status"])
                self.assertEqual(expected_stage, paused["currentStage"])
                self.assertIsNone(paused["stageStartedAt"])
                self.assertIsNone(paused["stageEndedAt"])
                self.assertEqual(
                    {"completedCount": 0, "plannedCount": None},
                    paused["renders"],
                )

    def test_partial_render_pause_follows_the_real_step_order(self) -> None:
        self._write_events(
            [
                self._event(
                    "2026-08-21T11:28:18",
                    "command_received",
                    request_id="req-1",
                    command="workflow-production",
                ),
                self._event(
                    "2026-08-21T11:48:01",
                    "step_started",
                    request_id="req-1",
                    step="renders",
                ),
                self._event(
                    "2026-08-21T11:49:00",
                    "step_succeeded",
                    request_id="req-1",
                    step="renders",
                    detail="成功 1/计划 2（跳过 0）",
                ),
                self._event(
                    "2026-08-21T11:49:01",
                    "production_paused",
                    request_id="req-1",
                    produced_count=1,
                ),
            ]
        )

        paused = build_workflow_batch_status(self.repository_root, self.batch_id)

        self.assertEqual("paused", paused["status"])
        self.assertEqual("renders", paused["currentStage"])
        self.assertEqual("2026-08-21T11:48:01", paused["stageStartedAt"])
        self.assertEqual("2026-08-21T11:49:01", paused["stageEndedAt"])
        self.assertEqual(
            {"completedCount": 1, "plannedCount": 2},
            paused["renders"],
        )

    def test_completed_status_counts_unique_images_and_ignores_render_retry(self) -> None:
        self._write_events(
            [
                self._event(
                    "2026-08-21T11:48:00",
                    "command_received",
                    request_id="req-1",
                    command="workflow-production",
                ),
                self._event(
                    "2026-08-21T11:48:01",
                    "step_started",
                    request_id="req-1",
                    step="renders",
                ),
                self._persisted(
                    "2026-08-21T11:48:02",
                    "req-1",
                    "main_01",
                ),
                self._event(
                    "2026-08-21T11:48:03",
                    "render_retry",
                    config_id="detail_01",
                    attempt=1,
                    failure_code="render_timeout",
                    delay_seconds=5,
                ),
                self._persisted(
                    "2026-08-21T11:48:04",
                    "req-1",
                    "detail_01",
                ),
                self._persisted(
                    "2026-08-21T11:48:05",
                    "req-1",
                    "detail_01",
                    backfilled=True,
                ),
                self._event(
                    "2026-08-21T11:49:00",
                    "step_succeeded",
                    request_id="req-1",
                    step="renders",
                    detail="成功 2/计划 2（跳过 0）",
                ),
                self._event(
                    "2026-08-21T11:49:01",
                    "production_completed",
                    request_id="req-1",
                    produced_count=2,
                ),
            ]
        )

        summary = build_workflow_batch_status(self.repository_root, self.batch_id)

        self.assertEqual("completed", summary["status"])
        self.assertEqual("renders", summary["currentStage"])
        self.assertEqual("2026-08-21T11:48:01", summary["stageStartedAt"])
        self.assertEqual("2026-08-21T11:49:00", summary["stageEndedAt"])
        self.assertEqual(
            {"completedCount": 2, "plannedCount": 2},
            summary["renders"],
        )
        self.assertNotIn("failureCode", summary)
        self.assertNotIn("message", summary)

    def test_failed_status_preserves_truth_fields_and_fixed_render_counts(self) -> None:
        failure_message = "渲染失败：HTTP 524；成功 1/计划 3/跳过 0"
        self._write_events(
            [
                self._event(
                    "2026-08-21T11:48:00",
                    "command_received",
                    request_id="req-1",
                    command="workflow-production",
                ),
                self._event(
                    "2026-08-21T11:48:01",
                    "step_started",
                    request_id="req-1",
                    step="renders",
                ),
                self._event(
                    "2026-08-21T11:48:02",
                    "render_retry",
                    config_id="main_02",
                    attempt=1,
                    failure_code="render_http_error",
                    http_status=524,
                    delay_seconds=5,
                ),
                self._persisted(
                    "2026-08-21T11:48:03",
                    "req-1",
                    "main_01",
                ),
                self._event(
                    "2026-08-21T11:50:00",
                    "step_failed",
                    request_id="req-1",
                    detail=failure_message,
                    failure_code="render_http_error",
                ),
            ]
        )

        summary = build_workflow_batch_status(self.repository_root, self.batch_id)

        self.assertEqual("failed", summary["status"])
        self.assertEqual("renders", summary["currentStage"])
        self.assertEqual("2026-08-21T11:48:01", summary["stageStartedAt"])
        self.assertEqual("2026-08-21T11:50:00", summary["stageEndedAt"])
        self.assertEqual("render_http_error", summary["failureCode"])
        self.assertEqual(failure_message, summary["message"])
        self.assertEqual(
            {"completedCount": 1, "plannedCount": 3},
            summary["renders"],
        )

    def test_latest_request_supersedes_historical_failure(self) -> None:
        self._write_events(
            [
                self._event(
                    "2026-08-21T11:00:00",
                    "command_received",
                    request_id="req-old",
                    command="workflow-production",
                ),
                self._event(
                    "2026-08-21T11:00:01",
                    "step_started",
                    request_id="req-old",
                    step="renders",
                ),
                self._event(
                    "2026-08-21T11:01:00",
                    "step_failed",
                    request_id="req-old",
                    detail="渲染失败：HTTP 524；成功 0/计划 2/跳过 0",
                    failure_code="render_http_error",
                ),
                self._event(
                    "2026-08-21T11:10:00",
                    "command_received",
                    request_id="req-new",
                    command="workflow-production",
                ),
                self._event(
                    "2026-08-21T11:10:01",
                    "step_started",
                    request_id="req-new",
                    step="renders",
                ),
                self._event(
                    "2026-08-21T11:11:00",
                    "step_succeeded",
                    request_id="req-new",
                    step="renders",
                    detail="成功 2/计划 2（跳过 0）",
                ),
                self._event(
                    "2026-08-21T11:11:01",
                    "production_completed",
                    request_id="req-new",
                    produced_count=2,
                ),
            ]
        )

        summary = build_workflow_batch_status(self.repository_root, self.batch_id)

        self.assertEqual("completed", summary["status"])
        self.assertNotIn("failureCode", summary)
        self.assertNotIn("message", summary)
        self.assertEqual(2, summary["renders"]["plannedCount"])

    def test_completed_status_survives_a_sync_only_followup_command(self) -> None:
        events = [
            self._event(
                "2026-08-21T11:00:00",
                "command_received",
                request_id="req-original",
                command="workflow-production",
            ),
            self._event(
                "2026-08-21T11:00:01",
                "step_started",
                request_id="req-original",
                step="renders",
            ),
            self._persisted(
                "2026-08-21T11:00:02",
                "req-original",
                "main_01",
            ),
            self._event(
                "2026-08-21T11:00:03",
                "step_succeeded",
                request_id="req-original",
                step="renders",
                detail="成功 1/计划 1（跳过 0）",
            ),
            self._event(
                "2026-08-21T11:00:04",
                "production_completed",
                request_id="req-original",
                produced_count=1,
            ),
            # This is the real _sync_existing order: the command is recorded,
            # already-present outputs are backfilled, then the ready route returns
            # without writing a second production_completed event.
            self._event(
                "2026-08-21T11:10:00",
                "command_received",
                request_id="req-sync",
                command="workflow-production",
            ),
            self._persisted(
                "2026-08-21T11:10:01",
                "req-sync",
                "main_01",
                backfilled=True,
            ),
        ]
        self._write_events(events)

        synced = build_workflow_batch_status(self.repository_root, self.batch_id)

        self.assertEqual("completed", synced["status"])
        self.assertEqual("renders", synced["currentStage"])
        self.assertEqual("2026-08-21T11:00:01", synced["stageStartedAt"])
        self.assertEqual("2026-08-21T11:00:03", synced["stageEndedAt"])

        failed_events = [
            *events,
            self._event(
                "2026-08-21T11:10:02",
                "step_failed",
                request_id="req-sync",
                detail="回补后执行失败",
                failure_code="projection_failed",
            ),
        ]
        self._write_events(failed_events)

        failed = build_workflow_batch_status(self.repository_root, self.batch_id)
        self.assertEqual("failed", failed["status"])
        self.assertEqual("projection_failed", failed["failureCode"])

        resumed_events = [
            *events,
            self._event(
                "2026-08-21T11:10:02",
                "step_started",
                request_id="req-sync",
                step="qc",
            ),
        ]
        self._write_events(resumed_events)

        resumed = build_workflow_batch_status(self.repository_root, self.batch_id)
        self.assertEqual("running", resumed["status"])
        self.assertEqual("qc", resumed["currentStage"])
        self.assertEqual("2026-08-21T11:10:02", resumed["stageStartedAt"])
        self.assertIsNone(resumed["stageEndedAt"])

    def test_render_plan_uses_latest_attempt_and_rejects_conflict_within_attempt(self) -> None:
        historical_and_latest = [
            self._event(
                "2026-08-21T10:59:59",
                "command_received",
                request_id="req-old",
                command="workflow-production",
            ),
            self._event(
                "2026-08-21T11:00:00",
                "step_started",
                request_id="req-old",
                step="renders",
            ),
            self._event(
                "2026-08-21T11:00:10",
                "step_failed",
                request_id="req-old",
                step="renders",
                detail="渲染中止：成功 0/计划 1（跳过 0）；原因：HTTP 524",
            ),
            self._event(
                "2026-08-21T11:09:59",
                "command_received",
                request_id="req-complete",
                command="workflow-production",
            ),
            self._event(
                "2026-08-21T11:10:00",
                "step_started",
                request_id="req-complete",
                step="renders",
            ),
            self._event(
                "2026-08-21T11:10:10",
                "step_succeeded",
                request_id="req-complete",
                step="renders",
                detail="成功 2/计划 2（跳过 1）",
            ),
            self._event(
                "2026-08-21T11:10:11",
                "production_completed",
                request_id="req-complete",
                produced_count=3,
            ),
            self._event(
                "2026-08-21T11:14:59",
                "command_received",
                request_id="req-latest",
                command="workflow-production",
            ),
            self._event(
                "2026-08-21T11:15:00",
                "step_started",
                request_id="req-latest",
                step="renders",
            ),
        ]
        self._write_events(historical_and_latest)

        summary = build_workflow_batch_status(self.repository_root, self.batch_id)

        self.assertEqual(
            {"completedCount": 3, "plannedCount": 3},
            summary["renders"],
        )

        same_attempt_conflict = [
            self._event(
                "2026-08-21T11:19:59",
                "command_received",
                request_id="req-conflict",
                command="workflow-production",
            ),
            self._event(
                "2026-08-21T11:20:00",
                "step_started",
                request_id="req-conflict",
                step="renders",
            ),
            self._event(
                "2026-08-21T11:20:10",
                "step_succeeded",
                request_id="req-conflict",
                step="renders",
                detail="成功 1/计划 2（跳过 0）",
            ),
            self._event(
                "2026-08-21T11:20:11",
                "production_completed",
                request_id="req-conflict",
                produced_count=3,
            ),
        ]
        self._write_events(same_attempt_conflict)

        with self.assertRaises(WorkflowBatchStatusError) as captured:
            build_workflow_batch_status(self.repository_root, self.batch_id)
        self.assertEqual(409, captured.exception.http_status)
        self.assertIn("完成前序损坏", str(captured.exception))

    def test_rebind_recompute_invalidates_old_render_evidence(self) -> None:
        events = [
            self._event(
                "2026-08-21T11:00:00",
                "command_received",
                request_id="req-old",
                command="workflow-production",
            ),
            self._event(
                "2026-08-21T11:00:01",
                "step_started",
                request_id="req-old",
                step="renders",
            ),
            self._persisted(
                "2026-08-21T11:00:02",
                "req-old",
                "main_01",
            ),
            self._event(
                "2026-08-21T11:00:03",
                "step_succeeded",
                request_id="req-old",
                step="renders",
                detail="成功 1/计划 1（跳过 0）",
            ),
            self._event(
                "2026-08-21T11:00:04",
                "production_completed",
                request_id="req-old",
                produced_count=1,
            ),
            self._event(
                "2026-08-21T11:05:00",
                "white_bg_rebind_recompute",
                missing=["img_001.png"],
                remaining_count=1,
            ),
        ]
        self._write_events(events)

        after_rebind = build_workflow_batch_status(self.repository_root, self.batch_id)

        self.assertEqual("queued", after_rebind["status"])
        self.assertEqual(
            {"completedCount": 0, "plannedCount": None},
            after_rebind["renders"],
        )

    def test_rebind_is_an_inactive_boundary_and_requires_a_fresh_modern_command(self) -> None:
        active_rebind = [
            self._event(
                "2026-08-21T11:00:00",
                "command_received",
                request_id="req-old",
                command="workflow-production",
            ),
            self._event(
                "2026-08-21T11:00:01",
                "step_started",
                request_id="req-old",
                step="renders",
            ),
            self._event(
                "2026-08-21T11:00:02",
                "white_bg_rebind_recompute",
                missing=["img_001.png"],
                remaining_count=1,
            ),
        ]
        self._assert_rejected(active_rebind, message_contains="重新绑定前序")

        completed_then_rebound = [
            active_rebind[0],
            active_rebind[1],
            self._event(
                "2026-08-21T11:00:02",
                "step_succeeded",
                request_id="req-old",
                step="renders",
                detail="成功 1/计划 1（跳过 0）",
            ),
            self._event(
                "2026-08-21T11:00:03",
                "production_completed",
                request_id="req-old",
                produced_count=1,
            ),
            self._event(
                "2026-08-21T11:00:04",
                "white_bg_rebind_recompute",
                missing=["img_001.png"],
                remaining_count=1,
            ),
            self._event(
                "2026-08-21T11:00:05",
                "command_received",
                request_id="req-new",
                command="workflow-production",
            ),
            self._event(
                "2026-08-21T11:00:06",
                "step_started",
                request_id="req-new",
                step="identity",
            ),
        ]
        self._write_events(completed_then_rebound)
        rebound = build_workflow_batch_status(self.repository_root, self.batch_id)
        self.assertEqual("running", rebound["status"])
        self.assertEqual("identity", rebound["currentStage"])
        self.assertEqual(
            {"completedCount": 0, "plannedCount": None},
            rebound["renders"],
        )

    def test_ordinary_command_received_preserves_persisted_render_progress(self) -> None:
        self._write_events(
            [
                self._event(
                    "2026-08-21T11:00:00",
                    "command_received",
                    request_id="req-old",
                    command="workflow-production",
                ),
                self._event(
                    "2026-08-21T11:00:01",
                    "step_started",
                    request_id="req-old",
                    step="renders",
                ),
                self._persisted(
                    "2026-08-21T11:00:02",
                    "req-old",
                    "main_01",
                ),
                self._event(
                    "2026-08-21T11:00:03",
                    "step_failed",
                    request_id="req-old",
                    step="renders",
                    detail="渲染中止：成功 1/计划 2（跳过 0）；原因：HTTP 524",
                ),
                self._event(
                    "2026-08-21T11:10:00",
                    "command_received",
                    request_id="req-new",
                    command="workflow-production",
                ),
                self._persisted(
                    "2026-08-21T11:10:01",
                    "req-new",
                    "detail_01",
                ),
            ]
        )

        summary = build_workflow_batch_status(self.repository_root, self.batch_id)

        self.assertEqual(
            {"completedCount": 2, "plannedCount": 2},
            summary["renders"],
        )

    def test_invalid_batch_ids_and_url_decoding_fail_closed(self) -> None:
        invalid_batch_ids = (
            "杯子:事故批",
            "杯子\r\n事故批",
            "杯子\x01事故批",
            "CON",
            "con.txt",
            "COM¹",
            "com².txt",
            "COM³.log",
            "LPT¹",
            "lpt².txt",
            "LPT³.log",
            "杯子_事故批.",
            "a" * 129,
            "../escape",
        )
        before = self._snapshot(self.repository_root)

        for batch_id in invalid_batch_ids:
            with self.subTest(batch_id=repr(batch_id), call="direct"):
                with self.assertRaises(WorkflowBatchStatusError) as captured:
                    build_workflow_batch_status(self.repository_root, batch_id)
                self.assertEqual(400, captured.exception.http_status)
            with self.subTest(batch_id=repr(batch_id), call="http"):
                status, _headers, body = self._get(self._status_path(batch_id))
                self.assertEqual(400, status)
                self.assertEqual({"ok": False, "error": "request_rejected"}, body)

        encoded_probes = (
            "/workflow-production/%E6%9D%AF%E5%AD%90%3A%E4%BA%8B%E6%95%85/status",
            "/workflow-production/%E6%9D%AF%E5%AD%90%0D%0A%E4%BA%8B%E6%95%85/status",
            "/workflow-production/..%2Fescape/status",
        )
        for path in encoded_probes:
            with self.subTest(path=path, call="encoded-http"):
                status, _headers, body = self._get(path)
                self.assertEqual(400, status)
                self.assertEqual({"ok": False, "error": "request_rejected"}, body)
        self.assertEqual(before, self._snapshot(self.repository_root))

    def test_status_ledger_rejects_symlinks_and_resolved_escape(self) -> None:
        self.ledger.write_text(
            json.dumps(
                self._event("2026-08-21T11:00:00", "command_received"),
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        real_is_symlink = Path.is_symlink
        with mock.patch.object(
            Path,
            "is_symlink",
            autospec=True,
            side_effect=lambda path: path == self.ledger or real_is_symlink(path),
        ):
            with self.assertRaises(WorkflowBatchStatusError) as captured:
                build_workflow_batch_status(self.repository_root, self.batch_id)
        self.assertEqual(409, captured.exception.http_status)

        outside_target = self.repository_root / "outside.events.jsonl"
        outside_target.write_text(self.ledger.read_text(encoding="utf-8"), encoding="utf-8")
        real_resolve = Path.resolve

        def resolve_outside(path: Path, strict: bool = False) -> Path:
            if path == self.ledger:
                return outside_target
            return real_resolve(path, strict=strict)

        with mock.patch.object(
            Path,
            "resolve",
            autospec=True,
            side_effect=resolve_outside,
        ):
            with self.assertRaises(WorkflowBatchStatusError) as escaped:
                build_workflow_batch_status(self.repository_root, self.batch_id)
        self.assertEqual(409, escaped.exception.http_status)

    def test_impossible_calendar_timestamp_is_a_damaged_ledger(self) -> None:
        for invalid_timestamp in (
            "2026-02-30T11:28:18",
            "2026-13-01T11:28:18",
            "2026-08-21T24:00:00",
        ):
            with self.subTest(timestamp=invalid_timestamp):
                self._write_events(
                    [self._event(invalid_timestamp, "command_received")]
                )
                with self.assertRaises(WorkflowBatchStatusError) as captured:
                    build_workflow_batch_status(self.repository_root, self.batch_id)
                self.assertEqual(409, captured.exception.http_status)
                self.assertIn("第 1 行损坏", str(captured.exception))

    def test_timestamps_must_be_nondecreasing(self) -> None:
        self._write_events(
            [
                self._event(
                    "2026-08-21T11:28:18",
                    "command_received",
                    request_id="req-equal",
                    command="workflow-production",
                ),
                self._event(
                    "2026-08-21T11:28:18",
                    "step_started",
                    request_id="req-equal",
                    step="identity",
                ),
            ]
        )
        equal_timestamps = build_workflow_batch_status(
            self.repository_root,
            self.batch_id,
        )
        self.assertEqual("running", equal_timestamps["status"])

        self._write_events(
            [
                self._event(
                    "2026-08-21T11:28:19",
                    "command_received",
                    request_id="req-reversed",
                    command="workflow-production",
                ),
                self._event(
                    "2026-08-21T11:28:18",
                    "step_started",
                    request_id="req-reversed",
                    step="identity",
                ),
            ]
        )
        before = self._snapshot(self.repository_root)

        with self.assertRaises(WorkflowBatchStatusError) as captured:
            build_workflow_batch_status(self.repository_root, self.batch_id)
        self.assertEqual(409, captured.exception.http_status)
        self.assertIn("第 2 行时间倒序", str(captured.exception))

        status, _headers, body = self._get(self._status_path())
        self.assertEqual(409, status)
        self.assertEqual({"ok": False, "error": "request_rejected"}, body)
        self.assertEqual(before, self._snapshot(self.repository_root))

    def test_active_step_mismatch_fails_closed_without_rejecting_legacy_history(self) -> None:
        self._write_events(
            [
                self._event(
                    "2026-08-21T11:28:18",
                    "step_started",
                    step="identity",
                    command="run: identity",
                ),
                self._event(
                    "2026-08-21T11:28:19",
                    "step_succeeded",
                    step="style_master",
                    detail="完成",
                ),
            ]
        )
        with self.assertRaises(WorkflowBatchStatusError) as captured:
            build_workflow_batch_status(self.repository_root, self.batch_id)
        self.assertEqual(409, captured.exception.http_status)
        self.assertIn("阶段起止记录不一致", str(captured.exception))

        self._write_events(
            [
                self._event(
                    "2026-08-21T11:28:18",
                    "step_started",
                    step="renders",
                    command="run: renders",
                ),
                self._event(
                    "2026-08-21T11:28:19",
                    "step_succeeded",
                    step="renders",
                    detail="成功 1/计划 1（跳过 0）",
                ),
                # Existing legacy ledgers contain this exact shape: a recorded
                # failure after a succeeded step without a fresh start marker.
                self._event(
                    "2026-08-21T11:28:20",
                    "step_failed",
                    step="renders",
                    detail="旧账本补记失败",
                ),
            ]
        )

        legacy = build_workflow_batch_status(self.repository_root, self.batch_id)
        self.assertEqual("failed", legacy["status"])
        self.assertEqual("renders", legacy["currentStage"])

    def test_step_succeeded_requires_a_matching_active_start(self) -> None:
        self._write_events(
            [
                self._event(
                    "2026-08-21T11:28:18",
                    "command_received",
                    request_id="req-1",
                    command="workflow-production",
                ),
                self._event(
                    "2026-08-21T11:28:19",
                    "step_succeeded",
                    request_id="req-1",
                    step="identity",
                    detail="完成",
                ),
            ]
        )
        before = self._snapshot(self.repository_root)

        with self.assertRaises(WorkflowBatchStatusError) as captured:
            build_workflow_batch_status(self.repository_root, self.batch_id)

        self.assertEqual(409, captured.exception.http_status)
        self.assertIn("阶段起止记录不一致", str(captured.exception))
        self.assertEqual(before, self._snapshot(self.repository_root))

    def test_prestart_failure_requires_matching_current_command_evidence(self) -> None:
        rejected_ledgers = (
            [
                self._event(
                    "2026-08-21T11:28:18",
                    "step_failed",
                    step="identity",
                    detail="无前序失败",
                )
            ],
            [
                self._event(
                    "2026-08-21T11:28:18",
                    "command_received",
                    request_id="req-current",
                    command="workflow-production",
                ),
                self._event(
                    "2026-08-21T11:28:19",
                    "step_failed",
                    request_id="req-other",
                    detail="请求不匹配",
                ),
            ],
            [
                self._event(
                    "2026-08-21T11:28:18",
                    "command_received",
                    request_id="req-current",
                ),
                self._event(
                    "2026-08-21T11:28:19",
                    "step_failed",
                    request_id="req-current",
                    detail="命令证据缺失",
                ),
            ],
        )
        for events in rejected_ledgers:
            with self.subTest(detail=events[-1]["detail"]):
                self._write_events(events)
                before = self._snapshot(self.repository_root)
                with self.assertRaises(WorkflowBatchStatusError) as captured:
                    build_workflow_batch_status(self.repository_root, self.batch_id)
                self.assertEqual(409, captured.exception.http_status)
                self.assertEqual(before, self._snapshot(self.repository_root))

        self._write_events(
            [
                self._event(
                    "2026-08-21T11:28:18",
                    "command_received",
                    request_id="req-current",
                    command="workflow-production",
                ),
                self._event(
                    "2026-08-21T11:28:19",
                    "step_failed",
                    request_id="req-current",
                    detail="启动阶段前失败",
                    failure_code="projection_failed",
                ),
            ]
        )

        allowed = build_workflow_batch_status(self.repository_root, self.batch_id)

        self.assertEqual("failed", allowed["status"])
        self.assertIsNone(allowed["currentStage"])
        self.assertEqual("projection_failed", allowed["failureCode"])

    def test_modern_command_is_unique_well_typed_and_uses_the_real_value(self) -> None:
        invalid_commands = (
            self._event(
                "2026-08-21T11:00:00",
                "command_received",
                request_id=7,
                command="workflow-production",
            ),
            self._event(
                "2026-08-21T11:00:00",
                "command_received",
                request_id="req-1",
                command={},
            ),
            self._event(
                "2026-08-21T11:00:00",
                "command_received",
                request_id="req-1",
                command="run: next",
            ),
            self._event(
                "2026-08-21T11:00:00",
                "command_received",
                request_id=" ",
                command="workflow-production",
            ),
        )
        for event in invalid_commands:
            with self.subTest(event=event):
                self._assert_rejected([event], message_contains="制作")

        repeated = [
            self._event(
                "2026-08-21T11:00:00",
                "command_received",
                request_id="req-1",
                command="workflow-production",
            ),
            self._event(
                "2026-08-21T11:00:01",
                "command_received",
                request_id="req-1",
                command="workflow-production",
            ),
        ]
        self._assert_rejected(repeated, message_contains="重复")

        abandoned_queued = [
            repeated[0],
            self._event(
                "2026-08-21T11:00:01",
                "command_received",
                request_id="req-2",
                command="workflow-production",
            ),
        ]
        self._write_events(abandoned_queued)
        self.assertEqual(
            "queued",
            build_workflow_batch_status(self.repository_root, self.batch_id)["status"],
        )

        command_while_active = [
            repeated[0],
            self._event(
                "2026-08-21T11:00:01",
                "step_started",
                request_id="req-1",
                step="identity",
            ),
            self._event(
                "2026-08-21T11:00:02",
                "command_received",
                request_id="req-2",
                command="workflow-production",
            ),
        ]
        self._write_events(command_while_active)
        active_superseded = build_workflow_batch_status(
            self.repository_root, self.batch_id
        )
        self.assertEqual("queued", active_superseded["status"])
        self.assertIsNone(active_superseded["currentStage"])

        self._assert_rejected(
            [
                *command_while_active,
                self._event(
                    "2026-08-21T11:00:03",
                    "step_succeeded",
                    request_id="req-1",
                    step="identity",
                    detail="旧请求迟到",
                ),
            ],
            message_contains="请求归属",
        )

        command_between_steps = [
            *command_while_active[:2],
            self._event(
                "2026-08-21T11:00:02",
                "step_succeeded",
                request_id="req-1",
                step="identity",
                detail="完成",
            ),
            self._event(
                "2026-08-21T11:00:03",
                "command_received",
                request_id="req-2",
                command="workflow-production",
            ),
        ]
        self._write_events(command_between_steps)
        self.assertEqual(
            "queued",
            build_workflow_batch_status(self.repository_root, self.batch_id)["status"],
        )

    def test_modern_lifecycle_events_must_belong_to_the_current_request(self) -> None:
        command = self._event(
            "2026-08-21T11:00:00",
            "command_received",
            request_id="req-current",
            command="workflow-production",
        )
        start = self._event(
            "2026-08-21T11:00:01",
            "step_started",
            request_id="req-current",
            step="renders",
        )
        success = self._event(
            "2026-08-21T11:00:02",
            "step_succeeded",
            request_id="req-current",
            step="renders",
            detail="成功 1/计划 1（跳过 0）",
        )
        wrong_request_cases = (
            [
                command,
                self._event(
                    "2026-08-21T11:00:01",
                    "step_started",
                    request_id="req-other",
                    step="identity",
                ),
            ],
            [
                command,
                start,
                self._event(
                    "2026-08-21T11:00:02",
                    "step_succeeded",
                    request_id="req-other",
                    step="renders",
                    detail="完成",
                ),
            ],
            [
                command,
                start,
                self._event(
                    "2026-08-21T11:00:02",
                    "step_failed",
                    request_id="req-other",
                    detail="失败",
                ),
            ],
            [
                command,
                start,
                success,
                self._event(
                    "2026-08-21T11:00:03",
                    "production_paused",
                    request_id="req-other",
                    produced_count=1,
                ),
            ],
            [
                command,
                start,
                success,
                self._event(
                    "2026-08-21T11:00:03",
                    "production_completed",
                    request_id="req-other",
                    produced_count=1,
                ),
            ],
        )
        for events in wrong_request_cases:
            with self.subTest(event=events[-1]["event"]):
                self._assert_rejected(events)

        bad_type_cases = (
            [
                command,
                self._event(
                    "2026-08-21T11:00:01",
                    "step_started",
                    request_id=7,
                    step="identity",
                ),
            ],
            [
                command,
                start,
                self._event(
                    "2026-08-21T11:00:02",
                    "step_succeeded",
                    request_id=False,
                    step="renders",
                    detail="完成",
                ),
            ],
            [
                command,
                start,
                self._event(
                    "2026-08-21T11:00:02",
                    "step_failed",
                    request_id={},
                    detail="失败",
                ),
            ],
            [
                command,
                start,
                success,
                self._event(
                    "2026-08-21T11:00:03",
                    "production_paused",
                    request_id=None,
                    produced_count=1,
                ),
            ],
            [
                command,
                start,
                success,
                self._event(
                    "2026-08-21T11:00:03",
                    "production_completed",
                    request_id=[],
                    produced_count=1,
                ),
            ],
        )
        for events in bad_type_cases:
            with self.subTest(event=events[-1]["event"], value=events[-1]["request_id"]):
                self._assert_rejected(events)

    def test_failed_request_is_terminal_until_a_new_command(self) -> None:
        failed = [
            self._event(
                "2026-08-21T11:00:00",
                "command_received",
                request_id="req-failed",
                command="workflow-production",
            ),
            self._event(
                "2026-08-21T11:00:01",
                "step_started",
                request_id="req-failed",
                step="identity",
            ),
            self._event(
                "2026-08-21T11:00:02",
                "step_failed",
                request_id="req-failed",
                detail="失败",
            ),
        ]
        forbidden_followups = (
            self._event(
                "2026-08-21T11:00:03",
                "step_started",
                request_id="req-failed",
                step="identity",
            ),
            self._event(
                "2026-08-21T11:00:03",
                "step_succeeded",
                request_id="req-failed",
                step="identity",
                detail="伪造成功",
            ),
            self._event(
                "2026-08-21T11:00:03",
                "step_failed",
                request_id="req-failed",
                detail="重复失败",
            ),
            self._event(
                "2026-08-21T11:00:03",
                "production_paused",
                request_id="req-failed",
                produced_count=0,
                reason="awaiting_render_gate",
            ),
            self._event(
                "2026-08-21T11:00:03",
                "production_completed",
                request_id="req-failed",
                produced_count=0,
            ),
        )
        for followup in forbidden_followups:
            with self.subTest(event=followup["event"]):
                self._assert_rejected([*failed, followup])

        resumed_events = [
            *failed,
            self._event(
                "2026-08-21T11:00:03",
                "command_received",
                request_id="req-new",
                command="workflow-production",
            ),
            self._event(
                "2026-08-21T11:00:04",
                "step_started",
                request_id="req-new",
                step="identity",
            ),
        ]
        self._write_events(resumed_events)
        resumed = build_workflow_batch_status(self.repository_root, self.batch_id)
        self.assertEqual("running", resumed["status"])
        self.assertEqual("identity", resumed["currentStage"])

    def test_prestart_failure_rejects_forged_step_evidence(self) -> None:
        command = self._event(
            "2026-08-21T11:00:00",
            "command_received",
            request_id="req-1",
            command="workflow-production",
        )
        for fake_step in ("identity", None, 7):
            with self.subTest(step=fake_step):
                self._assert_rejected(
                    [
                        command,
                        self._event(
                            "2026-08-21T11:00:01",
                            "step_failed",
                            request_id="req-1",
                            step=fake_step,
                            detail="启动前失败",
                        ),
                    ],
                    message_contains="启动前失败记录损坏",
                )

    def test_pause_only_accepts_the_two_producer_sequences(self) -> None:
        bare_or_active_cases = (
            [
                self._event(
                    "2026-08-21T11:00:00",
                    "production_paused",
                    request_id="req-1",
                    produced_count=0,
                )
            ],
            [
                self._event(
                    "2026-08-21T11:00:00",
                    "command_received",
                    request_id="req-1",
                    command="workflow-production",
                ),
                self._event(
                    "2026-08-21T11:00:01",
                    "production_paused",
                    request_id="req-1",
                    produced_count=0,
                ),
            ],
            [
                self._event(
                    "2026-08-21T11:00:00",
                    "command_received",
                    request_id="req-1",
                    command="workflow-production",
                ),
                self._event(
                    "2026-08-21T11:00:01",
                    "step_started",
                    request_id="req-1",
                    step="renders",
                ),
                self._event(
                    "2026-08-21T11:00:02",
                    "production_paused",
                    request_id="req-1",
                    produced_count=0,
                ),
            ],
        )
        for events in bare_or_active_cases:
            with self.subTest(case=len(events)):
                self._assert_rejected(events, message_contains="暂停前序")

        identity_success = [
            self._event(
                "2026-08-21T11:00:00",
                "command_received",
                request_id="req-1",
                command="workflow-production",
            ),
            self._event(
                "2026-08-21T11:00:01",
                "step_started",
                request_id="req-1",
                step="identity",
            ),
            self._event(
                "2026-08-21T11:00:02",
                "step_succeeded",
                request_id="req-1",
                step="identity",
                detail="完成",
            ),
        ]
        self._assert_rejected(
            [
                *identity_success,
                self._event(
                    "2026-08-21T11:00:03",
                    "production_paused",
                    request_id="req-1",
                    produced_count=0,
                ),
            ],
            message_contains="暂停前序",
        )
        self._assert_rejected(
            [
                *identity_success,
                self._event(
                    "2026-08-21T11:00:03",
                    "production_paused",
                    request_id="req-1",
                    produced_count=0,
                    reason="awaiting_render_gate",
                ),
            ],
            message_contains="出图闸门前序",
        )

        render_success = [
            identity_success[0],
            self._event(
                "2026-08-21T11:00:01",
                "step_started",
                request_id="req-1",
                step="renders",
            ),
            self._event(
                "2026-08-21T11:00:02",
                "step_succeeded",
                request_id="req-1",
                step="renders",
                detail="成功 1/计划 1（跳过 0）",
            ),
        ]
        for bad_reason in (None, "unknown_reason", ""):
            with self.subTest(reason=bad_reason):
                self._assert_rejected(
                    [
                        *render_success,
                        self._event(
                            "2026-08-21T11:00:03",
                            "production_paused",
                            request_id="req-1",
                            produced_count=1,
                            reason=bad_reason,
                        ),
                    ],
                    message_contains="暂停原因",
                )

    def test_legacy_compatibility_requires_a_real_self_contained_command(self) -> None:
        invalid_starts = (
            self._event(
                "2026-08-21T11:00:00",
                "step_started",
                step="identity",
            ),
            self._event(
                "2026-08-21T11:00:00",
                "step_started",
                step="renders",
                command="run: identity",
            ),
            self._event(
                "2026-08-21T11:00:00",
                "step_started",
                step="renders",
                command={},
            ),
            self._event(
                "2026-08-21T11:00:00",
                "step_started",
                request_id=None,
                step="renders",
                command="run: renders",
            ),
        )
        for event in invalid_starts:
            with self.subTest(event=event):
                self._assert_rejected([event])

        consecutive_starts = [
            self._event(
                "2026-08-21T11:00:00",
                "step_started",
                step="identity",
                command="run: identity",
            ),
            self._event(
                "2026-08-21T11:00:01",
                "step_started",
                step="identity",
                command="run: next",
            ),
            self._event(
                "2026-08-21T11:00:02",
                "step_failed",
                step="identity",
                detail="旧版第二次执行失败",
            ),
        ]
        self._write_events(consecutive_starts)
        legacy = build_workflow_batch_status(self.repository_root, self.batch_id)
        self.assertEqual("failed", legacy["status"])
        self.assertEqual("2026-08-21T11:00:01", legacy["stageStartedAt"])

        modern_then_legacy_failure = [
            self._event(
                "2026-08-21T11:00:00",
                "command_received",
                request_id="req-1",
                command="workflow-production",
            ),
            self._event(
                "2026-08-21T11:00:01",
                "step_started",
                request_id="req-1",
                step="renders",
            ),
            self._event(
                "2026-08-21T11:00:02",
                "step_succeeded",
                request_id="req-1",
                step="renders",
                detail="成功 1/计划 1（跳过 0）",
            ),
            self._event(
                "2026-08-21T11:00:03",
                "step_failed",
                step="renders",
                detail="伪装成旧版补记",
            ),
        ]
        self._assert_rejected(modern_then_legacy_failure, message_contains="旧版")

        legacy_identity_after_success = [
            self._event(
                "2026-08-21T11:00:00",
                "step_started",
                step="identity",
                command="run: identity",
            ),
            self._event(
                "2026-08-21T11:00:01",
                "step_succeeded",
                step="identity",
                detail="完成",
            ),
            self._event(
                "2026-08-21T11:00:02",
                "step_failed",
                step="identity",
                detail="非 renders 补记",
            ),
        ]
        self._assert_rejected(legacy_identity_after_success)

        nonadjacent_legacy_render_failure = [
            self._event(
                "2026-08-21T11:00:00",
                "step_started",
                step="renders",
                command="retry: renders",
            ),
            self._event(
                "2026-08-21T11:00:01",
                "step_succeeded",
                step="renders",
                detail="成功 1/计划 1（跳过 0）",
            ),
            self._event(
                "2026-08-21T11:00:02",
                "workspace_relocated",
            ),
            self._event(
                "2026-08-21T11:00:03",
                "step_failed",
                step="renders",
                detail="非相邻补记",
            ),
        ]
        self._assert_rejected(nonadjacent_legacy_render_failure)

    def test_render_retry_shape_matches_the_producer_bounds(self) -> None:
        active_render = [
            self._event(
                "2026-08-21T11:00:00",
                "command_received",
                request_id="req-1",
                command="workflow-production",
            ),
            self._event(
                "2026-08-21T11:00:01",
                "step_started",
                request_id="req-1",
                step="renders",
            ),
        ]
        valid_retry = self._event(
            "2026-08-21T11:00:02",
            "render_retry",
            config_id="main_01",
            attempt=2,
            failure_code="render_timeout",
            http_status=599,
            delay_seconds=600,
        )
        self._write_events([*active_render, valid_retry])
        valid = build_workflow_batch_status(self.repository_root, self.batch_id)
        self.assertEqual("running", valid["status"])

        invalid_fields = (
            {"attempt": 0},
            {"attempt": 3},
            {"attempt": True},
            {"attempt": "1"},
            {"delay_seconds": -1},
            {"delay_seconds": 601},
            {"delay_seconds": 1.5},
            {"http_status": 99},
            {"http_status": 600},
            {"http_status": True},
            {"failure_code": "render_response_invalid"},
            {"request_id": "req-1"},
        )
        for replacement in invalid_fields:
            with self.subTest(replacement=replacement):
                invalid_retry = dict(valid_retry)
                invalid_retry.update(replacement)
                self._assert_rejected(
                    [*active_render, invalid_retry],
                    message_contains="出图重试记录损坏",
                )

        self._assert_rejected(
            [active_render[0], valid_retry],
            message_contains="出图重试记录损坏",
        )
        self._assert_rejected(
            [
                self._event(
                    "2026-08-21T11:00:00",
                    "step_started",
                    step="renders",
                    command="retry: renders",
                ),
                valid_retry,
            ],
            message_contains="出图重试记录损坏",
        )

    def test_production_completed_allows_real_qc_continuation_but_not_forged_followups(self) -> None:
        completed = [
            self._event(
                "2026-08-21T11:00:00",
                "command_received",
                request_id="req-1",
                command="workflow-production",
            ),
            self._event(
                "2026-08-21T11:00:01",
                "step_started",
                request_id="req-1",
                step="renders",
            ),
            self._event(
                "2026-08-21T11:00:02",
                "step_succeeded",
                request_id="req-1",
                step="renders",
                detail="成功 1/计划 1（跳过 0）",
            ),
            self._event(
                "2026-08-21T11:00:03",
                "production_completed",
                request_id="req-1",
                produced_count=1,
            ),
        ]
        qc_continuation = [
            *completed,
            self._event(
                "2026-08-21T11:00:04",
                "step_started",
                request_id="req-1",
                step="qc",
            ),
            self._event(
                "2026-08-21T11:00:05",
                "step_succeeded",
                request_id="req-1",
                step="qc",
                detail="QC 报告已生成",
            ),
        ]
        self._write_events(qc_continuation)
        qc = build_workflow_batch_status(self.repository_root, self.batch_id)
        self.assertEqual("completed", qc["status"])
        self.assertEqual("qc", qc["currentStage"])

        self._assert_rejected(
            [
                *qc_continuation,
                self._event(
                    "2026-08-21T11:00:06",
                    "command_received",
                    request_id="req-after-qc",
                    command="workflow-production",
                ),
                self._event(
                    "2026-08-21T11:00:07",
                    "step_started",
                    request_id="req-after-qc",
                    step="qc",
                ),
            ],
            message_contains="生命周期",
        )

        self._assert_rejected(
            [
                *completed,
                self._event(
                    "2026-08-21T11:00:04",
                    "step_started",
                    request_id="req-1",
                    step="renders",
                ),
            ]
        )
        self._assert_rejected(
            [
                *completed,
                self._event(
                    "2026-08-21T11:00:04",
                    "command_received",
                    request_id="req-2",
                    command="workflow-production",
                ),
                self._event(
                    "2026-08-21T11:00:05",
                    "production_completed",
                    request_id="req-2",
                    produced_count=1,
                ),
            ],
            message_contains="完成前序损坏",
        )

    def test_production_completed_requires_inactive_successful_render_evidence(self) -> None:
        invalid_ledgers = (
            [
                self._event(
                    "2026-08-21T11:28:18",
                    "production_completed",
                    request_id="req-no-command",
                    produced_count=1,
                )
            ],
            [
                self._event(
                    "2026-08-21T11:28:17",
                    "command_received",
                    request_id="req-active",
                    command="workflow-production",
                ),
                self._event(
                    "2026-08-21T11:28:18",
                    "step_started",
                    request_id="req-active",
                    step="renders",
                ),
                self._event(
                    "2026-08-21T11:28:19",
                    "production_completed",
                    request_id="req-active",
                    produced_count=1,
                ),
            ],
            [
                self._event(
                    "2026-08-21T11:28:17",
                    "command_received",
                    request_id="req-no-render",
                    command="workflow-production",
                ),
                self._event(
                    "2026-08-21T11:28:18",
                    "step_started",
                    request_id="req-no-render",
                    step="identity",
                ),
                self._event(
                    "2026-08-21T11:28:19",
                    "step_succeeded",
                    request_id="req-no-render",
                    step="identity",
                    detail="完成",
                ),
                self._event(
                    "2026-08-21T11:28:20",
                    "production_completed",
                    request_id="req-no-render",
                    produced_count=1,
                ),
            ],
        )
        for events in invalid_ledgers:
            with self.subTest(events=[event["event"] for event in events]):
                self._write_events(events)
                before = self._snapshot(self.repository_root)
                with self.assertRaises(WorkflowBatchStatusError) as captured:
                    build_workflow_batch_status(self.repository_root, self.batch_id)
                self.assertEqual(409, captured.exception.http_status)
                self.assertIn("完成前序损坏", str(captured.exception))
                self.assertEqual(before, self._snapshot(self.repository_root))

    def test_production_completed_cannot_overwrite_a_failure(self) -> None:
        self._write_events(
            [
                self._event(
                    "2026-08-21T11:28:18",
                    "command_received",
                    request_id="req-1",
                    command="workflow-production",
                ),
                self._event(
                    "2026-08-21T11:28:19",
                    "step_started",
                    request_id="req-1",
                    step="renders",
                ),
                self._event(
                    "2026-08-21T11:28:20",
                    "step_succeeded",
                    request_id="req-1",
                    step="renders",
                    detail="成功 1/计划 1（跳过 0）",
                ),
                self._event(
                    "2026-08-21T11:28:21",
                    "step_failed",
                    request_id="req-1",
                    detail="完成后补记失败",
                ),
                self._event(
                    "2026-08-21T11:28:22",
                    "production_completed",
                    request_id="req-1",
                    produced_count=1,
                ),
            ]
        )
        before = self._snapshot(self.repository_root)

        with self.assertRaises(WorkflowBatchStatusError) as captured:
            build_workflow_batch_status(self.repository_root, self.batch_id)

        self.assertEqual(409, captured.exception.http_status)
        self.assertIn("完成前序损坏", str(captured.exception))
        self.assertEqual(before, self._snapshot(self.repository_root))

    def test_image_persisted_requires_the_current_modern_request(self) -> None:
        command = self._event(
            "2026-08-21T11:00:00",
            "command_received",
            request_id="req-current",
            command="workflow-production",
        )
        missing_request = self._persisted(
            "2026-08-21T11:00:01",
            "req-current",
            "main_01",
            backfilled=True,
        )
        missing_request.pop("request_id")
        wrong_request = self._persisted(
            "2026-08-21T11:00:01",
            "req-forged",
            "main_01",
            backfilled=True,
        )
        bad_type = self._persisted(
            "2026-08-21T11:00:01",
            "req-current",
            "main_01",
            backfilled=True,
        )
        bad_type["request_id"] = 7
        for persisted in (missing_request, wrong_request, bad_type):
            with self.subTest(request_id=persisted.get("request_id", "missing")):
                self._assert_rejected(
                    [command, persisted],
                    message_contains="请求归属",
                )

    def test_sync_existing_images_can_complete_without_a_renders_step(self) -> None:
        events = [
            self._event(
                "2026-08-21T11:00:00",
                "command_received",
                request_id="req-sync",
                command="workflow-production",
            ),
            self._persisted(
                "2026-08-21T11:00:01",
                "req-sync",
                "main_01",
                backfilled=True,
            ),
            self._persisted(
                "2026-08-21T11:00:02",
                "req-sync",
                "detail_01",
                backfilled=True,
            ),
            self._event(
                "2026-08-21T11:00:03",
                "production_completed",
                request_id="req-sync",
                produced_count=2,
            ),
        ]
        self._write_events(events)
        before = self._snapshot(self.repository_root)

        summary = build_workflow_batch_status(self.repository_root, self.batch_id)

        self.assertEqual("completed", summary["status"])
        self.assertEqual(
            {"completedCount": 2, "plannedCount": 2},
            summary["renders"],
        )
        self.assertEqual(before, self._snapshot(self.repository_root))

    def test_terminal_count_must_match_the_current_render_attempt(self) -> None:
        render_success = [
            self._event(
                "2026-08-21T11:00:00",
                "command_received",
                request_id="req-1",
                command="workflow-production",
            ),
            self._event(
                "2026-08-21T11:00:01",
                "step_started",
                request_id="req-1",
                step="renders",
            ),
            self._event(
                "2026-08-21T11:00:02",
                "step_succeeded",
                request_id="req-1",
                step="renders",
                detail="成功 1/计划 2（跳过 0）",
            ),
        ]
        self._assert_rejected(
            [
                *render_success,
                self._event(
                    "2026-08-21T11:00:03",
                    "production_completed",
                    request_id="req-1",
                    produced_count=2,
                ),
            ],
            message_contains="完成前序损坏",
        )
        self._assert_rejected(
            [
                *render_success,
                self._event(
                    "2026-08-21T11:00:03",
                    "production_paused",
                    request_id="req-1",
                    produced_count=2,
                ),
            ],
            message_contains="暂停前序损坏",
        )

    def test_historical_render_success_cannot_authorize_a_new_request(self) -> None:
        events = [
            self._event(
                "2026-08-21T11:00:00",
                "command_received",
                request_id="req-old",
                command="workflow-production",
            ),
            self._event(
                "2026-08-21T11:00:01",
                "step_started",
                request_id="req-old",
                step="renders",
            ),
            self._event(
                "2026-08-21T11:00:02",
                "step_succeeded",
                request_id="req-old",
                step="renders",
                detail="成功 1/计划 1（跳过 0）",
            ),
            self._event(
                "2026-08-21T11:00:03",
                "production_paused",
                request_id="req-old",
                produced_count=1,
            ),
            self._event(
                "2026-08-21T11:01:00",
                "command_received",
                request_id="req-new",
                command="workflow-production",
            ),
            self._event(
                "2026-08-21T11:01:01",
                "step_started",
                request_id="req-new",
                step="identity",
            ),
            self._event(
                "2026-08-21T11:01:02",
                "step_succeeded",
                request_id="req-new",
                step="identity",
                detail="完成",
            ),
            self._event(
                "2026-08-21T11:01:03",
                "production_completed",
                request_id="req-new",
                produced_count=1,
            ),
        ]
        self._assert_rejected(events, message_contains="完成前序损坏")

    def test_qc_repair_saga_is_isolated_from_the_production_summary(self) -> None:
        production = [
            self._event(
                "2026-08-21T11:00:00",
                "command_received",
                request_id="req-production",
                command="workflow-production",
            ),
            self._event(
                "2026-08-21T11:00:01",
                "step_started",
                request_id="req-production",
                step="renders",
            ),
            self._event(
                "2026-08-21T11:00:02",
                "step_succeeded",
                request_id="req-production",
                step="renders",
                detail="成功 1/计划 1（跳过 0）",
            ),
            self._event(
                "2026-08-21T11:00:03",
                "production_completed",
                request_id="req-production",
                produced_count=1,
            ),
        ]
        repair = [
            self._event(
                "2026-08-21T12:00:00",
                "command_received",
                request_id="req-repair",
                command="repair",
            ),
            self._event(
                "2026-08-21T12:00:01",
                "step_started",
                request_id="req-repair",
                step="repair",
                target_count=1,
                work_order_count=1,
            ),
            self._event(
                "2026-08-21T12:00:02",
                "repair_item_started",
                request_id="req-repair",
                step="repair",
                config_id="main_01",
                target_count=1,
                review_count=0,
            ),
            self._event(
                "2026-08-21T12:00:03",
                "repair_item_succeeded",
                request_id="req-repair",
                step="repair",
                config_id="main_01",
                sha256="b" * 64,
                byte_count=128,
                width=1024,
                height=1024,
            ),
            self._event(
                "2026-08-21T12:00:04",
                "repair_completed",
                request_id="req-repair",
                step="repair",
                succeeded_count=1,
                failed_count=0,
                skipped_count=0,
                failed_config_ids=[],
            ),
            self._event(
                "2026-08-21T12:00:05",
                "step_succeeded",
                request_id="req-repair",
                step="repair",
                detail="返修处理完成：成功 1，跳过 0",
            ),
        ]
        self._write_events(production)
        expected = build_workflow_batch_status(self.repository_root, self.batch_id)
        self._write_events([*production, *repair])
        before = self._snapshot(self.repository_root)

        repaired = build_workflow_batch_status(self.repository_root, self.batch_id)

        self.assertEqual(expected, repaired)
        self.assertEqual(before, self._snapshot(self.repository_root))
        self._assert_rejected(
            [
                *production,
                self._event(
                    "2026-08-21T12:00:00",
                    "repair_completed",
                    request_id="req-orphan",
                    step="repair",
                    succeeded_count=1,
                    failed_count=0,
                    skipped_count=0,
                    failed_config_ids=[],
                ),
            ],
            message_contains="返修请求归属损坏",
        )

    def test_image_persisted_requires_complete_writer_evidence(self) -> None:
        command = self._event(
            "2026-08-21T11:00:00",
            "command_received",
            request_id="req-1",
            command="workflow-production",
        )
        valid = self._persisted(
            "2026-08-21T11:00:01",
            "req-1",
            "main_01",
            backfilled=True,
        )
        invalid_events: list[dict[str, Any]] = []
        for field_name in ("source", "sha256", "byte_count", "width", "height"):
            missing = dict(valid)
            missing.pop(field_name)
            invalid_events.append(missing)
        for field_name, value in (
            ("source", "unknown"),
            ("sha256", "A" * 64),
            ("byte_count", True),
            ("width", 0),
            ("height", 2_147_483_648),
            ("backfilled", False),
        ):
            malformed = dict(valid)
            malformed[field_name] = value
            invalid_events.append(malformed)

        for invalid in invalid_events:
            with self.subTest(fields=invalid):
                self._assert_rejected(
                    [command, invalid],
                    message_contains="成图记录损坏",
                )

    def test_historical_persisted_evidence_authorizes_duplicate_suppressed_sync(self) -> None:
        events = [
            self._event(
                "2026-08-21T11:00:00",
                "command_received",
                request_id="req-crashed",
                command="workflow-production",
            ),
            self._persisted(
                "2026-08-21T11:00:01",
                "req-crashed",
                "main_01",
                backfilled=True,
            ),
            self._event(
                "2026-08-21T11:01:00",
                "command_received",
                request_id="req-restarted",
                command="workflow-production",
            ),
            # _backfill_persisted_event suppresses a duplicate config globally,
            # so a restarted writer can legitimately append only this terminal.
            self._event(
                "2026-08-21T11:01:01",
                "production_completed",
                request_id="req-restarted",
                produced_count=1,
            ),
        ]
        self._write_events(events)
        before = self._snapshot(self.repository_root)

        summary = build_workflow_batch_status(self.repository_root, self.batch_id)

        self.assertEqual("completed", summary["status"])
        self.assertEqual(
            {"completedCount": 1, "plannedCount": 1},
            summary["renders"],
        )
        self.assertEqual(before, self._snapshot(self.repository_root))

        self._assert_rejected(
            [
                *events[:2],
                self._event(
                    "2026-08-21T11:00:02",
                    "white_bg_rebind_recompute",
                ),
                *events[2:],
            ],
            message_contains="完成前序损坏",
        )

    def test_qc_repair_payloads_and_counts_are_writer_exact(self) -> None:
        valid = [
            self._event(
                "2026-08-21T12:00:00",
                "command_received",
                request_id="repair-1",
                command="repair",
            ),
            self._event(
                "2026-08-21T12:00:01",
                "step_started",
                request_id="repair-1",
                step="repair",
                target_count=1,
                work_order_count=1,
            ),
            self._event(
                "2026-08-21T12:00:02",
                "repair_item_started",
                request_id="repair-1",
                step="repair",
                config_id="main_01",
                target_count=1,
                review_count=0,
            ),
            self._event(
                "2026-08-21T12:00:03",
                "repair_item_succeeded",
                request_id="repair-1",
                step="repair",
                config_id="main_01",
                sha256="b" * 64,
                byte_count=128,
                width=1024,
                height=1024,
            ),
            self._event(
                "2026-08-21T12:00:04",
                "repair_completed",
                request_id="repair-1",
                step="repair",
                succeeded_count=1,
                failed_count=0,
                skipped_count=0,
                failed_config_ids=[],
            ),
            self._event(
                "2026-08-21T12:00:05",
                "step_succeeded",
                request_id="repair-1",
                step="repair",
                detail="返修处理完成：成功 1，跳过 0",
            ),
        ]
        self._write_events(valid)
        self.assertEqual(
            "queued",
            build_workflow_batch_status(self.repository_root, self.batch_id)["status"],
        )

        malformed_success = [dict(event) for event in valid]
        malformed_success[3].pop("width")
        inconsistent_target_total = [dict(event) for event in valid]
        inconsistent_target_total[1]["target_count"] = 2
        inconsistent_completion = [dict(event) for event in valid]
        inconsistent_completion[4]["succeeded_count"] = 0
        false_final_detail = [dict(event) for event in valid]
        false_final_detail[5]["detail"] = "返修处理完成：成功 0，跳过 1"
        for events in (
            malformed_success,
            inconsistent_target_total,
            inconsistent_completion,
            false_final_detail,
        ):
            with self.subTest(events=events):
                self._assert_rejected(events, message_contains="返修")

    def test_qc_repair_rejects_interleaving_and_duplicate_request_ids(self) -> None:
        production = self._event(
            "2026-08-21T12:00:00",
            "command_received",
            request_id="shared-request",
            command="workflow-production",
        )
        repair = self._event(
            "2026-08-21T12:00:01",
            "command_received",
            request_id="repair-1",
            command="repair",
        )
        second_repair = self._event(
            "2026-08-21T12:00:02",
            "command_received",
            request_id="repair-2",
            command="repair",
        )
        for events in (
            [production, repair],
            [repair, production],
            [repair, second_repair],
            [
                production,
                self._event(
                    "2026-08-21T12:00:01",
                    "step_failed",
                    request_id="shared-request",
                    detail="制作失败",
                ),
                self._event(
                    "2026-08-21T12:00:02",
                    "command_received",
                    request_id="shared-request",
                    command="repair",
                ),
            ],
        ):
            with self.subTest(events=events):
                self._assert_rejected(events)

    def test_qc_repair_failure_and_skip_summary_matches_all_items(self) -> None:
        events = [
            self._event(
                "2026-08-21T12:00:00",
                "command_received",
                request_id="repair-mixed",
                command="repair",
            ),
            self._event(
                "2026-08-21T12:00:01",
                "step_started",
                request_id="repair-mixed",
                step="repair",
                target_count=3,
                work_order_count=3,
            ),
        ]
        outcomes = (
            (
                "main_01",
                self._event(
                    "2026-08-21T12:00:03",
                    "repair_item_succeeded",
                    request_id="repair-mixed",
                    step="repair",
                    config_id="main_01",
                    sha256="a" * 64,
                    byte_count=128,
                    width=1024,
                    height=1024,
                ),
            ),
            (
                "detail_01",
                self._event(
                    "2026-08-21T12:00:05",
                    "repair_item_failed",
                    request_id="repair-mixed",
                    step="repair",
                    config_id="detail_01",
                    detail="图片返修执行失败，未自动重试",
                ),
            ),
            (
                "detail_02",
                self._event(
                    "2026-08-21T12:00:07",
                    "repair_item_skipped_existing",
                    request_id="repair-mixed",
                    step="repair",
                    config_id="detail_02",
                    sha256="b" * 64,
                ),
            ),
        )
        for index, (config_id, terminal) in enumerate(outcomes, start=1):
            events.extend(
                [
                    self._event(
                        f"2026-08-21T12:00:{index * 2:02d}",
                        "repair_item_started",
                        request_id="repair-mixed",
                        step="repair",
                        config_id=config_id,
                        target_count=1,
                        review_count=0,
                    ),
                    terminal,
                ]
            )
        events.extend(
            [
                self._event(
                    "2026-08-21T12:00:08",
                    "repair_completed",
                    request_id="repair-mixed",
                    step="repair",
                    succeeded_count=1,
                    failed_count=1,
                    skipped_count=1,
                    failed_config_ids=["detail_01"],
                ),
                self._event(
                    "2026-08-21T12:00:09",
                    "step_completed_with_failures",
                    request_id="repair-mixed",
                    step="repair",
                    succeeded_count=1,
                    failed_count=1,
                    skipped_count=1,
                    failed_config_ids=["detail_01"],
                ),
            ]
        )
        self._write_events(events)
        before = self._snapshot(self.repository_root)

        summary = build_workflow_batch_status(self.repository_root, self.batch_id)

        self.assertEqual("queued", summary["status"])
        self.assertEqual(before, self._snapshot(self.repository_root))

    def test_qc_repair_gate_and_protection_failure_match_writer_sequences(self) -> None:
        standalone_gate = [
            self._event(
                "2026-08-21T12:00:00",
                "gate_rejected",
                request_id="repair-gate",
                command="repair",
                detail="返修命令未通过解析门禁",
            )
        ]
        self._write_events(standalone_gate)
        self.assertEqual(
            "queued",
            build_workflow_batch_status(self.repository_root, self.batch_id)["status"],
        )

        protection_failure = [
            self._event(
                "2026-08-21T12:00:00",
                "command_received",
                request_id="repair-protection",
                command="repair",
            ),
            self._event(
                "2026-08-21T12:00:01",
                "step_started",
                request_id="repair-protection",
                step="repair",
                target_count=1,
                work_order_count=1,
            ),
            self._event(
                "2026-08-21T12:00:02",
                "repair_item_started",
                request_id="repair-protection",
                step="repair",
                config_id="main_01",
                target_count=1,
                review_count=0,
            ),
            self._event(
                "2026-08-21T12:00:03",
                "repair_item_skipped_existing",
                request_id="repair-protection",
                step="repair",
                config_id="main_01",
                sha256="c" * 64,
            ),
            self._event(
                "2026-08-21T12:00:04",
                "renders_protection_failed",
                request_id="repair-protection",
                step="repair",
                detail="正式 renders 在返修期间发生变化，已停止",
            ),
            self._event(
                "2026-08-21T12:00:05",
                "step_failed",
                request_id="repair-protection",
                step="repair",
                detail="返修执行已停止，未自动重试",
            ),
        ]
        self._write_events(protection_failure)
        self.assertEqual(
            "queued",
            build_workflow_batch_status(self.repository_root, self.batch_id)["status"],
        )

    def test_pathological_json_and_non_scalar_truth_text_return_409(self) -> None:
        nested = "[" * 10_000 + "0" + "]" * 10_000
        self.ledger.write_text(
            '{"ts":"2026-08-21T11:00:00","event":"command_received",'
            '"request_id":"req-1","command":"workflow-production",'
            f'"nested":{nested}}}\n',
            encoding="utf-8",
        )
        before_nested = self._snapshot(self.repository_root)
        with self.assertRaises(WorkflowBatchStatusError) as nested_direct:
            build_workflow_batch_status(self.repository_root, self.batch_id)
        self.assertEqual(409, nested_direct.exception.http_status)
        nested_status, _headers, nested_body = self._get(self._status_path())
        self.assertEqual(409, nested_status)
        self.assertEqual({"ok": False, "error": "request_rejected"}, nested_body)
        self.assertEqual(before_nested, self._snapshot(self.repository_root))

        surrogate_events = [
            self._event(
                "2026-08-21T11:00:00",
                "command_received",
                request_id="req-1",
                command="workflow-production",
            ),
            self._event(
                "2026-08-21T11:00:01",
                "step_failed",
                request_id="req-1",
                detail="\ud800",
                failure_code="failure\udfff",
            ),
        ]
        self.ledger.write_text(
            "".join(
                json.dumps(event, ensure_ascii=True) + "\n"
                for event in surrogate_events
            ),
            encoding="utf-8",
        )
        before_surrogate = self._snapshot(self.repository_root)
        with self.assertRaises(WorkflowBatchStatusError) as surrogate_direct:
            build_workflow_batch_status(self.repository_root, self.batch_id)
        self.assertEqual(409, surrogate_direct.exception.http_status)
        surrogate_status, _headers, surrogate_body = self._get(self._status_path())
        self.assertEqual(409, surrogate_status)
        self.assertEqual({"ok": False, "error": "request_rejected"}, surrogate_body)
        self.assertEqual(before_surrogate, self._snapshot(self.repository_root))

    def test_oversized_json_and_structured_integers_fail_closed(self) -> None:
        oversized_digits = "9" * 5_000
        self.ledger.write_text(
            '{"ts":"2026-08-21T11:28:18","event":"production_paused",'
            f'"produced_count":{oversized_digits}}}\n',
            encoding="utf-8",
        )
        before = self._snapshot(self.repository_root)

        with self.assertRaises(WorkflowBatchStatusError) as captured:
            build_workflow_batch_status(self.repository_root, self.batch_id)

        self.assertEqual(409, captured.exception.http_status)
        self.assertIn("第 1 行损坏", str(captured.exception))
        self.assertEqual(before, self._snapshot(self.repository_root))

        self._write_events(
            [
                self._event(
                    "2026-08-21T11:28:18",
                    "production_paused",
                    produced_count=10_000,
                )
            ]
        )
        before = self._snapshot(self.repository_root)

        with self.assertRaises(WorkflowBatchStatusError) as structured:
            build_workflow_batch_status(self.repository_root, self.batch_id)

        self.assertEqual(409, structured.exception.http_status)
        self.assertIn("出图计数损坏", str(structured.exception))
        self.assertEqual(before, self._snapshot(self.repository_root))

    def test_oversized_render_detail_integer_fails_before_conversion(self) -> None:
        oversized_digits = "9" * 5_000
        self._write_events(
            [
                self._event(
                    "2026-08-21T11:28:18",
                    "step_started",
                    step="renders",
                    command="run: renders",
                ),
                self._event(
                    "2026-08-21T11:28:19",
                    "step_failed",
                    step="renders",
                    detail=f"渲染中止：成功 {oversized_digits}/计划 1（跳过 0）",
                ),
            ]
        )
        before = self._snapshot(self.repository_root)

        with self.assertRaises(WorkflowBatchStatusError) as captured:
            build_workflow_batch_status(self.repository_root, self.batch_id)

        self.assertEqual(409, captured.exception.http_status)
        self.assertIn("出图计数损坏", str(captured.exception))
        self.assertEqual(before, self._snapshot(self.repository_root))

    def test_manifest_is_not_required_and_status_reads_write_nothing(self) -> None:
        self._write_events(
            [
                self._event(
                    "2026-08-21T11:28:18",
                    "command_received",
                    request_id="req-1",
                    command="workflow-production",
                ),
                self._event(
                    "2026-08-21T11:28:19",
                    "step_started",
                    request_id="req-1",
                    step="identity",
                ),
            ]
        )
        manifest = self.manifests_root / f"{self.batch_id}.batch_manifest.json"
        self.assertFalse(manifest.exists())
        before = self._snapshot(self.repository_root)

        direct = build_workflow_batch_status(self.repository_root, self.batch_id)
        status, _headers, via_http = self._get(self._status_path())

        self.assertEqual(200, status)
        self.assertEqual(direct, via_http)
        self.assertFalse(manifest.exists())
        self.assertEqual(before, self._snapshot(self.repository_root))

    def test_missing_ledger_has_404_semantics_without_writes(self) -> None:
        before = self._snapshot(self.repository_root)
        with self.assertRaises(WorkflowBatchStatusError) as captured:
            build_workflow_batch_status(self.repository_root, self.batch_id)
        self.assertEqual(404, captured.exception.http_status)
        self.assertEqual("批次状态账本不存在。", str(captured.exception))

        status, _headers, body = self._get(self._status_path())

        self.assertEqual(404, status)
        self.assertEqual({"ok": False, "error": "request_rejected"}, body)
        self.assertEqual(before, self._snapshot(self.repository_root))

    def test_damaged_ledger_fails_closed_without_guessing_or_writes(self) -> None:
        self.ledger.write_text(
            json.dumps(
                self._event(
                    "2026-08-21T11:28:18",
                    "command_received",
                    request_id="req-1",
                    command="workflow-production",
                ),
                ensure_ascii=False,
            )
            + "\nnot-json\n",
            encoding="utf-8",
        )
        before = self._snapshot(self.repository_root)

        with self.assertRaises(WorkflowBatchStatusError) as captured:
            build_workflow_batch_status(self.repository_root, self.batch_id)
        self.assertEqual(409, captured.exception.http_status)
        self.assertIn("第 2 行损坏", str(captured.exception))

        status, _headers, body = self._get(self._status_path())
        self.assertEqual(409, status)
        self.assertEqual({"ok": False, "error": "request_rejected"}, body)
        self.assertEqual(before, self._snapshot(self.repository_root))

    def test_duplicate_json_keys_at_any_depth_fail_closed_without_writes(self) -> None:
        duplicate_rows = {
            "top-level event": (
                '{"ts":"2026-08-21T11:28:18","event":"command_received",'
                '"event":"step_started","request_id":"req-1",'
                '"command":"workflow-production"}'
            ),
            "top-level request": (
                '{"ts":"2026-08-21T11:28:18","event":"command_received",'
                '"request_id":"req-1","request_id":"req-2",'
                '"command":"workflow-production"}'
            ),
            "nested object": (
                '{"ts":"2026-08-21T11:28:18","event":"command_received",'
                '"request_id":"req-1","command":"workflow-production",'
                '"metadata":{"source":"first","source":"second"}}'
            ),
        }
        for case, row in duplicate_rows.items():
            with self.subTest(case=case):
                self.ledger.write_text(row + "\n", encoding="utf-8")
                before = self._snapshot(self.repository_root)

                with self.assertRaises(WorkflowBatchStatusError) as captured:
                    build_workflow_batch_status(self.repository_root, self.batch_id)
                self.assertEqual(409, captured.exception.http_status)
                self.assertIn("第 1 行损坏", str(captured.exception))

                status, _headers, body = self._get(self._status_path())
                self.assertEqual(409, status)
                self.assertEqual(
                    {"ok": False, "error": "request_rejected"},
                    body,
                )
                self.assertEqual(before, self._snapshot(self.repository_root))

    def test_authentication_and_origin_fail_before_status_read(self) -> None:
        for token in (None, "wrong-token"):
            with self.subTest(token=token):
                status, _headers, body = self._get(
                    self._status_path(),
                    token=token,
                )
                self.assertEqual(401, status)
                self.assertEqual({"ok": False, "error": "request_rejected"}, body)

        status, _headers, body = self._get(
            self._status_path(),
            origin="https://example.invalid",
        )
        self.assertEqual(403, status)
        self.assertEqual({"ok": False, "error": "request_rejected"}, body)
        self.assertFalse(self.ledger.exists())


if __name__ == "__main__":
    unittest.main()
