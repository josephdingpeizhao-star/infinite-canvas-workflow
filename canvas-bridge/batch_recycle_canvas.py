"""Exact batch-owned Canvas node selection and one-shot cleanup."""

from __future__ import annotations

from typing import Any, Mapping

import ic_client


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _belongs_to_batch(node: Mapping[str, Any], batch_id: str) -> bool:
    node_id = str(node.get("id") or "")
    owned_prefixes = (
        f"wf:{batch_id}:",
        f"wfedit:{batch_id}:",
        f"wfrun:{batch_id}:",
        f"wflog:{batch_id}:",
        f"wfprod-output:{batch_id}:",
        f"wfprod-repaired:{batch_id}:",
    )
    if node_id == f"wfprod-receiving:{batch_id}" or any(
        node_id.startswith(prefix) for prefix in owned_prefixes
    ):
        return True

    metadata = _mapping(node.get("metadata"))
    production_output = _mapping(metadata.get("workflowProductionOutput"))
    if production_output.get("batchId") == batch_id:
        return True
    workflow_ref = _mapping(metadata.get("workflowRef"))
    if (
        workflow_ref.get("product_id") == batch_id
        and workflow_ref.get("controller") in {"batch_run", "batch_events"}
    ):
        return True
    intake = _mapping(metadata.get("batchIntake"))
    receipt = _mapping(intake.get("receipt"))
    return (
        node.get("type") == "batch-info"
        and (receipt.get("batchId") == batch_id or intake.get("batchId") == batch_id)
    )


def batch_canvas_node_ids(
    nodes: list[Mapping[str, Any]],
    batch_id: str,
) -> list[str]:
    """Select only nodes with durable, exact ownership proof for one batch."""

    selected: list[str] = []
    seen: set[str] = set()
    for node in nodes:
        node_id = str(node.get("id") or "")
        if node_id and node_id not in seen and _belongs_to_batch(node, batch_id):
            selected.append(node_id)
            seen.add(node_id)
    return selected


def clear_batch_canvas_nodes(client: Any, batch_id: str) -> list[str]:
    state = client.call_tool("canvas_get_state")
    if (
        not isinstance(state, Mapping)
        or "nodes" not in state
        or not isinstance(state.get("nodes"), list)
    ):
        raise ic_client.CanvasAgentError("画布状态格式无效。")
    safe_nodes = [
        node for node in state["nodes"] if isinstance(node, Mapping)
    ]
    ids = batch_canvas_node_ids(safe_nodes, batch_id)
    if ids:
        client.apply_ops([{"type": "delete_node", "ids": ids}])
    return ids
