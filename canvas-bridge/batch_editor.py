"""Whitelist batch-manifest editing from the canvas (phase 3).

Flow: the projection includes an editor node (``wfedit:<pid>:batch``) whose
text renders the editable whitelist fields. The user edits that text on the
canvas; ``--apply-edits`` reads it back and runs three gates:

1. parse - only whitelist keys are accepted;
2. field validation - enum membership, duplicates, length caps;
3. dry run - the patched manifest is routed through the real route_batch()
   before anything is written.

Only when all gates pass is the manifest written atomically (temp + replace).
Nothing outside the whitelist is ever modified. Topology editing stays
forbidden in phase 3.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from batch_recycle_lock import BatchOperationBusy, existing_batch_operation
from batch_recycle_state import (
    BatchLifecycleReadError,
    read_batch_lifecycle,
)
import run_controller
import state_reader

EDITOR_PREFIX = "wfedit"
EDITABLE_KEYS = ("requested_outputs", "notes")
ALLOWED_OUTPUTS = ["main", "detail", "final_prompts", "qc_reports"]
NOTES_MAX_LEN = 2000


class EditValidationError(ValueError):
    pass


def editor_node_id(product_id: str) -> str:
    return f"{EDITOR_PREFIX}:{product_id}:batch"


def render_editor_content(manifest: dict[str, Any]) -> str:
    requested = manifest.get("requested_outputs") or []
    notes = str(manifest.get("notes") or "")
    return "\n".join(
        [
            "# 批次配置：仅下方白名单字段可编辑，改完后执行",
            "#   python canvas-bridge/spike_canvas_push.py --apply-edits <批次manifest>",
            f"requested_outputs: {', '.join(str(item) for item in requested)}",
            f"notes: {notes}",
        ]
    )


def editor_node_op(product_id: str, manifest: dict[str, Any], *, x: int = 80, y: int = -180) -> dict[str, Any]:
    return {
        "type": "add_node",
        "id": editor_node_id(product_id),
        "nodeType": "text",
        "title": "📝 批次配置（可编辑）",
        "position": {"x": x, "y": y},
        "width": 520,
        "height": 150,
        "metadata": {
            "content": render_editor_content(manifest),
            "fontSize": 13,
            "status": "success",
            "workflowRef": {"editor": "batch_manifest", "product_id": product_id},
        },
    }


def _split_key_value(line: str) -> tuple[str, str]:
    positions = [index for index in (line.find(":"), line.find("：")) if index != -1]
    if not positions:
        raise EditValidationError(f"无法解析的行（缺少冒号）：{line}")
    split_at = min(positions)
    return line[:split_at].strip(), line[split_at + 1 :].strip()


def parse_editor_content(text: str) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, value = _split_key_value(line)
        if key not in EDITABLE_KEYS:
            raise EditValidationError(f"不允许编辑的字段：{key}；白名单：{', '.join(EDITABLE_KEYS)}")
        if key in fields:
            raise EditValidationError(f"字段重复出现：{key}")
        fields[key] = value

    if not fields:
        raise EditValidationError("未发现可编辑字段（requested_outputs / notes）")

    if "requested_outputs" in fields:
        items = [item for item in re.split(r"[,，\s]+", fields["requested_outputs"]) if item]
        invalid = [item for item in items if item not in ALLOWED_OUTPUTS]
        if invalid:
            raise EditValidationError(
                f"requested_outputs 含非法值：{', '.join(invalid)}；允许：{', '.join(ALLOWED_OUTPUTS)}"
            )
        fields["requested_outputs"] = [item for item in ALLOWED_OUTPUTS if item in set(items)]

    if "notes" in fields and len(fields["notes"]) > NOTES_MAX_LEN:
        raise EditValidationError(f"notes 超过 {NOTES_MAX_LEN} 字符上限")

    return fields


def _apply_edits_active(manifest_path: Path, fields: dict[str, Any]) -> dict[str, Any]:
    """Gate 3 + atomic write. Returns a summary of the applied change."""
    illegal = [key for key in fields if key not in EDITABLE_KEYS]
    if illegal:
        raise EditValidationError(f"不允许编辑的字段：{', '.join(illegal)}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise EditValidationError(f"批次 manifest 不是对象：{manifest_path}")
    patched = dict(manifest)
    patched.update(fields)

    try:
        route = state_reader.route_manifest(patched, manifest_path)
    except Exception as exc:  # noqa: BLE001 - dry-run failure must block the write with the real reason.
        raise EditValidationError(f"修改后的 manifest 路由预演失败：{exc}") from exc

    temp_path = manifest_path.with_name(manifest_path.name + ".tmp")
    temp_path.write_text(json.dumps(patched, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp_path, manifest_path)

    return {
        "applied_fields": sorted(fields),
        "current_stage": route.get("current_stage"),
        "next_required_skill": route.get("next_required_skill"),
        "blocked_reasons": route.get("blocked_reasons"),
    }


def apply_edits(
    manifest_path: Path,
    fields: dict[str, Any],
    *,
    batch_lock_root: Path | None = None,
) -> dict[str, Any]:
    """Freeze direct manifest editing while the batch workspace is recycled."""

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise EditValidationError("批次 manifest 无法读取。") from None
    batch_id = (
        str(manifest.get("product_id") or "")
        if isinstance(manifest, dict)
        else ""
    )
    if not batch_id:
        raise EditValidationError("批次 manifest 缺少批次号。")
    journal = run_controller.journal_path(manifest_path, batch_id)
    try:
        with existing_batch_operation(
            batch_id,
            lock_root=batch_lock_root,
        ):
            try:
                lifecycle = read_batch_lifecycle(journal)
            except BatchLifecycleReadError:
                raise EditValidationError("批次账本暂时无法读取，配置未修改。") from None
            if lifecycle.recycled:
                raise EditValidationError("批次已回收，配置未修改。")
            return _apply_edits_active(manifest_path, fields)
    except BatchOperationBusy:
        raise EditValidationError("本批次有任务正在运行，配置未修改。") from None
