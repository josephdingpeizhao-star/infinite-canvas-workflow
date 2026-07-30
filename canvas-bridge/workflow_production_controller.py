"""Pure gates and selection rules for the M2-c real workflow machine.

This module deliberately does not invent a workflow route.  Every executable
step is parsed and authorised by :mod:`run_controller`; this layer only selects
the existing ``run``/``retry`` command text and enforces the M2-c stop after
QC.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import batch_editor
import run_controller
from category_recipes import DEFAULT_CATEGORY_KEY, load_category_recipe
from image_count_contract import default_image_counts


PRODUCTION_REQUESTED_OUTPUTS = ("main", "detail", "final_prompts")


class ProductionGateError(ValueError):
    """A real workflow request is unsafe or ambiguous and must stop."""


@dataclass(frozen=True)
class ProductionSelection:
    machine_id: str
    card_id: str
    batch_id: str
    material_count: int


def _default_total_images() -> int:
    recipe = load_category_recipe(
        Path(__file__).resolve().parent.parent,
        DEFAULT_CATEGORY_KEY,
    )
    return sum(default_image_counts(recipe.form))


def _validated_total_count(total_count: int | None) -> int:
    value = _default_total_images() if total_count is None else total_count
    if type(value) is not int or not 2 <= value <= 60:
        raise ProductionGateError("批次图片总数异常，真实制作已停止。")
    return value


def _node_id(node: Mapping[str, Any]) -> str:
    value = node.get("id")
    return value if isinstance(value, str) else ""


def _stored_image(node: Mapping[str, Any] | None) -> bool:
    if not isinstance(node, Mapping) or node.get("type") != "image":
        return False
    metadata = node.get("metadata") if isinstance(node.get("metadata"), Mapping) else {}
    return str(metadata.get("storageKey") or "").startswith("image:") and bool(metadata.get("content"))


def resolve_production_selection(machine_id: str, state: Mapping[str, Any]) -> ProductionSelection:
    """Resolve exactly one registered information card connected to a machine.

    A connected but unfinished card is an error rather than a reason to fall
    back to the M1 zero-cost demo.
    """

    nodes = [item for item in state.get("nodes", []) if isinstance(item, Mapping)]
    connections = [item for item in state.get("connections", []) if isinstance(item, Mapping)]
    machine = next(
        (item for item in nodes if _node_id(item) == machine_id and item.get("type") == "workflow"),
        None,
    )
    if machine is None:
        raise ProductionGateError("找不到这台工作流机器。")
    node_by_id = {_node_id(item): item for item in nodes if _node_id(item)}
    card_ids = {
        str(connection.get("fromNodeId"))
        for connection in connections
        if connection.get("toNodeId") == machine_id
        and (node_by_id.get(str(connection.get("fromNodeId"))) or {}).get("type") == "batch-info"
    }
    if not card_ids:
        raise ProductionGateError("这台机器没有连接批次信息卡。")
    if len(card_ids) != 1:
        raise ProductionGateError("一台真实工作流机器只能连接一张批次信息卡。")
    card_id = next(iter(card_ids))
    card = node_by_id[card_id]
    metadata = card.get("metadata") if isinstance(card.get("metadata"), Mapping) else {}
    intake = metadata.get("batchIntake") if isinstance(metadata.get("batchIntake"), Mapping) else {}
    receipt = intake.get("receipt") if isinstance(intake.get("receipt"), Mapping) else {}
    batch_id = receipt.get("batchId")
    if intake.get("status") != "completed" or not isinstance(batch_id, str) or not batch_id.strip():
        raise ProductionGateError("信息卡尚未登记完成，不能进入真实制作。")
    material_ids = {
        str(connection.get("fromNodeId"))
        for connection in connections
        if connection.get("toNodeId") == machine_id
        and _stored_image(node_by_id.get(str(connection.get("fromNodeId"))))
    }
    if not material_ids:
        raise ProductionGateError("请保留已登记信息卡，并把至少 1 张批次素材连到工作流机器。")
    return ProductionSelection(
        machine_id=machine_id,
        card_id=card_id,
        batch_id=batch_id.strip(),
        material_count=len(material_ids),
    )


def apply_production_requested_outputs(manifest_path: Path) -> dict[str, Any]:
    """Declare the standard production targets through the existing editor gate.

    Only the approved empty -> canonical transition is automatic.  A non-empty
    divergent list represents prior user intent and is never overwritten.
    """

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProductionGateError("批次清单无法读取，真实制作没有开始。") from exc
    if not isinstance(manifest, dict):
        raise ProductionGateError("批次清单格式无效，真实制作没有开始。")
    current = manifest.get("requested_outputs")
    if not isinstance(current, list):
        raise ProductionGateError("批次目标清单格式无效，真实制作没有开始。")
    canonical = list(PRODUCTION_REQUESTED_OUTPUTS)
    dormant_qc = [*canonical, "qc_reports"]
    if current == canonical or current == dormant_qc:
        return {"changed": False, "requested_outputs": list(current)}
    if current:
        raise ProductionGateError("批次已有不同的制作目标，未自动覆盖，请先人工核对。")
    try:
        result = batch_editor.apply_edits(manifest_path, {"requested_outputs": canonical})
    except batch_editor.EditValidationError as exc:
        raise ProductionGateError("批次目标没有通过既有编辑门禁，真实制作已停止。") from exc
    return {"changed": True, "requested_outputs": canonical, **result}


def next_gated_command(
    route: Mapping[str, Any],
    *,
    accepted_render_count: int,
    total_count: int | None = None,
) -> str | None:
    """Choose existing run-controller syntax; never authorise a step here."""

    expected_total = _validated_total_count(total_count)
    if not 0 <= accepted_render_count <= expected_total:
        raise ProductionGateError("正式图片计数异常，真实制作已停止。")
    stage = str(route.get("current_stage") or "")
    if stage == "needs_qc_reports":
        if accepted_render_count >= expected_total:
            return "run: qc"
        return "retry: renders"
    if stage == "ready":
        return None
    return "run: next"


def resolve_gated_step(command_text: str, route: dict[str, Any], integrity: dict[str, Any]) -> str:
    """Pass gate 1 and gate 2 to the existing run controller verbatim."""

    command = run_controller.parse_run_content(command_text)
    if command is None:
        raise ProductionGateError("真实制作命令为空。")
    try:
        return run_controller.resolve_command(command, route, integrity)
    except run_controller.RunValidationError as exc:
        raise ProductionGateError(str(exc)) from exc


def human_step_message(
    step: str,
    *,
    produced_count: int = 0,
    total_count: int | None = None,
) -> str:
    expected_total = _validated_total_count(total_count)
    if step in {"identity", "style_master"}:
        return "机器正在理解产品和想要的风格…"
    if step == "angle_inventory":
        return "正在检查哪些角度真正可用…"
    if step in {"main_vc", "detail_vc"}:
        return "正在安排主图和详情图…"
    if step == "final_prompts":
        return "正在整理每张图的制作说明…"
    if step == "integrity":
        return "正在做出图前的最后检查…"
    if step == "renders":
        return (
            f"正在制作第 {min(expected_total, produced_count + 1)}/"
            f"{expected_total} 张…"
        )
    if step == "qc":
        return f"正在逐张质检 {expected_total} 张成图…"
    return "机器正在继续处理…"
