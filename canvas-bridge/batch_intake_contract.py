"""Canonical payload-field contract shared by Canvas intake and the backend gate."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


class BatchIntakeContractError(ValueError):
    pass


def _contract_path(repository_root: Path) -> Path:
    return (
        repository_root.resolve()
        / "categories"
        / "_shared"
        / "batch-intake-contract.json"
    )


def load_batch_intake_contract(repository_root: Path) -> dict[str, Any]:
    try:
        value = json.loads(_contract_path(repository_root).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise BatchIntakeContractError("批次信息字段契约无法读取") from None
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "contract", "payload"}
        or value["schema_version"] != 4
        or value["contract"] != "canvas_batch_intake_payload"
        or not isinstance(value["payload"], dict)
    ):
        raise BatchIntakeContractError("批次信息字段契约无效")
    return value


def canonical_contract_bytes(repository_root: Path) -> bytes:
    value = load_batch_intake_contract(repository_root)
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def batch_intake_contract_sha256(repository_root: Path) -> str:
    return hashlib.sha256(canonical_contract_bytes(repository_root)).hexdigest()
