"""Persist and restore canvas layout for a batch projection (phase 2).

Layout is pure UI state, keyed by workflow graph node id so it survives full
re-projection. Default location is ``manifests/<product_id>.canvas_layout.json``
in the repository (git-tracked, diff-friendly); an explicit path may override,
e.g. for demo workspaces.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
LAYOUT_VERSION = 1


def default_layout_path(product_id: str) -> Path:
    return ROOT / "manifests" / f"{product_id}.canvas_layout.json"


def build_layout(
    product_id: str,
    graph_id: str,
    canvas_nodes: list[dict[str, Any]],
    viewport: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Extract this batch's node geometry from a canvas_get_state node list.

    Canvas ids look like ``wf:<product_id>:<graph_node_id>``; anything else
    (stress nodes, images, other batches) is ignored."""
    prefix = f"wf:{product_id}:"
    nodes: dict[str, Any] = {}
    for node in canvas_nodes:
        node_id = str(node.get("id") or "")
        if not node_id.startswith(prefix):
            continue
        graph_node_id = node_id[len(prefix):]
        position = node.get("position") or {}
        entry: dict[str, Any] = {"position": {"x": position.get("x", 0), "y": position.get("y", 0)}}
        if isinstance(node.get("width"), (int, float)):
            entry["width"] = node["width"]
        if isinstance(node.get("height"), (int, float)):
            entry["height"] = node["height"]
        nodes[graph_node_id] = entry

    layout: dict[str, Any] = {
        "artifact_type": "canvas_layout",
        "layout_version": LAYOUT_VERSION,
        "product_id": product_id,
        "graph_id": graph_id,
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "nodes": nodes,
    }
    if viewport and all(key in viewport for key in ("x", "y", "k")):
        layout["viewport"] = {"x": viewport["x"], "y": viewport["y"], "k": viewport["k"]}
    return layout


def save_layout(path: Path, layout: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(layout, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_layout(path: Path) -> dict[str, Any] | None:
    """Return the layout dict, or None when the file does not exist."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if not isinstance(data, dict) or data.get("artifact_type") != "canvas_layout":
        raise ValueError(f"not a canvas_layout file: {path}")
    if data.get("layout_version") != LAYOUT_VERSION:
        raise ValueError(f"unsupported layout_version {data.get('layout_version')} in {path}")
    return data
