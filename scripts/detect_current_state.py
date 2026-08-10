from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from detect_current_stage import (
    PERSISTENT_DOCS,
    STAGE3_DIRS,
    STAGE3_MANIFESTS,
    STAGE3_SCHEMAS,
    STAGE3_SCRIPTS,
    STAGE4_REPORTS,
    STAGE5_SCRIPTS,
    build_stage_plan,
)


ALL_SKILLS = [
    "workflow-router",
    "product-identity-archive",
    "style-master-extractor",
    "angle-inventory",
    "main-variable-config",
    "detail-variable-config",
    "final-prompt-compiler",
    "qc-inspector",
    "set-product-identity",
    "set-angle-layout-inventory",
    "set-variable-config-extension",
]

BASELINE_FILES = [
    *PERSISTENT_DOCS,
    *STAGE3_MANIFESTS,
    *STAGE3_SCHEMAS,
    *STAGE3_SCRIPTS,
    "scripts/validate_artifact_schema.py",
    "scripts/build_batch_manifest.py",
    *STAGE4_REPORTS,
    *STAGE5_SCRIPTS,
]

SET_SKILLS = {
    "set-product-identity",
    "set-angle-layout-inventory",
    "set-variable-config-extension",
}

EXTERNAL_WORKSPACE_MARKERS = {
    "manifests",
    "inputs",
    "artifacts",
    "outputs",
}

PROTECTED_REPOSITORY_NAMES = {
    ".agents",
    ".codex",
    "_archive",
    "docs",
    "manifests",
    "reports",
    "schemas",
    "scripts",
    "tests",
}

INPUT_DEFAULTS = {
    "white_bg_images": "inputs/products/{product_id}/white_bg",
    "style_reference_images": "inputs/products/{product_id}/style_refs",
    "set_group_images": "inputs/products/{product_id}/set_group",
    "component_white_bg_images": "inputs/products/{product_id}/component_white_bg",
}

DRAFT_DEFAULTS = {
    "product_identity_draft": "artifacts/{product_id}/drafts/product_identity_draft.md",
    "style_master_draft": "artifacts/{product_id}/drafts/style_master_draft.md",
}

ARTIFACT_DEFAULTS = {
    "asset_manifest": "manifests/{product_id}.asset_manifest.json",
    "product_identity_archive": "artifacts/{product_id}/identity",
    "style_master": "artifacts/{product_id}/style_master",
    "angle_inventory": "artifacts/{product_id}/angle_inventory",
    "main_variable_configs": "artifacts/{product_id}/variable_configs",
    "detail_variable_configs": "artifacts/{product_id}/variable_configs",
    "set_product_identity": "artifacts/{product_id}/identity",
    "set_angle_layout_inventory": "artifacts/{product_id}/angle_inventory",
    "final_prompts": "artifacts/{product_id}/final_prompts",
    "comfyui_jobs": "artifacts/{product_id}/comfyui_jobs",
    "qc_reports": "artifacts/{product_id}/qc_reports",
}

OUTPUT_DEFAULTS = {
    "renders": "artifacts/{product_id}/outputs/renders",
    "repaired": "artifacts/{product_id}/outputs/repaired",
}

ARTIFACT_TYPES = {
    "asset_manifest": {"asset_manifest"},
    "product_identity_archive": {"product_identity_archive"},
    "style_master": {"style_master"},
    "angle_inventory": {"angle_inventory"},
    "main_variable_configs": {"main_variable_config"},
    "detail_variable_configs": {"detail_variable_config"},
    "set_product_identity": {"set_product_identity"},
    "set_angle_layout_inventory": {"set_angle_layout_inventory"},
    "final_prompts": {"final_prompt"},
    "comfyui_jobs": {"comfyui_job"},
    "qc_reports": {"qc_report"},
}


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def rel(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def load_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def normalize_paths(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str) and item]
    return []


def content_files(path: Path) -> list[Path]:
    if path.is_file() and path.name != ".gitkeep":
        return [path]
    if not path.is_dir():
        return []
    return sorted(item for item in path.rglob("*") if item.is_file() and item.name != ".gitkeep")


