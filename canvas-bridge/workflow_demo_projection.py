"""Projection helpers for M1-b demo outputs.

The data-URI path is intentionally demo-only.  M2 real-image delivery will use
its own storage and transport design.  Cleanup is deliberately restricted to
``wfdemo-output:`` ids and is never part of normal startup.
"""

from __future__ import annotations

import base64
from typing import Any

from workflow_demo_executor import WorkflowDemoArtifact


OUTPUT_NODE_PREFIX = "wfdemo-output:"

MAIN_OFFSETS = (
    (0, -120),
    (0, 120),
    (205, -235),
    (205, 235),
    (410, -350),
    (410, 350),
)
DETAIL_OFFSETS = (
    (635, -430),
    (635, -150),
    (635, 150),
    (635, 430),
    (830, -570),
    (830, -285),
    (830, 285),
    (830, 570),
)


def output_node_id(machine_id: str, run_id: str, index: int) -> str:
    return f"{OUTPUT_NODE_PREFIX}{machine_id}:{run_id}:{index:02d}"


def _rect(node: dict[str, Any]) -> tuple[float, float, float, float] | None:
    position = node.get("position") or {}
    try:
        x = float(position["x"])
        y = float(position["y"])
        width = float(node.get("width") or 0)
        height = float(node.get("height") or 0)
    except (KeyError, TypeError, ValueError):
        return None
    return x, y, width, height


def _overlaps(first: dict[str, Any], second: dict[str, Any], gap: float = 20) -> bool:
    left = _rect(first)
    right = _rect(second)
    if left is None or right is None:
        return False
    ax, ay, aw, ah = left
    bx, by, bw, bh = right
    return not (
        ax + aw + gap <= bx - gap
        or bx + bw + gap <= ax - gap
        or ay + ah + gap <= by - gap
        or by + bh + gap <= ay - gap
    )


def _display_size(artifact: WorkflowDemoArtifact) -> tuple[int, int]:
    return (176, 176) if artifact.kind == "main" else (168, 224)


def _base_offset(artifact: WorkflowDemoArtifact) -> tuple[int, int]:
    offsets = MAIN_OFFSETS if artifact.kind == "main" else DETAIL_OFFSETS
    return offsets[artifact.ordinal - 1]


def find_output_position(
    machine: dict[str, Any],
    existing_nodes: list[dict[str, Any]],
    artifact: WorkflowDemoArtifact,
) -> dict[str, float]:
    position = machine.get("position") or {}
    machine_x = float(position.get("x") or 0)
    machine_y = float(position.get("y") or 0)
    machine_width = float(machine.get("width") or 420)
    machine_height = float(machine.get("height") or 300)
    width, height = _display_size(artifact)
    offset_x, offset_y = _base_offset(artifact)
    base_x = machine_x + machine_width + 140 + offset_x
    base_y = machine_y + machine_height / 2 + offset_y - height / 2
    vertical_lanes = (-3, -2, -1, 1, 2, 3)

    for attempt in range(241):
        column = 0 if attempt == 0 else (attempt + len(vertical_lanes) - 1) // len(vertical_lanes)
        lane = 0 if attempt == 0 else vertical_lanes[(attempt - 1) % len(vertical_lanes)]
        candidate = {
            "position": {"x": base_x + column * (width + 64), "y": base_y + lane * (height + 52)},
            "width": width,
            "height": height,
        }
        if not any(_overlaps(candidate, node) for node in existing_nodes):
            return candidate["position"]
    return {"x": base_x + 80 * (width + 64), "y": base_y}


def build_output_projection_ops(
    machine: dict[str, Any],
    existing_nodes: list[dict[str, Any]],
    run_id: str,
    artifact: WorkflowDemoArtifact,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    machine_id = str(machine.get("id") or "")
    if not machine_id:
        raise ValueError("工作流机器节点缺少 id")
    node_id = output_node_id(machine_id, run_id, artifact.index)
    display_width, display_height = _display_size(artifact)
    position = find_output_position(machine, [node for node in existing_nodes if node.get("id") != node_id], artifact)
    label = f"演示 · {'主图' if artifact.kind == 'main' else '详情'} {artifact.ordinal}"
    content = "data:image/png;base64," + base64.b64encode(artifact.path.read_bytes()).decode("ascii")
    projected = {
        "id": node_id,
        "type": "image",
        "title": label,
        "position": position,
        "width": display_width,
        "height": display_height,
        "metadata": {
            "content": content,
            "status": "success",
            "mimeType": "image/png",
            "naturalWidth": artifact.width,
            "naturalHeight": artifact.height,
            "workflowDemoOutput": {
                "workflowNodeId": machine_id,
                "runId": run_id,
                "index": artifact.index,
            },
        },
    }
    ops = [
        {"type": "delete_node", "ids": [node_id]},
        {
            "type": "add_node",
            "id": node_id,
            "nodeType": "image",
            "title": label,
            "position": position,
            "width": display_width,
            "height": display_height,
            "metadata": projected["metadata"],
        },
        {
            "type": "connect_nodes",
            "id": f"conn:{node_id}",
            "fromNodeId": machine_id,
            "toNodeId": node_id,
        },
    ]
    return ops, projected


def clear_workflow_demo_output_ids(nodes: list[dict[str, Any]], machine_id: str | None = None) -> list[str]:
    prefix = OUTPUT_NODE_PREFIX if machine_id is None else f"{OUTPUT_NODE_PREFIX}{machine_id}:"
    result: list[str] = []
    seen: set[str] = set()
    for node in nodes:
        node_id = str(node.get("id") or "")
        if node_id.startswith(prefix) and node_id not in seen:
            result.append(node_id)
            seen.add(node_id)
    return result
