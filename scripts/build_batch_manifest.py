from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

from category_recipes import (  # noqa: E402
    DEFAULT_CATEGORY_KEY,
    CategoryRecipeError,
    load_category_recipe,
)
from image_count_contract import (  # noqa: E402
    ImageCountContractError,
    detail_handheld_limit_message,
    handheld_count_maximum,
    image_count_spec,
    validate_image_count,
)

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
    return ROOT


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


def positive_integer(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive integer") from None
    if value <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return value


def strict_boolean(raw: str) -> bool:
    normalized = raw.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise argparse.ArgumentTypeError("must be true or false")


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
    manifest["artifacts"]["set_product_identity"] = absolute_text(paths["artifacts_root"] / "identity")
    manifest["artifacts"]["set_angle_layout_inventory"] = absolute_text(paths["artifacts_root"] / "angle_inventory")
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
    manifest["artifacts"]["set_product_identity"] = f"artifacts/{product_id}/identity/"
    manifest["artifacts"]["set_angle_layout_inventory"] = f"artifacts/{product_id}/angle_inventory/"
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
    parser.add_argument("--product-type", required=True, help="Confirmed non-empty product category.")
    parser.add_argument("--batch-type", choices=("single", "set"), default="single")
    parser.add_argument(
        "--category",
        help="Installed category recipe key. Existing calls default to 杯类.",
    )
    parser.add_argument("--length-cm", type=positive_integer)
    parser.add_argument("--width-cm", type=positive_integer)
    parser.add_argument("--height-cm", required=True, type=positive_integer)
    parser.add_argument(
        "--main-count",
        type=int,
        help="Main-image count. Omit to use the selected category default.",
    )
    parser.add_argument(
        "--detail-count",
        type=int,
        help="Detail-image count. Omit to use the selected category default.",
    )
    parser.add_argument("--handheld-main", required=True, type=int)
    parser.add_argument("--handheld-detail", required=True, type=int)
    parser.add_argument("--forbid-pouring-and-heating", required=True, type=strict_boolean)
    parser.add_argument("--missing-d-no-retake", required=True, type=strict_boolean)
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
    product_type = args.product_type.strip()
    if not product_type:
        print("product-type must not be empty")
        return 2
    category = (args.category or DEFAULT_CATEGORY_KEY).strip()
    try:
        recipe = load_category_recipe(root, category)
    except CategoryRecipeError as exc:
        print(f"产品品类配方不可用：{exc}")
        return 2
    try:
        main_count = validate_image_count(
            (
                image_count_spec(recipe.form, "main").default
                if args.main_count is None
                else args.main_count
            ),
            recipe.form,
            "main",
        )
        detail_count = validate_image_count(
            (
                image_count_spec(recipe.form, "detail").default
                if args.detail_count is None
                else args.detail_count
            ),
            recipe.form,
            "detail",
        )
    except ImageCountContractError as exc:
        print(str(exc))
        return 2
    dimensions = {
        "length_cm": args.length_cm,
        "width_cm": args.width_cm,
        "height_cm": args.height_cm,
    }
    if any(
        dimensions[key] is None
        for key in recipe.form["dimensions"]["required"]
    ):
        print("selected category is missing a required dimension")
        return 2
    for field in recipe.form["dimensions"]["fields"]:
        value = dimensions[field["key"]]
        if value is not None and not field["minimum"] <= value <= field["maximum"]:
            print(f"{field['key']} is outside the selected category range")
            return 2
    for mode, value, image_count in (
        ("main", args.handheld_main, main_count),
        ("detail", args.handheld_detail, detail_count),
    ):
        bounds = recipe.form["handheld"][mode]
        maximum = handheld_count_maximum(mode, image_count)
        if type(value) is not int or not bounds["minimum"] <= value <= maximum:
            if mode == "detail" and type(value) is int and value > maximum:
                print(detail_handheld_limit_message(image_count))
                return 2
            print(
                f"handheld-{mode} must be an integer from "
                f"{bounds['minimum']} through {maximum}"
            )
            return 2

    manifest = load_template(root)
    manifest["product_id"] = product_id
    manifest["batch_id"] = product_id
    manifest["batch_type"] = args.batch_type
    manifest["category"] = category
    manifest["user_declared_set_product"] = args.batch_type == "set"
    manifest["current_stage"] = "not_started"
    manifest["next_skill"] = "workflow-router"
    manifest["user_confirmed_facts"] = {
        "product_type": product_type,
        "length_cm": args.length_cm,
        "width_cm": args.width_cm,
        "height_cm": args.height_cm,
        "main_image_count": main_count,
        "detail_image_count": detail_count,
        "handheld_main": args.handheld_main,
        "handheld_detail": args.handheld_detail,
        "forbid_pouring_and_heating": args.forbid_pouring_and_heating,
        "missing_d_no_retake": args.missing_d_no_retake,
    }

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
