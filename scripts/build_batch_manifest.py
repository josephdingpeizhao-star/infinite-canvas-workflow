from __future__ import annotations

import argparse
import json
from pathlib import Path


ARTIFACT_DIRS = [
    "identity",
    "style_master",
    "angle_inventory",
    "variable_configs",
    "final_prompts",
    "comfyui_jobs",
    "qc_reports",
]

INPUT_DIRS = [
    "white_bg",
    "style_refs",
    "set_group",
    "component_white_bg",
]

DRAFT_FILES = {
    "product_identity_draft": "product_identity_draft.md",
    "style_master_draft": "style_master_draft.md",
}

OUTPUT_DIRS = [
    "renders",
    "repaired",
]


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def load_template(root: Path) -> dict:
    template_path = root / "manifests" / "batch_manifest.template.json"
    with template_path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_asset_template(root: Path) -> dict:
    template_path = root / "manifests" / "asset_manifest.template.json"
    with template_path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def absolute_text(path: Path) -> str:
    return str(path.resolve())


def ensure_dirs(paths: list[Path], *, dry_run: bool) -> None:
    if dry_run:
        return
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)


def apply_external_workspace(manifest: dict, root: Path, product_id: str, workspace_root: Path) -> tuple[list[Path], dict[str, Path]]:
    workspace_root = workspace_root.resolve()
    paths = {
        "workspace_root": workspace_root,
        "manifests_root": workspace_root / "manifests",
        "inputs_root": workspace_root / "inputs",
        "drafts_root": workspace_root / "drafts",
        "artifacts_root": workspace_root / "artifacts",
        "outputs_root": workspace_root / "outputs",
    }

    manifest["workspace"] = {
        "mode": "external",
        "root": absolute_text(paths["workspace_root"]),
        "layout": "external_run_folder_v1",
        "manifests_root": absolute_text(paths["manifests_root"]),
        "inputs_root": absolute_text(paths["inputs_root"]),
        "drafts_root": absolute_text(paths["drafts_root"]),
        "artifacts_root": absolute_text(paths["artifacts_root"]),
        "outputs_root": absolute_text(paths["outputs_root"]),
    }
    manifest["inputs"]["white_bg_images"] = [absolute_text(paths["inputs_root"] / "white_bg")]
    manifest["inputs"]["style_reference_images"] = [absolute_text(paths["inputs_root"] / "style_refs")]
    manifest["inputs"]["set_group_images"] = [absolute_text(paths["inputs_root"] / "set_group")]
    manifest["inputs"]["component_white_bg_images"] = [absolute_text(paths["inputs_root"] / "component_white_bg")]
    manifest["drafts"]["product_identity_draft"] = absolute_text(paths["drafts_root"] / DRAFT_FILES["product_identity_draft"])
    manifest["drafts"]["style_master_draft"] = absolute_text(paths["drafts_root"] / DRAFT_FILES["style_master_draft"])
    manifest["artifacts"]["asset_manifest"] = absolute_text(paths["manifests_root"] / "asset_manifest.json")
    manifest["artifacts"]["product_identity_archive"] = absolute_text(paths["artifacts_root"] / "identity")
    manifest["artifacts"]["style_master"] = absolute_text(paths["artifacts_root"] / "style_master")
    manifest["artifacts"]["angle_inventory"] = absolute_text(paths["artifacts_root"] / "angle_inventory")
    manifest["artifacts"]["main_variable_configs"] = [absolute_text(paths["artifacts_root"] / "variable_configs")]
    manifest["artifacts"]["detail_variable_configs"] = [absolute_text(paths["artifacts_root"] / "variable_configs")]
    manifest["artifacts"]["set_product_identity"] = ""
    manifest["artifacts"]["set_angle_layout_inventory"] = ""
    manifest["artifacts"]["final_prompts"] = [absolute_text(paths["artifacts_root"] / "final_prompts")]
    manifest["artifacts"]["comfyui_jobs"] = [absolute_text(paths["artifacts_root"] / "comfyui_jobs")]
    manifest["artifacts"]["qc_reports"] = [absolute_text(paths["artifacts_root"] / "qc_reports")]
    manifest["outputs"]["renders"] = [absolute_text(paths["outputs_root"] / "renders")]
    manifest["outputs"]["repaired"] = [absolute_text(paths["outputs_root"] / "repaired")]

    directories = [
        paths["manifests_root"],
        paths["drafts_root"],
        *(paths["inputs_root"] / directory for directory in INPUT_DIRS),
        *(paths["artifacts_root"] / directory for directory in ARTIFACT_DIRS),
        *(paths["outputs_root"] / directory for directory in OUTPUT_DIRS),
    ]
    return directories, paths


