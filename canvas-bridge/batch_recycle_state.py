"""Shared, ledger-authoritative lifecycle state for batch recycling."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


RECYCLED_EVENT = "batch_recycled"
RESTORED_EVENT = "batch_restored"
CLOSED_EVENT = "batch_acceptance_closed"

RECYCLED_MESSAGE = "本批次已回收，不能执行会写入批次或产生生产副作用的操作。"
BUSY_MESSAGE = "本批次有任务正在运行，请等待任务结束后再试。"
LOCK_UNAVAILABLE_MESSAGE = "批次独占保护暂时不可用，本次操作已安全停止，未写入任何事件。"


class BatchLifecycleReadError(RuntimeError):
    """The audit ledger could not be read safely."""


@dataclass(frozen=True)
class BatchLifecycle:
    """The effective state after scanning the append-only journal in order."""

    recycled: bool
    closed: bool
    active_recycled_event: Mapping[str, Any] | None
    last_recycled_event: Mapping[str, Any] | None
    last_restored_event: Mapping[str, Any] | None

    @property
    def status(self) -> str:
        if self.recycled:
            return "recycled"
        if self.closed:
            return "closed"
        return "open"


def read_batch_lifecycle(journal_path: Path) -> BatchLifecycle:
    """Scan lifecycle events once; later restore events thaw an earlier recycle."""

    try:
        lines = journal_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    except (OSError, UnicodeError):
        raise BatchLifecycleReadError("批次账本暂时无法读取。") from None

    active_recycled: Mapping[str, Any] | None = None
    last_recycled: Mapping[str, Any] | None = None
    last_restored: Mapping[str, Any] | None = None
    closed = False
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            # Match the established acceptance scan: unrelated damaged lines do
            # not erase a valid lifecycle event that remains auditable.
            continue
        if not isinstance(value, Mapping):
            continue
        event = value.get("event")
        if event == RECYCLED_EVENT:
            active_recycled = value
            last_recycled = value
        elif event == RESTORED_EVENT and active_recycled is not None:
            active_recycled = None
            last_restored = value
        elif event == CLOSED_EVENT:
            closed = True
    return BatchLifecycle(
        recycled=active_recycled is not None,
        closed=closed,
        active_recycled_event=active_recycled,
        last_recycled_event=last_recycled,
        last_restored_event=last_restored,
    )
