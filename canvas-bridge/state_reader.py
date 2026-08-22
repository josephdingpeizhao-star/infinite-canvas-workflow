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

import runtime_roots


SCRIPTS = runtime_roots.PROGRAM_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import detect_current_state  # noqa: E402


def read_batch_route(
    manifest_path: Path,
    *,
    repository_root: Path | None = None,
) -> dict[str, Any]:
    """Load a batch manifest from an arbitrary path and route it exactly like
    detect_current_state.inspect_batch(). Returns the route_batch result,
    which embeds the inputs/drafts/artifacts/outputs summaries."""
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError(f"batch manifest is not an object: {manifest_path}")
    return route_manifest(manifest, manifest_path, repository_root=repository_root)


def route_manifest(
    manifest: dict[str, Any],
    manifest_path: Path,
    *,
    repository_root: Path | None = None,
) -> dict[str, Any]:
    """Route an in-memory manifest dict (used both for reads and for the
    pre-write dry run of canvas edits)."""
    product_id = str(manifest.get("product_id") or manifest_path.stem)
    data_repository_root = (
        repository_root.resolve()
        if repository_root is not None
        else runtime_roots.repository_root()
    )

    def section(name: str, defaults: dict[str, str]) -> dict[str, Any]:
        return {
            key: detect_current_state.summarize_path_values(
                data_repository_root,
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


def integrity_report_status(
    route: dict[str, Any],
    *,
    repository_root: Path | None = None,
) -> dict[str, Any]:
    """Locate the final prompt integrity report the same way rendering does:
    manifest-declared qc_reports folder first, then repo reports/."""
    data_repository_root = (
        repository_root.resolve()
        if repository_root is not None
        else runtime_roots.repository_root()
    )
    candidates: list[Path] = []
    qc_summary = (route.get("artifacts") or {}).get("qc_reports") or {}
    for entry in qc_summary.get("paths") or []:
        resolved = entry.get("resolved_path")
        if resolved:
            candidates.append(Path(resolved) / "final_prompt_integrity_report.json")
    product_id = route.get("product_id")
    if product_id:
        candidates.append(data_repository_root / "reports" / f"{product_id}_final_prompt_integrity_report.json")

    for candidate in candidates:
        target = candidate if candidate.is_absolute() else data_repository_root / candidate
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
