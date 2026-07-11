"""Project the workflow graph template plus a batch manifest into canvas ops.

Pure functions only. This module reads repository files and returns
``canvas_apply_ops`` payloads; it never writes anything. The canvas is a
projection target, never a source of truth.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
GRAPH_PATH = ROOT / "manifests" / "workflow_graph.template.json"

NODE_ID_PREFIX = "wf"

KIND_MARKS = {
    "input": "📥",
    "stage": "⚙️",
    "gate": "🚦",
    "artifact": "📦",
    "output": "🖼️",
}

NODE_SIZES = {
    "input": (280, 100),
    "stage": (320, 150),
    "gate": (320, 150),
    "artifact": (300, 130),
    "output": (280, 100),
}

X_GAP = 420
Y_GAP = 190


def load_graph(path: Path | None = None) -> dict[str, Any]:
    return json.loads((path or GRAPH_PATH).read_text(encoding="utf-8"))


def load_batch_manifest(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def batch_set_enabled(batch: dict[str, Any]) -> bool:
    declared = batch.get("user_declared_set_product") is True
    explicit = batch.get("explicit_set_request") is not None
    return batch.get("batch_type") == "set" and (declared or explicit)


def batch_requested(batch: dict[str, Any]) -> list[str]:
    requested = batch.get("requested_outputs")
    return [str(item) for item in requested] if isinstance(requested, list) else []


def condition_active(condition: dict[str, Any] | None, *, set_enabled: bool, requested: list[str]) -> bool:
    if not condition:
        return True
    if condition["when"] == "set_enabled":
        return set_enabled
    if condition["when"] == "requested_output":
        return condition["requested_output"] in requested
    raise ValueError(f"unknown condition: {condition}")


def active_subgraph(graph: dict[str, Any], batch: dict[str, Any]) -> tuple[dict[str, dict], list[dict]]:
    set_enabled = batch_set_enabled(batch)
    requested = batch_requested(batch)
    nodes = {
        node["id"]: node
        for node in graph["nodes"]
        if condition_active(node.get("condition"), set_enabled=set_enabled, requested=requested)
    }
    edges = [
        edge
        for edge in graph["edges"]
        if condition_active(edge.get("condition"), set_enabled=set_enabled, requested=requested)
        and edge["from"] in nodes
        and edge["to"] in nodes
    ]
    return nodes, edges


def longest_path_layers(nodes: dict[str, dict], edges: list[dict]) -> dict[str, int]:
    """Layer index per node id via longest-path layering (DAG assumed)."""
    incoming: dict[str, set[str]] = {node_id: set() for node_id in nodes}
    outgoing: dict[str, set[str]] = {node_id: set() for node_id in nodes}
    for edge in edges:
        incoming[edge["to"]].add(edge["from"])
        outgoing[edge["from"]].add(edge["to"])

    layer = {node_id: 0 for node_id in nodes}
    pending = {node_id: set(deps) for node_id, deps in incoming.items()}
    ready = sorted(node_id for node_id, deps in pending.items() if not deps)
    seen = set(ready)
    processed = 0
    while ready:
        current = ready.pop(0)
        processed += 1
        for target in sorted(outgoing[current]):
            layer[target] = max(layer[target], layer[current] + 1)
            pending[target].discard(current)
            if not pending[target] and target not in seen:
                seen.add(target)
                ready.append(target)
        ready.sort()
    if processed != len(nodes):
        raise ValueError("workflow graph contains a cycle")
    return layer


def manifest_location(batch: dict[str, Any], node: dict[str, Any]) -> str:
    section = node.get("manifest_section")
    key = node.get("manifest_key")
    if not section or not key:
        return ""
    value = (batch.get(section) or {}).get(key)
    if isinstance(value, list):
        value = value[0] if value else ""
    return str(value or "")


def node_content(batch: dict[str, Any], node: dict[str, Any]) -> str:
    lines = [f"kind: {node['kind']}"]
    for field in ("executor", "skill", "script", "artifact_type", "schema_ref"):
        if node.get(field):
            lines.append(f"{field}: {node[field]}")
    location = manifest_location(batch, node)
    if location:
        lines.append(f"path: {location}")
    if node.get("notes"):
        lines.append(f"notes: {node['notes']}")
    return "\n".join(lines)


def canvas_node_id(product_id: str, graph_node_id: str) -> str:
    return f"{NODE_ID_PREFIX}:{product_id}:{graph_node_id}"


def project_batch(graph: dict[str, Any], batch: dict[str, Any], *, origin_x: int = 80, origin_y: int = 80) -> list[dict[str, Any]]:
    """Return canvas ops that render the batch pipeline. Idempotent: deletes
    previously projected nodes for the same product before re-adding them."""
    product_id = str(batch.get("product_id") or "unknown")
    nodes, edges = active_subgraph(graph, batch)
    layers = longest_path_layers(nodes, edges)

    by_layer: dict[int, list[str]] = {}
    for node_id, layer_index in layers.items():
        by_layer.setdefault(layer_index, []).append(node_id)

    kind_rank = {"input": 0, "stage": 1, "gate": 1, "artifact": 2, "output": 3}
    ops: list[dict[str, Any]] = [
        {"type": "delete_node", "ids": [canvas_node_id(product_id, node_id) for node_id in nodes]}
    ]

    for layer_index in sorted(by_layer):
        members = sorted(by_layer[layer_index], key=lambda node_id: (kind_rank.get(nodes[node_id]["kind"], 9), node_id))
        for row, node_id in enumerate(members):
            node = nodes[node_id]
            width, height = NODE_SIZES[node["kind"]]
            metadata: dict[str, Any] = {
                "content": node_content(batch, node),
                "fontSize": 13,
                "workflowRef": {
                    "graph_id": graph.get("graph_id"),
                    "graph_version": graph.get("graph_version"),
                    "node_id": node_id,
                    "product_id": product_id,
                },
            }
            if node["kind"] in {"stage", "gate"}:
                metadata["status"] = "idle"
            ops.append(
                {
                    "type": "add_node",
                    "id": canvas_node_id(product_id, node_id),
                    "nodeType": "text",
                    "title": f"{KIND_MARKS[node['kind']]} {node['title']}",
                    "position": {"x": origin_x + layer_index * X_GAP, "y": origin_y + row * Y_GAP},
                    "width": width,
                    "height": height,
                    "metadata": metadata,
                }
            )

    for edge in edges:
        ops.append(
            {
                "type": "connect_nodes",
                "fromNodeId": canvas_node_id(product_id, edge["from"]),
                "toNodeId": canvas_node_id(product_id, edge["to"]),
            }
        )
    return ops


def stage_node_ids(graph: dict[str, Any], batch: dict[str, Any]) -> list[str]:
    """Canvas node ids of stage/gate nodes in layer order (for status demos)."""
    product_id = str(batch.get("product_id") or "unknown")
    nodes, edges = active_subgraph(graph, batch)
    layers = longest_path_layers(nodes, edges)
    ordered = sorted(
        (node_id for node_id in nodes if nodes[node_id]["kind"] in {"stage", "gate"}),
        key=lambda node_id: (layers[node_id], node_id),
    )
    return [canvas_node_id(product_id, node_id) for node_id in ordered]
