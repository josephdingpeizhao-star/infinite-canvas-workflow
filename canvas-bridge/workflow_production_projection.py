"""Canvas-native projection for persisted M2-b production PNGs."""

from __future__ import annotations

import hashlib
import re
import struct
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from category_recipes import DEFAULT_CATEGORY_KEY, load_category_recipe
from image_count_contract import default_image_counts


OUTPUT_NODE_PREFIX = "wfprod-output:"
REPAIRED_NODE_PREFIX = "wfprod-repaired:"
OUTPUT_SOURCES = frozenset({"renders", "repaired"})
_CONFIG_ID = re.compile(r"^(main|detail)_([0-9]{2})$")


@dataclass(frozen=True)
class WorkflowProductionArtifact:
    batch_id: str
    config_id: str
    path: Path
    sha256: str
    width: int
    height: int
    byte_count: int
    source: str = "renders"

    @property
    def kind(self) -> str:
        return "main" if self.config_id.startswith("main_") else "detail"

    @property
    def ordinal(self) -> int:
        return int(self.config_id.rsplit("_", 1)[1])


def read_png_dimensions(path: Path) -> tuple[int, int]:
    header = path.read_bytes()[:24]
    if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise ValueError("正式图片不是有效 PNG")
    width, height = struct.unpack(">II", header[16:24])
    if width <= 0 or height <= 0:
        raise ValueError("正式图片尺寸无效")
    return width, height


def artifact_from_path(
    batch_id: str,
    path: Path,
    *,
    source: str = "renders",
) -> WorkflowProductionArtifact:
    if source not in OUTPUT_SOURCES:
        raise ValueError("图片来源不在白名单")
    config_id = path.stem
    match = _CONFIG_ID.fullmatch(config_id)
    if match is None or not 1 <= int(match.group(2)) <= 30:
        raise ValueError("正式图片名称不在支持的编号范围")
    width, height = read_png_dimensions(path)
    data = path.read_bytes()
    return WorkflowProductionArtifact(
        batch_id=batch_id,
        config_id=config_id,
        path=path,
        sha256=hashlib.sha256(data).hexdigest(),
        width=width,
        height=height,
        byte_count=len(data),
        source=source,
    )


def output_node_id(batch_id: str, config_id: str, source: str = "renders") -> str:
    if source not in OUTPUT_SOURCES:
        raise ValueError("图片来源不在白名单")
    prefix = OUTPUT_NODE_PREFIX if source == "renders" else REPAIRED_NODE_PREFIX
    return f"{prefix}{batch_id}:{config_id}"


def _rect(node: dict[str, Any]) -> tuple[float, float, float, float] | None:
    position = node.get("position") or {}
    try:
        return (
            float(position["x"]),
            float(position["y"]),
            float(node.get("width") or 0),
            float(node.get("height") or 0),
        )
    except (KeyError, TypeError, ValueError):
        return None


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


def _display_size(artifact: WorkflowProductionArtifact) -> tuple[int, int]:
    return (176, 176) if artifact.kind == "main" else (168, 224)


