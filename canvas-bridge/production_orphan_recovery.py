"""Recover workflow-production requests orphaned by a workbench restart."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from time import strftime
from typing import Any, Mapping

import run_controller
from workflow_batch_status import (
    WorkflowBatchStatusError,
    build_workflow_batch_status,
    build_workflow_batch_status_from_events,
)


LOGGER = logging.getLogger(__name__)

EVENT_LEDGER_SUFFIX = ".events.jsonl"
WORKBENCH_RESTART_FAILURE_CODE = "workbench_restart_interrupted"
WORKBENCH_RESTART_DETAIL = (
    "工作台重启中断了上次制作，已完成成果均已保留，可重新开始。"
)


def _result(batch_id: str, *, recovered: bool, reason: str) -> dict[str, Any]:
    return {
        "batch_id": batch_id,
        "recovered": recovered,
        "skipped": not recovered,
        "reason": reason,
    }


def _read_events(ledger: Path) -> list[Mapping[str, Any]]:
    lines = ledger.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


def _last_started_request_id(events: list[Mapping[str, Any]]) -> str | None:
    for event in reversed(events):
        if event.get("event") == "step_started":
            request_id = event.get("request_id")
            return request_id if type(request_id) is str else None
    return None


def recover_orphaned_productions(repository_root: Path) -> list[dict[str, Any]]:
    """Append one fail-closed terminal event for each startup-time running batch."""

    repository_root = Path(repository_root)
    manifests = repository_root / "manifests"
    results: list[dict[str, Any]] = []
    for ledger in sorted(manifests.glob(f"*{EVENT_LEDGER_SUFFIX}")):
        batch_id = ledger.name[: -len(EVENT_LEDGER_SUFFIX)]
        try:
            status = build_workflow_batch_status(repository_root, batch_id)
        except WorkflowBatchStatusError:
            results.append(_result(batch_id, recovered=False, reason="status_error"))
            continue

        if status.get("status") != "running":
            results.append(_result(batch_id, recovered=False, reason="not_running"))
            continue

        try:
            events = _read_events(ledger)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, RecursionError):
            results.append(_result(batch_id, recovered=False, reason="event_read_error"))
            continue

        request_id = _last_started_request_id(events)
        if request_id is None:
            results.append(
                _result(batch_id, recovered=False, reason="missing_modern_request_id")
            )
            continue

        failure_fields = {
            "request_id": request_id,
            "failure_code": WORKBENCH_RESTART_FAILURE_CODE,
            "detail": WORKBENCH_RESTART_DETAIL,
        }
        recovery_timestamp = strftime("%Y-%m-%dT%H:%M:%S")
        candidate_event = {
            "ts": recovery_timestamp,
            "event": "step_failed",
            **failure_fields,
        }
        try:
            dry_run_status = build_workflow_batch_status_from_events(
                batch_id,
                [*events, candidate_event],
            )
        except WorkflowBatchStatusError as error:
            LOGGER.warning(
                "workflow production orphan recovery dry-run failed for %s: %s",
                batch_id,
                error,
            )
            results.append(_result(batch_id, recovered=False, reason="dry_run_error"))
            continue
        if dry_run_status.get("status") != "failed":
            LOGGER.warning(
                "workflow production orphan recovery dry-run did not fail %s",
                batch_id,
            )
            results.append(
                _result(batch_id, recovered=False, reason="dry_run_not_failed")
            )
            continue

        run_controller.append_event(
            ledger,
            "step_failed",
            ts=recovery_timestamp,
            **failure_fields,
        )
        results.append(_result(batch_id, recovered=True, reason="recovered"))

    return results