def apply_repository_workspace(manifest: dict, root: Path, product_id: str) -> tuple[list[Path], dict[str, Path]]:
    paths = {
        "workspace_root": root,
        "manifests_root": root / "manifests",
        "inputs_root": root / "inputs" / "products" / product_id,
        "drafts_root": root / "artifacts" / product_id / "drafts",
        "artifacts_root": root / "artifacts" / product_id,
        "outputs_root": root / "artifacts" / product_id / "outputs",
    }

    manifest["workspace"] = {
        "mode": "repository",
        "root": absolute_text(paths["workspace_root"]),
        "layout": "repository_product_v1",
        "manifests_root": absolute_text(paths["manifests_root"]),
        "inputs_root": absolute_text(paths["inputs_root"]),
        "drafts_root": absolute_text(paths["drafts_root"]),
        "artifacts_root": absolute_text(paths["artifacts_root"]),
        "outputs_root": absolute_text(paths["outputs_root"]),
    }
    manifest["inputs"]["white_bg_images"] = [f"inputs/products/{product_id}/white_bg/"]
    manifest["inputs"]["style_reference_images"] = [f"inputs/products/{product_id}/style_refs/"]
    manifest["inputs"]["set_group_images"] = [f"inputs/products/{product_id}/set_group/"]
    manifest["inputs"]["component_white_bg_images"] = [f"inputs/products/{product_id}/component_white_bg/"]
    manifest["drafts"]["product_identity_draft"] = f"artifacts/{product_id}/drafts/{DRAFT_FILES['product_identity_draft']}"
    manifest["drafts"]["style_master_draft"] = f"artifacts/{product_id}/drafts/{DRAFT_FILES['style_master_draft']}"
    manifest["artifacts"]["asset_manifest"] = f"manifests/{product_id}.asset_manifest.json"
    manifest["artifacts"]["product_identity_archive"] = f"artifacts/{product_id}/identity/"
    manifest["artifacts"]["style_master"] = f"artifacts/{product_id}/style_master/"
    manifest["artifacts"]["angle_inventory"] = f"artifacts/{product_id}/angle_inventory/"
    manifest["artifacts"]["main_variable_configs"] = [f"artifacts/{product_id}/variable_configs/"]
    manifest["artifacts"]["detail_variable_configs"] = [f"artifacts/{product_id}/variable_configs/"]
    manifest["artifacts"]["set_product_identity"] = ""
    manifest["artifacts"]["set_angle_layout_inventory"] = ""
    manifest["artifacts"]["final_prompts"] = [f"artifacts/{product_id}/final_prompts/"]
    manifest["artifacts"]["comfyui_jobs"] = [f"artifacts/{product_id}/comfyui_jobs/"]
    manifest["artifacts"]["qc_reports"] = [f"artifacts/{product_id}/qc_reports/"]
    manifest["outputs"]["renders"] = [f"artifacts/{product_id}/outputs/renders/"]
    manifest["outputs"]["repaired"] = [f"artifacts/{product_id}/outputs/repaired/"]

    directories = [
        *(paths["inputs_root"] / directory for directory in INPUT_DIRS),
        paths["drafts_root"],
        *(paths["artifacts_root"] / directory for directory in ARTIFACT_DIRS),
        *(paths["outputs_root"] / directory for directory in OUTPUT_DIRS),
    ]
    return directories, paths


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a product batch manifest and workspace folders.")
    parser.add_argument("--product-id", required=True, help="Product id used for the manifest name and batch id.")
    parser.add_argument(
        "--workspace-root",
        help="Optional external run folder. When provided, all batch inputs and outputs are scaffolded there.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print the planned manifest and directories without writing files.")
    args = parser.parse_args()

    root = project_root()
    product_id = args.product_id.strip()
    if not product_id:
        print("product-id must not be empty")
        return 2

    manifest = load_template(root)
    manifest["product_id"] = product_id
    manifest["batch_id"] = product_id
    manifest["batch_type"] = "single"
    manifest["user_declared_set_product"] = False
    manifest["current_stage"] = "not_started"
    manifest["next_skill"] = "workflow-router"

    if args.workspace_root:
        directories, workspace_paths = apply_external_workspace(manifest, root, product_id, Path(args.workspace_root))
        workspace_manifest_path = workspace_paths["manifests_root"] / "batch_manifest.json"
        asset_manifest_path = workspace_paths["manifests_root"] / "asset_manifest.json"
    else:
        directories, workspace_paths = apply_repository_workspace(manifest, root, product_id)
        workspace_manifest_path = root / "manifests" / f"{product_id}.batch_manifest.json"
        asset_manifest_path = root / "manifests" / f"{product_id}.asset_manifest.json"

    manifest_path = root / "manifests" / f"{product_id}.batch_manifest.json"
    if manifest_path.exists():
        print(f"manifest already exists, not overwriting: {manifest_path}")
        return 1
    if args.workspace_root and workspace_manifest_path.exists():
        print(f"workspace manifest already exists, not overwriting: {workspace_manifest_path}")
        return 1
    if asset_manifest_path.exists():
        print(f"asset manifest already exists, not overwriting: {asset_manifest_path}")
        return 1

    asset_manifest = load_asset_template(root)
    if not args.dry_run:
        ensure_dirs(directories, dry_run=False)
        write_json(manifest_path, manifest)
        if args.workspace_root:
            write_json(workspace_manifest_path, manifest)
        write_json(asset_manifest_path, asset_manifest)

    print(
        json.dumps(
            {
                "status": "planned" if args.dry_run else "created",
                "manifest": str(manifest_path),
                "workspace_manifest": str(workspace_manifest_path),
                "asset_manifest": str(asset_manifest_path),
                "workspace_root": str(workspace_paths["workspace_root"]),
                "directories": [str(path) for path in directories],
                "manifest_data": manifest if args.dry_run else None,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
