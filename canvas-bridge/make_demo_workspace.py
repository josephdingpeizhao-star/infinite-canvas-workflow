"""Scaffold a throwaway external demo workspace for canvas phase-1 testing.

Writes ONLY inside the given --root (default D:/dev/canvas-demo-workspace).
Never touches the repository: the demo batch manifest lives inside the
workspace itself, so detect_current_state / workflow_doctor will not discover
it as an active batch.

Steps mirror the real pipeline artifacts so route_batch() sees authentic
typed artifacts:

    python canvas-bridge/make_demo_workspace.py --init
    python canvas-bridge/make_demo_workspace.py --add-inputs
    python canvas-bridge/make_demo_workspace.py --advance identity
    ... style_master | angle_inventory | main_vc | detail_vc | final_prompts
        | integrity | renders | qc
    python canvas-bridge/make_demo_workspace.py --reset
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DEFAULT_ROOT = Path("D:/dev/canvas-demo-workspace")
PRODUCT_ID = "demo_live"
MARKER = ".canvas_demo"

INPUT_DIRS = ["white_bg", "style_refs", "set_group", "component_white_bg"]
ARTIFACT_DIRS = ["identity", "style_master", "angle_inventory", "variable_configs", "final_prompts", "comfyui_jobs", "qc_reports"]
OUTPUT_DIRS = ["renders", "repaired"]

STEPS = ["identity", "style_master", "angle_inventory", "main_vc", "detail_vc", "final_prompts", "integrity", "renders", "qc"]


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_manifest(root: Path) -> dict:
    def p(*parts: str) -> str:
        return str(root.joinpath(*parts))

    return {
        "batch_id": PRODUCT_ID,
        "product_id": PRODUCT_ID,
        "batch_type": "single",
        "user_declared_set_product": False,
        "requested_outputs": ["main", "detail", "final_prompts", "qc_reports"],
        "current_stage": "not_started",
        "next_skill": "workflow-router",
        "workspace": {
            "mode": "external",
            "root": str(root),
            "layout": "external_run_folder_v1",
            "manifests_root": p("manifests"),
            "inputs_root": p("inputs"),
            "drafts_root": p("drafts"),
            "artifacts_root": p("artifacts"),
            "outputs_root": p("outputs"),
        },
        "inputs": {
            "white_bg_images": [p("inputs", "white_bg")],
            "style_reference_images": [p("inputs", "style_refs")],
            "set_group_images": [p("inputs", "set_group")],
            "component_white_bg_images": [p("inputs", "component_white_bg")],
        },
        "drafts": {
            "product_identity_draft": p("drafts", "product_identity_draft.md"),
            "style_master_draft": p("drafts", "style_master_draft.md"),
        },
        "artifacts": {
            "asset_manifest": p("manifests", "asset_manifest.json"),
            "product_identity_archive": p("artifacts", "identity"),
            "style_master": p("artifacts", "style_master"),
            "angle_inventory": p("artifacts", "angle_inventory"),
            "main_variable_configs": [p("artifacts", "variable_configs")],
            "detail_variable_configs": [p("artifacts", "variable_configs")],
            "set_product_identity": "",
            "set_angle_layout_inventory": "",
            "final_prompts": [p("artifacts", "final_prompts")],
            "comfyui_jobs": [p("artifacts", "comfyui_jobs")],
            "qc_reports": [p("artifacts", "qc_reports")],
        },
        "outputs": {
            "renders": [p("outputs", "renders")],
            "repaired": [p("outputs", "repaired")],
        },
        "missing_required_artifacts": [],
        "blocked_reasons": [],
        "notes": "",
    }


def cmd_init(root: Path) -> None:
    for name in INPUT_DIRS:
        (root / "inputs" / name).mkdir(parents=True, exist_ok=True)
    (root / "drafts").mkdir(parents=True, exist_ok=True)
    for name in ARTIFACT_DIRS:
        (root / "artifacts" / name).mkdir(parents=True, exist_ok=True)
    for name in OUTPUT_DIRS:
        (root / "outputs" / name).mkdir(parents=True, exist_ok=True)
    (root / "manifests").mkdir(parents=True, exist_ok=True)
    (root / MARKER).write_text("canvas demo workspace; safe to delete\n", encoding="utf-8")
    manifest_path = root / "manifests" / "batch_manifest.json"
    write_json(manifest_path, build_manifest(root))
    print(json.dumps({"initialized": str(root), "manifest": str(manifest_path)}, ensure_ascii=False))


def ensure_demo_root(root: Path) -> None:
    if not (root / MARKER).is_file():
        raise SystemExit(f"refusing to touch {root}: missing {MARKER} marker (run --init first, or wrong --root)")


def cmd_add_inputs(root: Path) -> None:
    ensure_demo_root(root)
    files = [
        root / "inputs" / "white_bg" / "kettle_front.png",
        root / "inputs" / "white_bg" / "kettle_side.png",
        root / "inputs" / "style_refs" / "style_ref_1.png",
    ]
    for item in files:
        item.write_bytes(b"demo-image-stub")
    print(json.dumps({"added_inputs": [str(item) for item in files]}, ensure_ascii=False))


def cmd_advance(root: Path, step: str) -> None:
    ensure_demo_root(root)
    art = root / "artifacts"
    if step == "identity":
        write_json(art / "identity" / "product_identity_archive.json", {"artifact_type": "product_identity_archive", "product_id": PRODUCT_ID, "demo": True})
    elif step == "style_master":
        write_json(art / "style_master" / "style_master.json", {"artifact_type": "style_master", "product_id": PRODUCT_ID, "demo": True})
    elif step == "angle_inventory":
        write_json(art / "angle_inventory" / "angle_inventory.json", {"artifact_type": "angle_inventory", "product_id": PRODUCT_ID, "demo": True})
    elif step == "main_vc":
        write_json(art / "variable_configs" / "main_variable_config.json", {"artifact_type": "main_variable_config", "product_id": PRODUCT_ID, "demo": True})
    elif step == "detail_vc":
        write_json(art / "variable_configs" / "detail_variable_config.json", {"artifact_type": "detail_variable_config", "product_id": PRODUCT_ID, "demo": True})
    elif step == "final_prompts":
        write_json(art / "final_prompts" / "final_prompt_main_01.json", {"artifact_type": "final_prompt", "product_id": PRODUCT_ID, "demo": True})
        write_json(art / "comfyui_jobs" / "comfyui_jobs.json", {"artifact_type": "comfyui_job", "product_id": PRODUCT_ID, "demo": True})
    elif step == "integrity":
        write_json(
            art / "qc_reports" / "final_prompt_integrity_report.json",
            {"artifact_type": "final_prompt_integrity_report", "product_id": PRODUCT_ID, "status": "pass", "render_blocked": False, "demo": True},
        )
    elif step == "renders":
        (root / "outputs" / "renders" / "main_01.png").write_bytes(b"demo-render-stub")
        (root / "outputs" / "renders" / "main_02.png").write_bytes(b"demo-render-stub")
    elif step == "qc":
        write_json(art / "qc_reports" / "qc_report.json", {"artifact_type": "qc_report", "product_id": PRODUCT_ID, "status": "pass", "demo": True})
    else:
        raise SystemExit(f"unknown step: {step}; choose from {STEPS}")
    print(json.dumps({"advanced": step}, ensure_ascii=False))


def cmd_reset(root: Path) -> None:
    ensure_demo_root(root)
    removed = 0
    for base in ["inputs", "artifacts", "outputs"]:
        for item in (root / base).rglob("*"):
            if item.is_file():
                item.unlink()
                removed += 1
    print(json.dumps({"reset": str(root), "removed_files": removed}, ensure_ascii=False))


def main() -> int:
    parser = argparse.ArgumentParser(description="Scaffold and mutate a demo external workspace for canvas testing.")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--init", action="store_true")
    parser.add_argument("--add-inputs", action="store_true")
    parser.add_argument("--advance", choices=STEPS)
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args()

    ran = False
    if args.init:
        cmd_init(args.root)
        ran = True
    if args.add_inputs:
        cmd_add_inputs(args.root)
        ran = True
    if args.advance:
        cmd_advance(args.root, args.advance)
        ran = True
    if args.reset:
        cmd_reset(args.root)
        ran = True
    if not ran:
        parser.print_help()
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