def resolve_path(root: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return root / value


def resolved_path(path: Path) -> Path | None:
    try:
        return path.resolve()
    except OSError:
        return None


def same_or_nested(first: Path, second: Path) -> bool:
    first_resolved = resolved_path(first)
    second_resolved = resolved_path(second)
    if first_resolved is None or second_resolved is None:
        return False
    return (
        first_resolved == second_resolved
        or first_resolved in second_resolved.parents
        or second_resolved in first_resolved.parents
    )


def path_inside(path: Path, parent: Path) -> bool:
    path_resolved = resolved_path(path)
    parent_resolved = resolved_path(parent)
    if path_resolved is None or parent_resolved is None:
        return False
    return path_resolved == parent_resolved or parent_resolved in path_resolved.parents


def artifact_type_from_file(path: Path) -> str | None:
    if path.suffix.lower() != ".json":
        return None
    data = load_json(path)
    if isinstance(data, dict) and isinstance(data.get("artifact_type"), str):
        return data["artifact_type"]
    return None


def summarize_path_values(root: Path, values: list[str]) -> dict[str, Any]:
    entries = []
    total_files = 0
    typed_artifact_counts: dict[str, int] = {}

    for value in values:
        path = resolve_path(root, value)
        files = content_files(path)
        total_files += len(files)
        file_items = []
        for file_path in files:
            artifact_type = artifact_type_from_file(file_path)
            if artifact_type:
                typed_artifact_counts[artifact_type] = typed_artifact_counts.get(artifact_type, 0) + 1
            file_items.append(
                {
                    "path": rel(root, file_path),
                    "artifact_type": artifact_type,
                    "size": file_path.stat().st_size,
                }
            )
        entries.append(
            {
                "path": value.rstrip("/"),
                "resolved_path": rel(root, path),
                "exists": path.exists(),
                "is_dir": path.is_dir(),
                "file_count": len(files),
                "files": file_items,
            }
        )

    return {
        "paths": entries,
        "file_count": total_files,
        "typed_artifact_counts": dict(sorted(typed_artifact_counts.items())),
    }


def report_summary(root: Path, report_name: str) -> dict[str, Any]:
    path = root / "reports" / f"{report_name}.json"
    data = load_json(path)
    if not isinstance(data, dict):
        return {"path": rel(root, path), "exists": path.is_file(), "status": None}
    return {
        "path": rel(root, path),
        "exists": path.is_file(),
        "status": data.get("status"),
        "checked_at": data.get("checked_at"),
    }


def skill_tree_summary(root: Path) -> dict[str, Any]:
    report = load_json(root / "reports" / "skill_tree_report.json")
    if not isinstance(report, dict):
        return {
            "primary_skill_tree": ".agents/skills",
            "agents_exists": (root / ".agents" / "skills").is_dir(),
            "codex_exists": (root / ".codex" / "skills").is_dir(),
            "status": None,
        }
    roots = report.get("skill_roots", {})
    comparison = roots.get("comparison") or {}
    return {
        "status": report.get("status"),
        "primary_skill_tree": roots.get("primary_skill_tree"),
        "codex_skill_tree_role": (roots.get("codex_skill_tree") or {}).get("role"),
        "mirror_status": roots.get("mirror_status"),
        "missing_skill_count": len(report.get("missing_skills", [])),
        "missing_skill_md_count": len(report.get("missing_skill_md", [])),
        "missing_references_dir_count": len(report.get("missing_references_dir", [])),
        "changed_file_count": len(comparison.get("changed_files", [])),
        "extra_in_agents_count": len(comparison.get("extra_in_agents", [])),
        "missing_in_agents_count": len(comparison.get("missing_in_agents", [])),
    }


def reference_summary(root: Path) -> dict[str, Any]:
    report = load_json(root / "reports" / "reference_check_report.json")
    if not isinstance(report, dict):
        return {"status": None}
    return {
        "status": report.get("status"),
        "checked_skill_count": len(report.get("skills", [])),
        "missing_file_count": len(report.get("missing_files", [])),
        "extra_file_count": len(report.get("extra_files", [])),
        "misplaced_set_file_count": len(report.get("misplaced_set_files", [])),
    }


def file_group_summary(root: Path, directory: str, pattern: str = "*") -> dict[str, Any]:
    path = root / directory
    files = sorted(item for item in path.rglob(pattern) if item.is_file()) if path.is_dir() else []
    return {
        "path": directory,
        "exists": path.is_dir(),
        "file_count": len(files),
        "files": [rel(root, item) for item in files],
    }


def product_id_from_manifest_path(root: Path, manifest_path: Path) -> str:
    data = load_json(manifest_path)
    if isinstance(data, dict) and isinstance(data.get("product_id"), str) and data["product_id"]:
        return data["product_id"]
    return manifest_path.name.removesuffix(".batch_manifest.json")


def product_id_from_archived_manifest(root: Path, manifest_path: Path) -> str:
    data = load_json(manifest_path)
    if isinstance(data, dict) and isinstance(data.get("product_id"), str) and data["product_id"]:
        return data["product_id"]
    return manifest_path.name.split(".batch_manifest", 1)[0]


def product_dirs(base: Path) -> list[str]:
    if not base.is_dir():
        return []
    return sorted(item.name for item in base.iterdir() if item.is_dir() and item.name != "_template_product")


def product_ids_from_stage_reports(root: Path) -> dict[str, list[str]]:
    sources: dict[str, list[str]] = {}
    reports_dir = root / "reports"
    if not reports_dir.is_dir():
        return sources
    for report in sorted(reports_dir.glob("*_stage_*.json")):
        product_id = report.name.split("_stage_", 1)[0]
        if not product_id:
            continue
        sources.setdefault(product_id, []).append(rel(root, report))
    return sources


def previous_current_state_batch_ids(root: Path) -> list[str]:
    previous = load_json(root / "reports" / "current_state.json")
    if not isinstance(previous, dict):
        return []
    batches = previous.get("batches")
    if not isinstance(batches, list):
        return []
    ids = []
    for batch in batches:
        if isinstance(batch, dict) and isinstance(batch.get("product_id"), str) and batch["product_id"]:
            ids.append(batch["product_id"])
    return sorted(set(ids))


def manifest_workspace_summary(root: Path, manifest_path: Path) -> dict[str, Any]:
    product_id = product_id_from_manifest_path(root, manifest_path)
    data = load_json(manifest_path)
    workspace = data.get("workspace") if isinstance(data, dict) and isinstance(data.get("workspace"), dict) else {}
    workspace_root = workspace.get("root") if isinstance(workspace.get("root"), str) else None
    workspace_root_exists = None
    if workspace_root:
        workspace_root_exists = Path(workspace_root).exists()
    return {
        "product_id": product_id,
        "manifest_path": rel(root, manifest_path),
        "workspace_mode": workspace.get("mode"),
        "workspace_root": workspace_root,
        "workspace_root_exists": workspace_root_exists,
    }


def completed_manifest_product_ids(root: Path, product_ids: list[str]) -> list[str]:
    completed = []
    for product_id in sorted(set(product_ids)):
        reports_dir = root / "reports"
        if not reports_dir.is_dir():
            continue
        for path in sorted(reports_dir.glob(f"{product_id}_stage_12_*.json")):
            data = load_json(path)
            if isinstance(data, dict) and data.get("status") in {"pass", "pass_with_manual_review_recommended"}:
                completed.append(product_id)
                break
    return completed


def startup_hygiene_summary(root: Path, current_product_ids: list[str]) -> dict[str, Any]:
    manifest_paths = sorted(
        manifest
        for manifest in (root / "manifests").glob("*.batch_manifest.json")
        if manifest.name != "batch_manifest.template.json"
    )
    manifest_product_ids = sorted(set(product_id_from_manifest_path(root, manifest) for manifest in manifest_paths))
    input_dir_product_ids = product_dirs(root / "inputs" / "products")
    artifact_dir_product_ids = product_dirs(root / "artifacts")
    stage_report_sources = product_ids_from_stage_reports(root)
    stage_report_product_ids = sorted(stage_report_sources)
    previous_batch_ids = previous_current_state_batch_ids(root)
    current_ids = sorted(set(current_product_ids))
    current_id_set = set(current_ids)
    manifest_id_set = set(manifest_product_ids)
    directory_ids = sorted((set(input_dir_product_ids) | set(artifact_dir_product_ids)) - manifest_id_set)
    historical_report_only_ids = sorted(set(stage_report_product_ids) - current_id_set)
    previous_state_only_ids = sorted(set(previous_batch_ids) - current_id_set)

    archived_manifest_paths = sorted((root / "_archive").glob("**/*.batch_manifest*.json"))
    archived_manifest_ids = sorted(set(product_id_from_archived_manifest(root, path) for path in archived_manifest_paths))
    workspace_summaries = [manifest_workspace_summary(root, manifest) for manifest in manifest_paths]
    missing_workspace_roots = [
        item
        for item in workspace_summaries
        if item.get("workspace_mode") == "external" and item.get("workspace_root") and item.get("workspace_root_exists") is False
    ]
    completed_manifest_ids = completed_manifest_product_ids(root, manifest_product_ids)

    protected_audit_evidence = []
    for product_id in historical_report_only_ids:
        protected_audit_evidence.append(
            {
                "product_id": product_id,
                "source_type": "historical_stage_report",
                "file_count": len(stage_report_sources.get(product_id, [])),
                "sample_paths": stage_report_sources.get(product_id, [])[:5],
            }
        )
    for product_id in archived_manifest_ids:
        protected_audit_evidence.append(
            {
                "product_id": product_id,
                "source_type": "archived_stale_manifest",
                "file_count": len(
                    [
                        path
                        for path in archived_manifest_paths
                        if product_id_from_archived_manifest(root, path) == product_id
                    ]
                ),
            }
        )

    review_reasons = []
    if previous_state_only_ids:
        review_reasons.append("previous_current_state_contains_batches_not_found_by_fresh_scan")
    if directory_ids:
        review_reasons.append("repository_product_directories_without_batch_manifest")
    if historical_report_only_ids:
        review_reasons.append("historical_report_product_ids_are_audit_context_not_active_batches")
    if missing_workspace_roots:
        review_reasons.append("manifest_declared_external_workspace_root_missing")

    return {
        "status": "needs_review" if review_reasons else "pass",
        "mode": "report_only_no_delete",
        "review_reasons": review_reasons,
        "current_effective_batch_ids": current_ids,
        "current_batch_sources": {
            "manifest_product_ids": manifest_product_ids,
            "repository_input_dir_product_ids": input_dir_product_ids,
            "repository_artifact_dir_product_ids": artifact_dir_product_ids,
        },
        "previous_current_state_batch_ids": previous_batch_ids,
        "previous_state_only_batch_ids": previous_state_only_ids,
        "directory_residue_product_ids": directory_ids,
        "historical_report_only_product_ids": historical_report_only_ids,
        "archived_manifest_product_ids": archived_manifest_ids,
        "manifest_workspace_roots": workspace_summaries,
        "missing_manifest_workspace_roots": missing_workspace_roots,
        "completed_manifest_product_ids": completed_manifest_ids,
        "protected_audit_evidence": protected_audit_evidence,
        "cleanup_actions": [],
        "safe_cleanup_candidate_count": 0,
        "notes": (
            "Startup hygiene is conservative: it classifies active batch inputs, "
            "directory residues, stale previous state, and protected historical evidence without deleting files."
        ),
    }


def product_report_files(root: Path, product_id: str) -> list[Path]:
    reports_dir = root / "reports"
    if not reports_dir.is_dir():
        return []
    files: list[Path] = []
    for pattern in (f"{product_id}_*.json", f"{product_id}_*.md"):
        files.extend(path for path in reports_dir.glob(pattern) if path.is_file())
    return sorted(set(files))


def string_path_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        values: list[str] = []
        for item in value:
            values.extend(string_path_values(item))
        return values
    if isinstance(value, dict):
        values = []
        for item in value.values():
            values.extend(string_path_values(item))
        return values
    return []


def manifest_dependency_paths(root: Path, *, exclude_product_ids: set[str]) -> list[Path]:
    paths: list[Path] = []
    for manifest_path in sorted((root / "manifests").glob("*.batch_manifest.json")):
        if manifest_path.name == "batch_manifest.template.json":
            continue
        product_id = product_id_from_manifest_path(root, manifest_path)
        if product_id in exclude_product_ids:
            continue
        manifest = load_json(manifest_path)
        paths.append(manifest_path)
        if not isinstance(manifest, dict):
            continue
        for section_name in ("workspace", "inputs", "drafts", "artifacts", "outputs"):
            section = manifest.get(section_name)
            if section is None:
                continue
            for value in string_path_values(section):
                paths.append(resolve_path(root, value))
    return paths


def conflicts_with_protected_dependencies(path: Path, protected_paths: list[Path]) -> bool:
    return any(same_or_nested(path, protected) for protected in protected_paths)


def is_allowed_repository_cleanup_path(root: Path, path: Path, action_type: str) -> bool:
    resolved = resolved_path(path)
    root_resolved = resolved_path(root)
    if resolved is None or root_resolved is None:
        return False
    if not path_inside(resolved, root_resolved):
        return True

    if action_type == "abandoned_batch_manifest":
        return (
            resolved.parent == (root_resolved / "manifests")
            and resolved.name.endswith(".batch_manifest.json")
            and resolved.name != "batch_manifest.template.json"
        )
    if action_type == "abandoned_asset_manifest":
        return (
            resolved.parent == (root_resolved / "manifests")
            and resolved.name.endswith(".asset_manifest.json")
            and not resolved.name.endswith(".template.json")
        )
    if action_type == "historical_product_report":
        return resolved.parent == (root_resolved / "reports")
    if action_type == "abandoned_repository_input_dir":
        return path_inside(resolved, root_resolved / "inputs" / "products")
    if action_type == "abandoned_repository_artifact_dir":
        return path_inside(resolved, root_resolved / "artifacts")
    return False


def load_workspace_manifest(workspace_root: Path) -> dict[str, Any] | None:
    manifest = load_json(workspace_root / "manifests" / "batch_manifest.json")
    return manifest if isinstance(manifest, dict) else None


def workspace_manifest_points_to_root(manifest: dict[str, Any], workspace_root: Path) -> bool:
    workspace = manifest.get("workspace") if isinstance(manifest.get("workspace"), dict) else {}
    workspace_root_text = workspace.get("root") if isinstance(workspace.get("root"), str) else ""
    if not workspace_root_text:
        return False
    declared = resolved_path(Path(workspace_root_text))
    actual = resolved_path(workspace_root)
    return declared is not None and actual is not None and declared == actual


def has_external_workspace_markers(path: Path) -> bool:
    if not path.is_dir():
        return False
    return EXTERNAL_WORKSPACE_MARKERS.issubset({item.name for item in path.iterdir() if item.is_dir()})


def safe_external_workspace_root(root: Path, path: Path, product_id: str | None = None, manifest: dict[str, Any] | None = None) -> bool:
    resolved = resolved_path(path)
    if resolved is None or not resolved.exists() or not resolved.is_dir():
        return False
    root_resolved = resolved_path(root)
    if root_resolved is not None and path_inside(resolved, root_resolved):
        first_relative = resolved.relative_to(root_resolved).parts[0] if resolved != root_resolved else ""
        if first_relative in PROTECTED_REPOSITORY_NAMES:
            return False

    candidate_manifest = manifest if isinstance(manifest, dict) else load_workspace_manifest(resolved)
    if not isinstance(candidate_manifest, dict):
        return False
    if not has_external_workspace_markers(resolved):
        return False
    if not workspace_manifest_points_to_root(candidate_manifest, resolved):
        return False
    if product_id:
        declared_product_id = candidate_manifest.get("product_id")
        return declared_product_id == product_id
    return True


def workspace_product_id(path: Path) -> str:
    manifest = load_workspace_manifest(path)
    if isinstance(manifest, dict) and isinstance(manifest.get("product_id"), str) and manifest["product_id"]:
        return manifest["product_id"]
    return "unidentified_legacy_batch"


def external_workspace_parent_roots(root: Path) -> list[Path]:
    parents: set[Path] = set()
    for manifest_path in sorted((root / "manifests").glob("*.batch_manifest.json")):
        if manifest_path.name == "batch_manifest.template.json":
            continue
        data = load_json(manifest_path)
        workspace = data.get("workspace") if isinstance(data, dict) and isinstance(data.get("workspace"), dict) else {}
        workspace_root = workspace.get("root") if isinstance(workspace.get("root"), str) else ""
        if not workspace_root:
            continue
        parent = resolved_path(Path(workspace_root).parent)
        if parent is not None and parent.is_dir():
            parents.add(parent)
    return sorted(parents)


def manifest_declared_workspace_roots(root: Path) -> set[Path]:
    roots: set[Path] = set()
    for manifest_path in sorted((root / "manifests").glob("*.batch_manifest.json")):
        if manifest_path.name == "batch_manifest.template.json":
            continue
        data = load_json(manifest_path)
        workspace = data.get("workspace") if isinstance(data, dict) and isinstance(data.get("workspace"), dict) else {}
        workspace_root = workspace.get("root") if isinstance(workspace.get("root"), str) else ""
        resolved = resolved_path(Path(workspace_root)) if workspace_root else None
        if resolved is not None:
            roots.add(resolved)
    return roots


def unreferenced_external_workspace_roots(root: Path) -> list[Path]:
    referenced = manifest_declared_workspace_roots(root)
    candidates: list[Path] = []
    for parent in external_workspace_parent_roots(root):
        for child in sorted(item for item in parent.iterdir() if item.is_dir()):
            resolved = resolved_path(child)
            if resolved is None or resolved in referenced:
                continue
            if safe_external_workspace_root(root, resolved):
                candidates.append(resolved)
    return candidates


def startup_cleanup_candidates(
    root: Path,
    *,
    abandoned_product_ids: list[str],
    abandoned_paths: list[str] | None = None,
    delete_historical_product_reports: bool,
    historical_report_product_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen_paths: set[Path] = set()
    abandoned = sorted(set(item for item in abandoned_product_ids if item))
    protected_paths = manifest_dependency_paths(root, exclude_product_ids=set(abandoned))

    def add(path: Path, *, product_id: str, action_type: str, reason: str) -> None:
        resolved = resolved_path(path)
        if resolved is None:
            return
        if resolved in seen_paths or not resolved.exists():
            return
        if not is_allowed_repository_cleanup_path(root, resolved, action_type):
            return
        if conflicts_with_protected_dependencies(resolved, protected_paths):
            return
        seen_paths.add(resolved)
        candidates.append(
            {
                "product_id": product_id,
                "action_type": action_type,
                "path": str(path),
                "resolved_path": str(resolved),
                "reason": reason,
                "delete_mode": "recycle_bin",
            }
        )

    for product_id in abandoned:
        manifest_path = root / "manifests" / f"{product_id}.batch_manifest.json"
        manifest = load_json(manifest_path)
        add(
            manifest_path,
            product_id=product_id,
            action_type="abandoned_batch_manifest",
            reason="User confirmed this batch is no longer used.",
        )
        add(
            root / "manifests" / f"{product_id}.asset_manifest.json",
            product_id=product_id,
            action_type="abandoned_asset_manifest",
            reason="Companion asset manifest for an abandoned batch.",
        )
        add(
            root / "inputs" / "products" / product_id,
            product_id=product_id,
            action_type="abandoned_repository_input_dir",
            reason="Repository-local input directory for an abandoned batch.",
        )
        add(
            root / "artifacts" / product_id,
            product_id=product_id,
            action_type="abandoned_repository_artifact_dir",
            reason="Repository-local artifact directory for an abandoned batch.",
        )
        workspace = manifest.get("workspace") if isinstance(manifest, dict) and isinstance(manifest.get("workspace"), dict) else {}
        workspace_root = workspace.get("root") if isinstance(workspace.get("root"), str) else ""
        if workspace_root:
            external_root = Path(workspace_root)
            if safe_external_workspace_root(root, external_root, product_id, manifest if isinstance(manifest, dict) else None):
                add(
                    external_root,
                    product_id=product_id,
                    action_type="abandoned_external_workspace_root",
                    reason="Manifest-declared external workspace for an abandoned batch.",
                )

    for raw_path in sorted(set(abandoned_paths or [])):
        path = Path(raw_path)
        if not safe_external_workspace_root(root, path):
            continue
        product_id = workspace_product_id(path)
        add(
            path,
            product_id=product_id,
            action_type="abandoned_external_workspace_root_without_required_product_id",
            reason=(
                "Legacy external workspace is self-contained, has its own batch manifest, "
                "and is not referenced by any protected active batch."
            ),
        )

    for workspace_root in unreferenced_external_workspace_roots(root):
        product_id = workspace_product_id(workspace_root)
        add(
            workspace_root,
            product_id=product_id,
            action_type="unreferenced_external_workspace_root",
            reason=(
                "External workspace has a self-contained batch manifest but is not referenced "
                "by any current repository batch manifest."
            ),
        )

    report_product_ids = set(abandoned)
    report_product_ids.update(item for item in historical_report_product_ids or [] if item)
    if delete_historical_product_reports:
        report_product_ids.update(product_ids_from_stage_reports(root))
    for product_id in sorted(report_product_ids):
        for report_file in product_report_files(root, product_id):
            add(
                report_file,
                product_id=product_id,
                action_type="historical_product_report",
                reason="User confirmed historical reports should not remain audit context for residue detection.",
            )

    return candidates


def startup_cleanup_selection(
    root: Path,
    *,
    explicit_abandoned_product_ids: list[str],
    explicit_abandoned_paths: list[str] | None = None,
    delete_historical_product_reports: bool,
) -> dict[str, Any]:
    explicit_ids = sorted(set(item for item in explicit_abandoned_product_ids if item))
    explicit_paths = sorted(set(item for item in explicit_abandoned_paths or [] if item))
    if explicit_ids or explicit_paths:
        derived_ids = []
        for item in explicit_paths:
            path = Path(item)
            if safe_external_workspace_root(root, path):
                product_id = workspace_product_id(path)
                if product_id != "unidentified_legacy_batch":
                    derived_ids.append(product_id)
        explicit_ids = sorted(set(explicit_ids) | set(derived_ids))
        historical_report_ids = sorted(product_ids_from_stage_reports(root)) if delete_historical_product_reports else []
        return {
            "mode": "explicit",
            "automatic_detection_used": False,
            "explicit_abandoned_product_ids": explicit_ids,
            "explicit_abandoned_paths": explicit_paths,
            "abandoned_product_ids": explicit_ids,
            "abandoned_paths": explicit_paths,
            "historical_report_product_ids": historical_report_ids,
            "delete_historical_product_reports": delete_historical_product_reports,
            "skipped_protected_product_ids": [],
            "skipped_protected_reason": None,
        }

    summary = startup_hygiene_summary(root, discover_product_ids(root))
    missing_workspace_ids = sorted(
        item.get("product_id")
        for item in summary.get("missing_manifest_workspace_roots", [])
        if isinstance(item, dict) and item.get("product_id")
    )
    auto_abandoned_ids = sorted(
        set(summary.get("previous_state_only_batch_ids", []))
        | set(summary.get("directory_residue_product_ids", []))
        | set(summary.get("completed_manifest_product_ids", []))
        | set(missing_workspace_ids)
    )
    auto_historical_report_ids = sorted(summary.get("historical_report_only_product_ids", []))
    if delete_historical_product_reports:
        auto_historical_report_ids = sorted(set(auto_historical_report_ids) | set(product_ids_from_stage_reports(root)))
    auto_paths = [str(path) for path in unreferenced_external_workspace_roots(root)]

    return {
        "mode": "auto_detected",
        "automatic_detection_used": True,
        "explicit_abandoned_product_ids": [],
        "explicit_abandoned_paths": [],
        "abandoned_product_ids": auto_abandoned_ids,
        "abandoned_paths": auto_paths,
        "historical_report_product_ids": auto_historical_report_ids,
        "delete_historical_product_reports": delete_historical_product_reports,
        "skipped_protected_product_ids": sorted(summary.get("archived_manifest_product_ids", [])),
        "skipped_protected_reason": "Archived stale manifests are audit evidence and are not auto-cleaned.",
        "source_review_reasons": summary.get("review_reasons", []),
    }


def ordered_unique(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def path_status(root: Path, value: str, *, directory: bool = False) -> dict[str, Any]:
    path = root / value
    exists = path.is_dir() if directory else path.is_file()
    return {
        "path": value,
        "exists": exists,
        "type": "directory" if directory else "file",
    }


def required_directory_summary(root: Path) -> dict[str, Any]:
    items = [path_status(root, item, directory=True) for item in STAGE3_DIRS]
    return {
        "required_count": len(items),
        "confirmed_count": len([item for item in items if item["exists"]]),
        "missing": [item["path"] for item in items if not item["exists"]],
        "items": items,
    }


def required_file_summary(root: Path) -> dict[str, Any]:
    files = ordered_unique(BASELINE_FILES)
    items = [path_status(root, item) for item in files]
    return {
        "required_count": len(items),
        "confirmed_count": len([item for item in items if item["exists"]]),
        "confirmed": [item["path"] for item in items if item["exists"]],
        "missing": [item["path"] for item in items if not item["exists"]],
        "items": items,
    }


def skill_compatibility_detail(root: Path) -> dict[str, Any]:
    report = load_json(root / "reports" / "skill_tree_report.json")
    if not isinstance(report, dict):
        return {
            "status": None,
            "primary_skill_tree": ".agents/skills",
            "legacy_skill_tree": ".codex/skills",
            "needs_manual_review": True,
        }
    roots = report.get("skill_roots") if isinstance(report.get("skill_roots"), dict) else {}
    comparison = roots.get("comparison") if isinstance(roots.get("comparison"), dict) else {}
    return {
        "status": report.get("status"),
        "primary_skill_tree": roots.get("primary_skill_tree"),
        "legacy_skill_tree": (roots.get("codex_skill_tree") or {}).get("path"),
        "legacy_role": (roots.get("codex_skill_tree") or {}).get("role"),
        "mirror_status": roots.get("mirror_status"),
        "agents_file_count": comparison.get("agents_file_count"),
        "codex_file_count": comparison.get("codex_file_count"),
        "changed_files": comparison.get("changed_files", []),
        "missing_in_agents": comparison.get("missing_in_agents", []),
        "extra_in_agents": comparison.get("extra_in_agents", []),
        "needs_manual_review": bool(roots.get("needs_manual_review")) or roots.get("mirror_status") == "needs_manual_review",
    }


def collect_missing_files(root: Path, file_summary: dict[str, Any]) -> list[str]:
    missing = list(file_summary["missing"])
    skill_report = load_json(root / "reports" / "skill_tree_report.json")
    if isinstance(skill_report, dict):
        missing.extend(skill_report.get("missing_skill_md", []))
        missing.extend(skill_report.get("missing_references_dir", []))
    reference_report = load_json(root / "reports" / "reference_check_report.json")
    if isinstance(reference_report, dict):
        for item in reference_report.get("missing_files", []):
            missing.append(f".agents/skills/{item.get('skill')}/references/{item.get('file')}")
    return sorted(set(item for item in missing if item))


def collect_extra_files(root: Path) -> list[str]:
    extra: list[str] = []
    skill_report = load_json(root / "reports" / "skill_tree_report.json")
    if isinstance(skill_report, dict):
        roots = skill_report.get("skill_roots") if isinstance(skill_report.get("skill_roots"), dict) else {}
        comparison = roots.get("comparison") if isinstance(roots.get("comparison"), dict) else {}
        extra.extend(f".agents/skills/{item}" for item in comparison.get("extra_in_agents", []))
        extra.extend(f".codex/skills/{item}" for item in comparison.get("missing_in_agents", []))
    reference_report = load_json(root / "reports" / "reference_check_report.json")
    if isinstance(reference_report, dict):
        for item in reference_report.get("extra_files", []):
            extra.append(f".agents/skills/{item.get('skill')}/references/{item.get('file')}")
    return sorted(set(item for item in extra if item))


def collect_manual_review_items(root: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    skill_report = load_json(root / "reports" / "skill_tree_report.json")
    if isinstance(skill_report, dict):
        roots = skill_report.get("skill_roots") if isinstance(skill_report.get("skill_roots"), dict) else {}
        items.extend(roots.get("needs_manual_review", []) if isinstance(roots.get("needs_manual_review"), list) else [])
        migration = skill_report.get("migration_files") if isinstance(skill_report.get("migration_files"), dict) else {}
        items.extend(migration.get("needs_manual_review", []) if isinstance(migration.get("needs_manual_review"), list) else [])
        comparison = roots.get("comparison") if isinstance(roots.get("comparison"), dict) else {}
        for changed in comparison.get("changed_files", []):
            items.append({"path": changed.get("path"), "reason": ".agents and .codex Skill trees differ"})
    return items


def action_policy(stage_plan: dict[str, Any], validation_failed: list[str], batches: list[dict[str, Any]]) -> dict[str, list[str]]:
    forbidden = [
        "generate_images",
        "call_comfyui",
        "compile_final_prompts_before_variable_configs",
        "use_upstream_prompt_files_as_final_image_prompts",
        "rewrite_original_business_rule_files",
        "invent_product_facts_or_specs",
        "enable_set_product_skills_without_explicit_set_request",
        "render_without_final_prompt_integrity_gate",
    ]
    allowed = [
        "run_self_checks",
        "refresh_reports_current_state",
        "review_skill_tree_and_reference_reports",
    ]
    if validation_failed:
        allowed.append("fix_stage_1_to_stage_4_validation_failures")
        forbidden.extend(
            [
                "start_product_batch_intake",
                "generate_variable_configs",
                "prepare_render_jobs",
            ]
        )
    elif not batches:
        allowed.append("prepare_product_batch_intake_after_product_id_and_inputs_are_provided")
        forbidden.extend(
            [
                "generate_variable_configs_without_product_batch",
                "compile_final_prompts_without_upstream_artifacts",
                "run_qc_without_generated_images",
            ]
        )
    else:
        next_stage = stage_plan.get("next_stage") or {}
        allowed.append(f"advance_only_next_repository_gate_stage_{next_stage.get('stage')}")
    return {
        "allowed_next_actions": ordered_unique(allowed),
        "forbidden_next_actions": ordered_unique(forbidden),
    }


def discover_product_ids(root: Path) -> list[str]:
    ids: set[str] = set()
    for manifest in (root / "manifests").glob("*.batch_manifest.json"):
        if manifest.name == "batch_manifest.template.json":
            continue
        data = load_json(manifest)
        if isinstance(data, dict) and isinstance(data.get("product_id"), str) and data["product_id"]:
            ids.add(data["product_id"])
        else:
            ids.add(manifest.name.removesuffix(".batch_manifest.json"))

    for base in (root / "inputs" / "products", root / "artifacts"):
        if base.is_dir():
            for item in base.iterdir():
                if item.is_dir() and item.name != "_template_product":
                    ids.add(item.name)
    return sorted(ids)


def values_from_manifest_or_default(manifest: dict[str, Any] | None, section: str, key: str, product_id: str) -> list[str]:
    if isinstance(manifest, dict):
        section_values = manifest.get(section)
        if isinstance(section_values, dict) and key in section_values:
            return normalize_paths(section_values.get(key))

    defaults_by_section = {
        "inputs": INPUT_DEFAULTS,
        "drafts": DRAFT_DEFAULTS,
        "artifacts": ARTIFACT_DEFAULTS,
        "outputs": OUTPUT_DEFAULTS,
    }
    defaults = defaults_by_section.get(section, {})
    default = defaults.get(key)
    return [default.format(product_id=product_id)] if default else []


def artifact_present(summary: dict[str, Any], key: str) -> bool:
    typed_counts = summary["typed_artifact_counts"]
    expected_types = ARTIFACT_TYPES.get(key, set())
    if any(typed_counts.get(item, 0) > 0 for item in expected_types):
        return True

    file_count = summary["file_count"]
    if file_count == 0:
        return False

    # Dedicated artifact directories can be treated as present when they contain
    # untyped markdown/text artifacts. Shared variable_config folders cannot, and
    # qc_reports cannot either: the final prompt integrity gate writes its own
    # reports into that folder, and gate output must not count as completed QC.
    return key in {
        "asset_manifest",
        "product_identity_archive",
        "style_master",
        "angle_inventory",
        "final_prompts",
        "comfyui_jobs",
    }


def variable_config_present(summary: dict[str, Any], output_type: str) -> bool:
    typed_counts = summary["typed_artifact_counts"]
    expected_type = f"{output_type}_variable_config"
    if typed_counts.get(expected_type, 0) > 0:
        return True

    markers = {
        "main": ("main", "主图"),
        "detail": ("detail", "详情"),
    }[output_type]
    for entry in summary["paths"]:
        for file_item in entry["files"]:
            file_name = Path(file_item["path"]).name.lower()
            if any(marker.lower() in file_name for marker in markers):
                return True
    return False


def final_prompt_config_ids(summary: dict[str, Any], product_id: str) -> set[str] | None:
    has_materialized_files = False
    for entry in summary["paths"]:
        for file_item in entry["files"]:
            has_materialized_files = True
            if file_item.get("artifact_type") != "final_prompt_index":
                continue
            index_path = Path(str(file_item.get("path") or ""))
            if not index_path.is_absolute():
                index_path = project_root() / index_path
            index = load_json(index_path)
            if not isinstance(index, dict) or index.get("product_id") != product_id:
                continue
            items = index.get("items")
            if not isinstance(items, list) or index.get("prompt_count") != len(items) or not items:
                continue
            config_ids = [
                item.get("config_id")
                for item in items
                if isinstance(item, dict) and isinstance(item.get("config_id"), str) and item.get("config_id")
            ]
            if len(config_ids) == len(items) and len(set(config_ids)) == len(config_ids):
                return set(config_ids)
    # Older pure route-contract tests provide typed counts without filesystem
    # entries. Real summarized artifacts always include their materialized files.
    return set() if has_materialized_files else None


def generated_image_config_ids(outputs: dict[str, Any]) -> set[str]:
    config_ids: set[str] = set()
    for output_type in ("renders", "repaired"):
        for entry in outputs[output_type]["paths"]:
            for file_item in entry["files"]:
                file_path = Path(str(file_item.get("path") or ""))
                if file_path.suffix.lower() == ".png":
                    config_ids.add(file_path.stem)
    return config_ids


def route_batch(
    product_id: str,
    manifest_path: Path,
    manifest: dict[str, Any] | None,
    inputs: dict[str, Any],
    drafts: dict[str, Any],
    artifacts: dict[str, Any],
    outputs: dict[str, Any],
) -> dict[str, Any]:
    batch_type = "single"
    user_declared_set_product = False
    explicit_set_request = None
    requested_outputs: list[Any] = []

    if isinstance(manifest, dict):
        batch_type = manifest.get("batch_type") if manifest.get("batch_type") in {"single", "set"} else "single"
        user_declared_set_product = manifest.get("user_declared_set_product") is True
        explicit_set_request = manifest.get("explicit_set_request")
        requested_outputs = manifest.get("requested_outputs") if isinstance(manifest.get("requested_outputs"), list) else []

    set_enabled = batch_type == "set" and (user_declared_set_product or explicit_set_request is not None)
    allowed_skills = [skill for skill in ALL_SKILLS if set_enabled or skill not in SET_SKILLS]
    forbidden_skills = [] if set_enabled else sorted(SET_SKILLS)
    available_artifacts = []

    if artifact_present(artifacts["product_identity_archive"], "product_identity_archive"):
        available_artifacts.append("product_identity_archive")
    if artifact_present(artifacts["asset_manifest"], "asset_manifest"):
        available_artifacts.append("asset_manifest")
    if artifact_present(artifacts["style_master"], "style_master"):
        available_artifacts.append("style_master")
    if artifact_present(artifacts["angle_inventory"], "angle_inventory"):
        available_artifacts.append("angle_inventory")
    if variable_config_present(artifacts["main_variable_configs"], "main"):
        available_artifacts.append("main_variable_configs")
    if variable_config_present(artifacts["detail_variable_configs"], "detail"):
        available_artifacts.append("detail_variable_configs")
    if set_enabled and artifact_present(artifacts["set_product_identity"], "set_product_identity"):
        available_artifacts.append("set_product_identity")
    if set_enabled and artifact_present(artifacts["set_angle_layout_inventory"], "set_angle_layout_inventory"):
        available_artifacts.append("set_angle_layout_inventory")
    if artifact_present(artifacts["final_prompts"], "final_prompts"):
        available_artifacts.append("final_prompts")
    if artifact_present(artifacts["comfyui_jobs"], "comfyui_jobs"):
        available_artifacts.append("comfyui_jobs")
    if artifact_present(artifacts["qc_reports"], "qc_reports"):
        available_artifacts.append("qc_reports")

    missing_required_artifacts: list[str] = []
    blocked_reasons: list[str] = []
    next_skill: str | None = None
    current_stage = "ready"

    if manifest is None:
        current_stage = "missing_batch_manifest"
        missing_required_artifacts.append("batch_manifest")
        blocked_reasons.append(f"No batch manifest found at {rel(manifest_path.parents[1], manifest_path)}.")
    elif "product_identity_archive" not in available_artifacts:
        current_stage = "needs_product_identity_archive"
        next_skill = "product-identity-archive"
        missing_required_artifacts.append("product_identity_archive")
        source_file_count = (
            inputs["white_bg_images"]["file_count"]
            + inputs["style_reference_images"]["file_count"]
            + inputs["set_group_images"]["file_count"]
            + inputs["component_white_bg_images"]["file_count"]
            + drafts["product_identity_draft"]["file_count"]
            + drafts["style_master_draft"]["file_count"]
        )
        if source_file_count == 0 and not manifest.get("notes"):
            blocked_reasons.append("No product source inputs or manifest notes were found for product identity extraction.")
    elif "style_master" not in available_artifacts:
        current_stage = "needs_style_master"
        next_skill = "style-master-extractor"
        missing_required_artifacts.append("style_master")
        if inputs["style_reference_images"]["file_count"] == 0 and drafts["style_master_draft"]["file_count"] == 0:
            blocked_reasons.append("No style reference images found for style master extraction.")
    elif not set_enabled and "angle_inventory" not in available_artifacts:
        current_stage = "needs_angle_inventory"
        next_skill = "angle-inventory"
        missing_required_artifacts.append("angle_inventory")
        if inputs["white_bg_images"]["file_count"] == 0:
            blocked_reasons.append("No single-product white-background images found for angle inventory.")
    elif set_enabled and "set_product_identity" not in available_artifacts:
        current_stage = "needs_set_product_identity"
        next_skill = "set-product-identity"
        missing_required_artifacts.append("set_product_identity")
    elif set_enabled and "set_angle_layout_inventory" not in available_artifacts:
        current_stage = "needs_set_angle_layout_inventory"
        next_skill = "set-angle-layout-inventory"
        missing_required_artifacts.append("set_angle_layout_inventory")
        if inputs["set_group_images"]["file_count"] == 0:
            blocked_reasons.append("No set group images found for set angle/layout inventory.")
    elif "main" in requested_outputs and "main_variable_configs" not in available_artifacts:
        current_stage = "needs_main_variable_configs"
        next_skill = "main-variable-config"
        missing_required_artifacts.append("main_variable_configs")
    elif "detail" in requested_outputs and "detail_variable_configs" not in available_artifacts:
        current_stage = "needs_detail_variable_configs"
        next_skill = "detail-variable-config"
        missing_required_artifacts.append("detail_variable_configs")
    elif "final_prompts" in requested_outputs and "final_prompts" not in available_artifacts:
        current_stage = "needs_final_prompts"
        next_skill = "final-prompt-compiler"
        missing_required_artifacts.append("final_prompts")
    elif "qc_reports" in requested_outputs and "qc_reports" not in available_artifacts:
        render_targets = final_prompt_config_ids(artifacts["final_prompts"], product_id)
        generated_image_count = outputs["renders"]["file_count"] + outputs["repaired"]["file_count"]
        if render_targets is None:
            render_coverage_complete = bool(generated_image_count)
        else:
            render_coverage_complete = bool(render_targets) and render_targets.issubset(
                generated_image_config_ids(outputs)
            )
        if render_coverage_complete:
            current_stage = "needs_qc_reports"
            next_skill = "qc-inspector"
            missing_required_artifacts.append("qc_reports")
        else:
            current_stage = "needs_generated_images_before_qc"
            next_skill = None
            missing_required_artifacts.append("generated_images")
            if generated_image_count:
                blocked_reasons.append(
                    "QC is post-generation only; generated image coverage is incomplete for the final prompt config list."
                )
            else:
                blocked_reasons.append(
                    "QC is post-generation only; no generated images were detected in manifest-declared outputs."
                )
    elif (
        "qc_reports" not in requested_outputs
        and "final_prompts" in available_artifacts
        and ("main" in requested_outputs or "detail" in requested_outputs)
    ):
        render_targets = final_prompt_config_ids(artifacts["final_prompts"], product_id)
        generated_image_count = outputs["renders"]["file_count"] + outputs["repaired"]["file_count"]
        if render_targets is None:
            render_coverage_complete = bool(generated_image_count)
        else:
            render_coverage_complete = bool(render_targets) and render_targets.issubset(
                generated_image_config_ids(outputs)
            )
        if not render_coverage_complete:
            current_stage = "needs_generated_images_before_qc"
            next_skill = None
            missing_required_artifacts.append("generated_images")
            if generated_image_count:
                blocked_reasons.append("尚有图片未生成，需先完成出图。")
            else:
                blocked_reasons.append("尚未生成任何图片，需先完成出图。")
    elif not requested_outputs:
        current_stage = "awaiting_requested_outputs"
        next_skill = None
        blocked_reasons.append("requested_outputs is empty; variable config, final prompt, and QC targets are not selected.")

    return {
        "product_id": product_id,
        "manifest_path": rel(manifest_path.parents[1], manifest_path) if manifest_path.exists() else rel(project_root(), manifest_path),
        "manifest_exists": manifest is not None,
        "batch_type": batch_type,
        "user_declared_set_product": user_declared_set_product,
        "explicit_set_request": explicit_set_request,
        "requested_outputs": requested_outputs,
        "current_stage": current_stage,
        "next_skill": None if blocked_reasons else next_skill,
        "next_required_skill": next_skill,
        "available_artifacts": available_artifacts,
        "missing_required_artifacts": missing_required_artifacts,
        "blocked_reasons": blocked_reasons,
        "allowed_skills": allowed_skills,
        "forbidden_skills": forbidden_skills,
        "inputs": inputs,
        "drafts": drafts,
        "artifacts": artifacts,
        "outputs": outputs,
    }


def inspect_batch(root: Path, product_id: str) -> dict[str, Any]:
    manifest_path = root / "manifests" / f"{product_id}.batch_manifest.json"
    manifest = load_json(manifest_path)
    manifest_dict = manifest if isinstance(manifest, dict) else None

    inputs = {
        key: summarize_path_values(root, values_from_manifest_or_default(manifest_dict, "inputs", key, product_id))
        for key in INPUT_DEFAULTS
    }
    drafts = {
        key: summarize_path_values(root, values_from_manifest_or_default(manifest_dict, "drafts", key, product_id))
        for key in DRAFT_DEFAULTS
    }
    artifacts = {
        key: summarize_path_values(root, values_from_manifest_or_default(manifest_dict, "artifacts", key, product_id))
        for key in ARTIFACT_DEFAULTS
    }
    outputs = {
        key: summarize_path_values(root, values_from_manifest_or_default(manifest_dict, "outputs", key, product_id))
        for key in OUTPUT_DEFAULTS
    }
    return route_batch(product_id, manifest_path, manifest_dict, inputs, drafts, artifacts, outputs)


def write_markdown(path: Path, report: dict[str, Any]) -> None:
    current_stage_judgment = report.get("current_stage_judgment") or {}
    last_completed_stage = report.get("last_completed_stage") or {}
    next_stage = report.get("next_stage") or {}
    lines = [
        "# Current State",
        "",
        f"- status: {report['status']}",
        f"- checked_at: {report['checked_at']}",
        f"- current_stage: {report['current_stage']}",
        f"- current_stage_judgment: Stage {current_stage_judgment.get('stage')} - {current_stage_judgment.get('name')} ({current_stage_judgment.get('status')})",
        f"- last_completed_stage: Stage {last_completed_stage.get('stage')} - {last_completed_stage.get('name')}",
        f"- next_stage: Stage {next_stage.get('stage')} - {next_stage.get('name')} ({next_stage.get('status')})",
        f"- next_skill: {report['next_skill'] or 'None'}",
        f"- active_batch_count: {len(report['batches'])}",
        "",
        "## Allowed Next Actions",
        "",
    ]
    lines.extend(f"- {item}" for item in report.get("allowed_next_actions", []) or ["None"])
    lines.extend(
        [
            "",
            "## Forbidden Next Actions",
            "",
        ]
    )
    lines.extend(f"- {item}" for item in report.get("forbidden_next_actions", []) or ["None"])
    lines.extend([
        "",
        "## Validation Reports",
        "",
    ])
    for name, item in report["validation_reports"].items():
        lines.append(f"- {name}: status={item['status']}, path={item['path']}")

    lines.extend(
        [
            "",
            "## Skill Tree",
            "",
            f"- primary_skill_tree: {report['skill_tree'].get('primary_skill_tree')}",
            f"- codex_skill_tree_role: {report['skill_tree'].get('codex_skill_tree_role')}",
            f"- mirror_status: {report['skill_tree'].get('mirror_status')}",
            f"- changed_file_count: {report['skill_tree'].get('changed_file_count')}",
            f"- compatibility_status: {report['codex_agents_compatibility'].get('mirror_status')}",
            "",
            "## References",
            "",
            f"- status: {report['references'].get('status')}",
            f"- checked_skill_count: {report['references'].get('checked_skill_count')}",
            f"- missing_file_count: {report['references'].get('missing_file_count')}",
            f"- extra_file_count: {report['references'].get('extra_file_count')}",
            f"- misplaced_set_file_count: {report['references'].get('misplaced_set_file_count')}",
            "",
            "## Missing Required Artifacts",
            "",
        ]
    )
    if report["missing_required_artifacts"]:
        lines.extend(f"- {item}" for item in report["missing_required_artifacts"])
    else:
        lines.append("- None")

    lines.extend(["", "## Blocked Reasons", ""])
    if report["blocked_reasons"]:
        lines.extend(f"- {item}" for item in report["blocked_reasons"])
    else:
        lines.append("- None")

    lines.extend(["", "## Needs Manual Review", ""])
    if report["needs_manual_review"]:
        lines.extend(f"- {item.get('path')}: {item.get('reason')}" for item in report["needs_manual_review"])
    else:
        lines.append("- None")

    lines.extend(["", "## Missing Files", ""])
    if report["missing_files"]:
        lines.extend(f"- {item}" for item in report["missing_files"])
    else:
        lines.append("- None")

    lines.extend(["", "## Extra Files", ""])
    if report["extra_files"]:
        lines.extend(f"- {item}" for item in report["extra_files"])
    else:
        lines.append("- None")

    startup_hygiene = report.get("startup_hygiene")
    if isinstance(startup_hygiene, dict):
        cleanup_selection = (
            startup_hygiene.get("cleanup_selection")
            if isinstance(startup_hygiene.get("cleanup_selection"), dict)
            else None
        )
        lines.extend(
            [
                "",
                "## Startup Hygiene",
                "",
                f"- status: {startup_hygiene.get('status')}",
                f"- mode: {startup_hygiene.get('mode')}",
                f"- review_reasons: {', '.join(startup_hygiene.get('review_reasons', [])) if startup_hygiene.get('review_reasons') else 'None'}",
                f"- current_effective_batch_ids: {', '.join(startup_hygiene.get('current_effective_batch_ids', [])) if startup_hygiene.get('current_effective_batch_ids') else 'None'}",
                f"- previous_state_only_batch_ids: {', '.join(startup_hygiene.get('previous_state_only_batch_ids', [])) if startup_hygiene.get('previous_state_only_batch_ids') else 'None'}",
                f"- directory_residue_product_ids: {', '.join(startup_hygiene.get('directory_residue_product_ids', [])) if startup_hygiene.get('directory_residue_product_ids') else 'None'}",
                f"- historical_report_only_product_ids: {', '.join(startup_hygiene.get('historical_report_only_product_ids', [])) if startup_hygiene.get('historical_report_only_product_ids') else 'None'}",
                f"- completed_manifest_product_ids: {', '.join(startup_hygiene.get('completed_manifest_product_ids', [])) if startup_hygiene.get('completed_manifest_product_ids') else 'None'}",
                f"- missing_manifest_workspace_roots: {len(startup_hygiene.get('missing_manifest_workspace_roots', []))}",
                f"- protected_audit_evidence_count: {len(startup_hygiene.get('protected_audit_evidence', []))}",
                f"- cleanup_actions: {len(startup_hygiene.get('cleanup_actions', []))}",
                f"- safe_cleanup_candidate_count: {startup_hygiene.get('safe_cleanup_candidate_count', 0)}",
            ]
        )
        if cleanup_selection:
            lines.extend(
                [
                    f"- cleanup_selection_mode: {cleanup_selection.get('mode')}",
                    f"- cleanup_abandoned_product_ids: {', '.join(cleanup_selection.get('abandoned_product_ids', [])) if cleanup_selection.get('abandoned_product_ids') else 'None'}",
                    f"- cleanup_abandoned_paths: {', '.join(cleanup_selection.get('abandoned_paths', [])) if cleanup_selection.get('abandoned_paths') else 'None'}",
                    f"- cleanup_historical_report_product_ids: {', '.join(cleanup_selection.get('historical_report_product_ids', [])) if cleanup_selection.get('historical_report_product_ids') else 'None'}",
                    f"- cleanup_skipped_protected_product_ids: {', '.join(cleanup_selection.get('skipped_protected_product_ids', [])) if cleanup_selection.get('skipped_protected_product_ids') else 'None'}",
                ]
            )

    stage_plan = report.get("stage_plan")
    if isinstance(stage_plan, dict):
        current_stage = stage_plan.get("current_stage") or {}
        next_unblocked_stage = stage_plan.get("next_unblocked_stage") or {}
        lines.extend(
            [
                "",
                "## Stage Plan",
                "",
                f"- completed_stage_count: {stage_plan.get('completed_stage_count')}/{stage_plan.get('total_stage_count')}",
                f"- current_stage: Stage {current_stage.get('stage')} - {current_stage.get('name')} ({current_stage.get('status')})",
                f"- next_unblocked_stage: "
                f"{'Stage ' + str(next_unblocked_stage.get('stage')) + ' - ' + str(next_unblocked_stage.get('name')) if next_unblocked_stage else 'None'}",
                "",
                "## Stage Status",
                "",
            ]
        )
        for item in stage_plan.get("stages", []):
            lines.append(f"- Stage {item['stage']} {item['name']}: {item['status']}")

    workflow_doctor = report.get("workflow_doctor")
    if isinstance(workflow_doctor, dict):
        lines.extend(
            [
                "",
                "## Workflow Doctor",
                "",
                f"- checked_at: {workflow_doctor.get('checked_at')}",
                f"- validation_failed_count: {workflow_doctor.get('validation_failed_count')}",
            ]
        )
        for item in workflow_doctor.get("validation_runs", []):
            lines.append(f"- {item['script']}: exit={item['exit_code']}, report_status={item.get('report_status')}")

    lines.extend(["", "## Batches", ""])
    if not report["batches"]:
        lines.append("- None")
    for batch in report["batches"]:
        lines.append(
            f"- {batch['product_id']}: stage={batch['current_stage']}, "
            f"next_skill={batch['next_skill'] or 'None'}, "
            f"next_required_skill={batch['next_required_skill'] or 'None'}, "
            f"available={len(batch['available_artifacts'])}, "
            f"missing={len(batch['missing_required_artifacts'])}, "
            f"blocked={len(batch['blocked_reasons'])}"
        )

    lines.extend(
        [
            "",
            "## File Groups",
            "",
            f"- manifests: {report['file_groups']['manifests']['file_count']} files",
            f"- schemas: {report['file_groups']['schemas']['file_count']} files",
            f"- scripts: {report['file_groups']['scripts']['file_count']} files",
            f"- reports: {report['file_groups']['reports']['file_count']} files",
            f"- required_directories: {report['directory_tree_summary']['confirmed_count']}/{report['directory_tree_summary']['required_count']}",
            f"- required_files: {report['generated_or_confirmed_files']['confirmed_count']}/{report['generated_or_confirmed_files']['required_count']}",
        ]
    )

    lines.extend(["", "## Directory Tree Summary", ""])
    for item in report["directory_tree_summary"]["items"]:
        state = "confirmed" if item["exists"] else "missing"
        lines.append(f"- {state}: {item['path']}")

    lines.extend(["", "## Generated Or Confirmed Files", ""])
    for item in report["generated_or_confirmed_files"]["confirmed"]:
        lines.append(f"- {item}")
    if report["generated_or_confirmed_files"]["missing"]:
        lines.extend(["", "## Missing Generated Or Confirmed Files", ""])
        lines.extend(f"- {item}" for item in report["generated_or_confirmed_files"]["missing"])

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_report(root: Path) -> dict[str, Any]:
    validation_reports = {
        "workflow_architecture_report": report_summary(root, "workflow_architecture_report"),
        "skill_tree_report": report_summary(root, "skill_tree_report"),
        "reference_check_report": report_summary(root, "reference_check_report"),
        "production_readiness_report": report_summary(root, "production_readiness_report"),
    }
    product_ids = discover_product_ids(root)
    batches = [inspect_batch(root, product_id) for product_id in product_ids]

    missing_required_artifacts: list[str] = []
    blocked_reasons: list[str] = []
    for batch in batches:
        missing_required_artifacts.extend(f"{batch['product_id']}:{item}" for item in batch["missing_required_artifacts"])
        blocked_reasons.extend(f"{batch['product_id']}: {item}" for item in batch["blocked_reasons"])

    if not batches:
        missing_required_artifacts.extend(["active_product_batch_manifest", "product_source_inputs"])
        blocked_reasons.extend(
            [
                "No non-template product batch manifest found in manifests/.",
                "No product batch manifest points to repository inputs/artifacts or a manifest-declared external workspace.",
            ]
        )

    validation_failed = [name for name, item in validation_reports.items() if not item.get("exists") or item.get("status") != "pass"]
    if validation_failed:
        blocked_reasons.append(f"Validation reports are not all passing: {', '.join(validation_failed)}.")

    active_unblocked = next((batch for batch in batches if not batch["blocked_reasons"]), None)
    current_stage = active_unblocked["current_stage"] if active_unblocked else "repository_initialized_no_active_batch"
    next_skill = active_unblocked["next_skill"] if active_unblocked else None
    status = "ready" if active_unblocked and next_skill else "blocked" if blocked_reasons else "ready"
    directory_summary = required_directory_summary(root)
    file_summary = required_file_summary(root)

    report = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "project_root": str(root),
        "status": status,
        "current_stage": current_stage,
        "next_skill": next_skill,
        "missing_required_artifacts": sorted(set(missing_required_artifacts)),
        "blocked_reasons": blocked_reasons,
        "validation_reports": validation_reports,
        "skill_tree": skill_tree_summary(root),
        "references": reference_summary(root),
        "directory_tree_summary": directory_summary,
        "generated_or_confirmed_files": file_summary,
        "missing_files": collect_missing_files(root, file_summary),
        "extra_files": collect_extra_files(root),
        "codex_agents_compatibility": skill_compatibility_detail(root),
        "needs_manual_review": collect_manual_review_items(root),
        "file_groups": {
            "manifests": file_group_summary(root, "manifests", "*.json"),
            "schemas": file_group_summary(root, "schemas", "*.json"),
            "scripts": file_group_summary(root, "scripts", "*.py"),
            "reports": file_group_summary(root, "reports", "*"),
        },
        "batches": batches,
    }
    report["startup_hygiene"] = startup_hygiene_summary(root, product_ids)
    report["stage_plan"] = build_stage_plan(root, report)
    report["last_completed_stage"] = report["stage_plan"].get("last_completed_stage")
    report["next_stage"] = report["stage_plan"].get("next_stage")
    report["current_stage_judgment"] = report["stage_plan"].get("current_stage")
    report.update(action_policy(report["stage_plan"], validation_failed, batches))
    return report


def main() -> int:
    root = project_root()
    report = build_report(root)
    write_json(root / "reports" / "current_state.json", report)
    write_markdown(root / "reports" / "current_state.md", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
