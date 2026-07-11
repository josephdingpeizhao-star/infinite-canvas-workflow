"""Read real batch state for canvas projection.

Mirrors scripts/detect_current_state.inspect_batch(), but accepts a manifest
at any filesystem path so demo/external batches never need to pollute the
repository ``manifests/`` directory. Read-only.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import detect_current_state  # noqa: E402


def read_batch_route(manifest_path: Path) -> dict[str, Any]:
    """Load a batch manifest from an arbitrary path and route it exactly like
    detect_current_state.inspect_batch(). Returns the route_batch result,
    which embeds the inputs/drafts/artifacts/outputs summaries."""
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError(f"batch manifest is not an object: {manifest_path}")
    return route_manifest(manifest, manifest_path)


def route_manifest(manifest: dict[str, Any], manifest_path: Path) -> dict[str, Any]:
    """Route an in-memory manifest dict (used both for reads and for the
    pre-write dry run of canvas edits)."""
    product_id = str(manifest.get("product_id") or manifest_path.stem)

    def section(name: str, defaults: dict[str, str]) -> dict[str, Any]:
        return {
            key: detect_current_state.summarize_path_values(
                ROOT,
                detect_current_state.values_from_manifest_or_default(manifest, name, key, product_id),
            )
            for key in defaults
        }

    inputs = section("inputs", detect_current_state.INPUT_DEFAULTS)
    drafts = section("drafts", detect_current_state.DRAFT_DEFAULTS)
    artifacts = section("artifacts", detect_current_state.ARTIFACT_DEFAULTS)
    outputs = section("outputs", detect_current_state.OUTPUT_DEFAULTS)
    route = detect_current_state.route_batch(product_id, manifest_path, manifest, inputs, drafts, artifacts, outputs)
    route["manifest_source_path"] = str(manifest_path)
    return route


def integrity_report_status(route: dict[str, Any]) -> dict[str, Any]:
    """Locate the final prompt integrity report the same way rendering does:
    manifest-declared qc_reports folder first, then repo reports/."""
    candidates: list[Path] = []
    qc_summary = (route.get("artifacts") or {}).get("qc_reports") or {}
    for entry in qc_summary.get("paths") or []:
        resolved = entry.get("resolved_path")
        if resolved:
            candidates.append(Path(resolved) / "final_prompt_integrity_report.json")
    product_id = route.get("product_id")
    if product_id:
        candidates.append(ROOT / "reports" / f"{product_id}_final_prompt_integrity_report.json")

    for candidate in candidates:
        target = candidate if candidate.is_absolute() else ROOT / candidate
        if not target.is_file():
            continue
        try:
            report = json.loads(target.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        return {
            "found": True,
            "path": str(target),
            "status": str(report.get("status") or ""),
            "render_blocked": report.get("render_blocked") is True,
        }
    return {"found": False, "path": "", "status": "", "render_blocked": False}