def _base_position(machine: dict[str, Any], artifact: WorkflowProductionArtifact) -> tuple[float, float]:
    position = machine.get("position") or {}
    machine_x = float(position.get("x") or 0)
    machine_y = float(position.get("y") or 0)
    machine_width = float(machine.get("width") or 420)
    machine_height = float(machine.get("height") or 300)
    width, height = _display_size(artifact)
    if artifact.kind == "main":
        row = artifact.ordinal - 1
        offset_x = (row // 2) * 205
        offset_y = (-120, 120)[row % 2]
    else:
        row = artifact.ordinal - 1
        offset_x = 635 + (row // 4) * 195
        offset_y = (-430, -150, 150, 430)[row % 4]
    source_offset_x = 1_090 if artifact.source == "repaired" else 0
    return (
        machine_x + machine_width + 140 + offset_x + source_offset_x,
        machine_y + machine_height / 2 + offset_y - height / 2,
    )


def find_output_position(
    machine: dict[str, Any],
    existing_nodes: list[dict[str, Any]],
    artifact: WorkflowProductionArtifact,
) -> dict[str, float]:
    width, height = _display_size(artifact)
    base_x, base_y = _base_position(machine, artifact)
    for attempt in range(241):
        column = attempt
        candidate = {
            "position": {"x": base_x + column * (width + 64), "y": base_y},
            "width": width,
            "height": height,
        }
        if not any(_overlaps(candidate, node) for node in existing_nodes):
            return candidate["position"]
    return {"x": base_x + 241 * (width + 64), "y": base_y}


def _download_url(base_url: str, artifact: WorkflowProductionArtifact) -> str:
    batch = urllib.parse.quote(artifact.batch_id, safe="")
    config = urllib.parse.quote(artifact.config_id, safe="")
    source = urllib.parse.quote(artifact.source, safe="")
    return f"{base_url.rstrip('/')}/workflow-production/{batch}/outputs/{source}/{config}"


def _output_proof(
    artifact: WorkflowProductionArtifact,
    base_url: str,
    machine_id: str | None = None,
    main_count: int | None = None,
) -> dict[str, Any]:
    if main_count is None:
        recipe = load_category_recipe(
            Path(__file__).resolve().parent.parent,
            DEFAULT_CATEGORY_KEY,
        )
        main_count = default_image_counts(recipe.form)[0]
    proof: dict[str, Any] = {
        "batchId": artifact.batch_id,
        "configId": artifact.config_id,
        "index": (
            artifact.ordinal
            if artifact.kind == "main"
            else artifact.ordinal + main_count
        ),
        "source": artifact.source,
        "sha256": artifact.sha256,
        "downloadUrl": _download_url(base_url, artifact),
        "byteCount": artifact.byte_count,
    }
    if machine_id:
        proof["workflowNodeId"] = machine_id
    return proof


def build_render_source_backfill_op(
    node: dict[str, Any],
    artifact: WorkflowProductionArtifact,
    base_url: str,
    *,
    main_count: int | None = None,
) -> dict[str, Any]:
    if artifact.source != "renders":
        raise ValueError("仅允许为正式图补齐来源")
    metadata = node.get("metadata")
    proof = metadata.get("workflowProductionOutput") if isinstance(metadata, dict) else None
    expected_id = output_node_id(artifact.batch_id, artifact.config_id)
    valid = (
        node.get("id") == expected_id
        and isinstance(proof, dict)
        and proof.get("batchId") == artifact.batch_id
        and proof.get("configId") == artifact.config_id
        and proof.get("sha256") == artifact.sha256
    )
    if valid:
        updated = dict(proof)
        updated.update(_output_proof(artifact, base_url, main_count=main_count))
        updated.pop("sourceBackfillStatus", None)
        updated.pop("sourceBackfillCode", None)
    else:
        updated = dict(proof) if isinstance(proof, dict) else {}
        updated.pop("source", None)
        updated["sourceBackfillStatus"] = "rejected"
        updated["sourceBackfillCode"] = "source_proof_mismatch"
    return {
        "type": "update_node",
        "id": str(node.get("id") or expected_id),
        "metadata": {"workflowProductionOutput": updated},
    }


def build_output_projection_ops(
    machine: dict[str, Any],
    existing_nodes: list[dict[str, Any]],
    artifact: WorkflowProductionArtifact,
    base_url: str,
    *,
    main_count: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    machine_id = str(machine.get("id") or "")
    if not machine_id:
        raise ValueError("工作流机器缺少 id")
    node_id = output_node_id(artifact.batch_id, artifact.config_id, artifact.source)
    existing = next((node for node in existing_nodes if node.get("id") == node_id), None)
    width, height = _display_size(artifact)
    position = (
        dict(existing.get("position") or {})
        if existing
        else find_output_position(machine, existing_nodes, artifact)
    )
    label_prefix = "返修·" if artifact.source == "repaired" else "真实 · "
    label = f"{label_prefix}{'主图' if artifact.kind == 'main' else '详情'} {artifact.ordinal}"
    output_metadata = _output_proof(
        artifact,
        base_url,
        machine_id,
        main_count=main_count,
    )
    metadata = {
        "content": "",
        "status": "loading",
        "mimeType": "image/png",
        "naturalWidth": artifact.width,
        "naturalHeight": artifact.height,
        "bytes": artifact.byte_count,
        "workflowProductionOutput": output_metadata,
    }
    projected = {
        "id": node_id,
        "type": "image",
        "title": label,
        "position": position,
        "width": width,
        "height": height,
        "metadata": metadata,
    }
    ops: list[dict[str, Any]] = []
    existing_metadata = existing.get("metadata") if isinstance(existing, dict) else {}
    existing_output = (
        existing_metadata.get("workflowProductionOutput")
        if isinstance(existing_metadata, dict)
        and isinstance(existing_metadata.get("workflowProductionOutput"), dict)
        else {}
    )
    persisted = (
        bool(existing_metadata.get("storageKey"))
        and existing_output.get("sha256") == artifact.sha256
        and existing_output.get("source", "renders") == artifact.source
    )
    if existing is None:
        ops.append(
            {
                "type": "add_node",
                "id": node_id,
                "nodeType": "image",
                "title": label,
                "position": position,
                "width": width,
                "height": height,
                "metadata": metadata,
            }
        )
    elif not persisted:
        ops.append({"type": "update_node", "id": node_id, "metadata": metadata})
    ops.append(
        {
            "type": "connect_nodes",
            "id": f"conn:{node_id}",
            "fromNodeId": machine_id,
            "toNodeId": node_id,
        }
    )
    return ops, projected
