"""Canonical batch-id formatting and legacy-compatible stamp parsing.

New batch ids end in ``_YYYYMMDD_HHMMSS``. Historical ids ending in
``_YYYYMMDD`` remain readable and expose an empty time stamp.
"""

from __future__ import annotations

import re
from datetime import datetime


_BATCH_STAMP = re.compile(r"_(\d{8})(?:_(\d{6}))?$")


def format_batch_id(product_type: str, moment: datetime) -> str:
    return f"{product_type}_{moment:%Y%m%d_%H%M%S}"


def parse_batch_stamp(batch_id: str) -> tuple[str, str]:
    match = _BATCH_STAMP.search(batch_id)
    if match is None:
        return "", ""
    return match.group(1), match.group(2) or ""
