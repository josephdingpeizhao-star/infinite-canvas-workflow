"""Decide whether a production step is open for a declared batch type."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


SET_READY_STEPS: frozenset[str] = frozenset(
    {"identity", "style_master", "angle_inventory"}
)  # ST-03b 起继续纳入
_SET_BATCH_BLOCKED_MESSAGE = (
    "套装批次的后续生产工序尚未开通，本批次已停在未开通工序开始之前，"
    "未执行该工序，也未产生任何费用。"
)


def set_batch_blocked_message(
    manifest: Mapping[str, Any],
    step: str,
) -> str | None:
    batch_type = manifest.get("batch_type", "single")
    if batch_type == "single":
        return None
    if batch_type == "set" and step in SET_READY_STEPS:
        return None
    return _SET_BATCH_BLOCKED_MESSAGE
