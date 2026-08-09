"""Decide whether a production step is open for a declared batch type."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


SET_READY_STEPS: frozenset[str] = frozenset()  # ST-02 起逐步纳入
_SET_BATCH_BLOCKED_MESSAGE = (
    "套装批次的生产链路尚未开通，本批次已停在开始之前，"
    "未执行任何步骤，也未产生任何费用。"
)


def set_batch_blocked_message(
    manifest: Mapping[str, Any],
    step: str,
) -> str | None:
    if manifest.get("batch_type", "single") == "single" or step in SET_READY_STEPS:
        return None
    return _SET_BATCH_BLOCKED_MESSAGE
