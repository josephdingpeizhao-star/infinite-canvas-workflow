from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def stable_json_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class FinalPromptBundle:
    root: Path
    manifest_path: Path
    manifest: dict[str, Any]
    final_dir: Path
    index_path: Path
    main_config_path: Path
    detail_config_path: Path
    qc_dir: Path
    renders_dir: Path
    outputs_root: Path
    white_bg_dir: Path

    def prompt_path(self, config_id: str) -> Path:
        return self.final_dir / f"{config_id}_final_prompt.json"


def build_final_prompt_bundle(root: Path) -> FinalPromptBundle:
    product_id = "fixture_product"
    workspace = root / "workspace"
    manifests_dir = workspace / "manifests"
    inputs_root = workspace / "inputs"
    white_bg_dir = inputs_root / "white_bg"
    artifacts_root = workspace / "artifacts"
    identity_dir = artifacts_root / "identity"
    style_dir = artifacts_root / "style_master"
    angle_dir = artifacts_root / "angle_inventory"
    variable_dir = artifacts_root / "variable_configs"
    final_dir = artifacts_root / "final_prompts"
    comfy_dir = artifacts_root / "comfyui_jobs"
    qc_dir = artifacts_root / "qc_reports"
    outputs_root = workspace / "outputs"
    renders_dir = outputs_root / "renders"
    repaired_dir = outputs_root / "repaired"

    for directory in (
        manifests_dir,
        white_bg_dir,
        identity_dir,
        style_dir,
        angle_dir,
        variable_dir,
        final_dir,
        comfy_dir,
        qc_dir,
        renders_dir,
        repaired_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    reference_names = ("front.JPG", "side.png", "top.webp")
    for index, name in enumerate(reference_names, start=1):
        (white_bg_dir / name).write_bytes(f"reference-{index}".encode("ascii"))

    identity_path = identity_dir / "product_identity_archive.json"
    identity = {
        "product_id": product_id,
        "artifact_type": "product_identity_archive",
        "identity": {
            "product_name": "测试水壶",
            "negative_prompt_constraints": "不得改变商品结构与颜色",
        },
    }
    style_path = style_dir / "style_master.json"
    style = {"product_id": product_id, "artifact_type": "style_master", "style": "柔和自然光"}
    angle_path = angle_dir / "angle_inventory.json"
    angle = {
        "product_id": product_id,
        "artifact_type": "angle_inventory",
        "image_assets": [
            {"asset_id": "img_001", "file_path": reference_names[0]},
            {"asset_id": "img_002", "file_path": reference_names[1]},
            {"asset_id": "img_003", "file_path": reference_names[2]},
        ],
    }
    write_json(identity_path, identity)
    write_json(style_path, style)
    write_json(angle_path, angle)

    def config_document(mode: str, count: int) -> dict[str, Any]:
        common = {
            "产品身份": "测试水壶",
            "已确认高度": "约 25 厘米",
            "禁止事项": "不得改变商品结构与颜色",
        }
        configs = []
        for number in range(1, count + 1):
            config_id = f"{mode}_{number:02d}"
            handheld = config_id in {"main_02", "main_05", "detail_02"}
            reference_index = (number - 1) % len(reference_names)
            overrides = {
                "手持交互声明": "本张图启用手持场景" if handheld else "本张图不启用手持场景",
                "绑定角度槽位": f"A 槽位，绑定源图 img_{reference_index + 1:03d}",
                "输出画布比例": "1:1" if mode == "main" else "3:4",
            }
            resolved = dict(common)
            resolved.update(overrides)
            configs.append(
                {
                    "config_id": config_id,
                    "output_type": mode,
                    "per_image_overrides": overrides,
                    "resolved_variable_config_sha256": stable_json_sha256(resolved),
                    "notes": "fixture",
                }
            )
        return {
            "product_id": product_id,
            "artifact_type": f"{mode}_variable_config",
            "config_count": count,
            "common_constraints": common,
            "configs": configs,
            "notes": "fixture",
        }

    main_config_path = variable_dir / "main_variable_configs.json"
    detail_config_path = variable_dir / "detail_variable_configs.json"
    main_doc = config_document("main", 6)
    detail_doc = config_document("detail", 8)
    write_json(main_config_path, main_doc)
    write_json(detail_config_path, detail_doc)

    index_items = []
    for mode, document, source_path in (
        ("main", main_doc, main_config_path),
        ("detail", detail_doc, detail_config_path),
    ):
        source_hash = file_sha256(source_path)
        for index, config in enumerate(document["configs"]):
            config_id = config["config_id"]
            handheld = config_id in {"main_02", "main_05", "detail_02"}
            ratio = "1:1" if mode == "main" else "3:4"
            final_prompt = (
                f"测试电商图。画布比例固定为 {ratio}。产品高度约 25 厘米。"
                + ("手持状态：启用手持场景。" if handheld else "本张图不启用手持场景。")
            )
            prompt_path = final_dir / f"{config_id}_final_prompt.json"
            final_doc = {
                "product_id": product_id,
                "artifact_type": "final_prompt",
                "upstream_artifacts": {
                    "product_identity_archive": str(identity_path),
                    "style_master": str(style_path),
                    "angle_inventory": str(angle_path),
                    "variable_config": str(source_path),
                },
                "variable_config": {
                    "config_id": config_id,
                    "output_type": mode,
                    "source_path": str(source_path),
                    "source_sha256": source_hash,
                    "source_schema": "common_constraints + per_image_overrides",
                    "common_constraints_ref": {
                        "path": str(source_path),
                        "json_pointer": "/common_constraints",
                    },
                    "per_image_overrides_ref": {
                        "path": str(source_path),
                        "json_pointer": f"/configs/{index}/per_image_overrides",
                    },
                    "resolved_variable_config_sha256": config["resolved_variable_config_sha256"],
                },
                "uses_upstream_prompt_files_as_visual_requirements": False,
                "final_prompt": final_prompt,
                "negative_prompt": identity["identity"]["negative_prompt_constraints"],
                "notes": "fixture",
            }
            write_json(prompt_path, final_doc)
            bound_reference = reference_names[index % len(reference_names)]
            index_items.append(
                {
                    "config_id": config_id,
                    "output_type": mode,
                    "final_prompt_path": str(prompt_path),
                    "bound_reference": bound_reference,
                }
            )

    index_path = final_dir / "final_prompt_index.json"
    write_json(
        index_path,
        {
            "product_id": product_id,
            "artifact_type": "final_prompt_index",
            "prompt_count": len(index_items),
            "uses_upstream_prompt_files_as_visual_requirements": False,
            "items": index_items,
            "notes": "fixture",
        },
    )

    manifest = {
        "product_id": product_id,
        "batch_type": "single",
        "requested_outputs": ["main", "detail", "final_prompts", "qc_reports"],
        "workspace": {
            "mode": "external",
            "root": str(workspace),
            "manifests_root": str(manifests_dir),
            "inputs_root": str(inputs_root),
            "artifacts_root": str(artifacts_root),
            "outputs_root": str(outputs_root),
        },
        "inputs": {
            "white_bg_images": [str(white_bg_dir)],
            "style_reference_images": [],
            "set_group_images": [],
            "component_white_bg_images": [],
        },
        "artifacts": {
            "product_identity_archive": str(identity_dir),
            "style_master": str(style_dir),
            "angle_inventory": str(angle_dir),
            "main_variable_configs": [str(variable_dir)],
            "detail_variable_configs": [str(variable_dir)],
            "final_prompts": [str(final_dir)],
            "comfyui_jobs": [str(comfy_dir)],
            "qc_reports": [str(qc_dir)],
        },
        "outputs": {
            "renders": [str(renders_dir)],
            "repaired": [str(repaired_dir)],
        },
        "notes": (
            "用户确认产品类型: 水壶 | 用户确认高度厘米: 25 | "
            "主图手持数量: 2 | 详情图手持数量: 1"
        ),
    }
    manifest_path = manifests_dir / f"{product_id}.batch_manifest.json"
    write_json(manifest_path, manifest)
    return FinalPromptBundle(
        root=root,
        manifest_path=manifest_path,
        manifest=manifest,
        final_dir=final_dir,
        index_path=index_path,
        main_config_path=main_config_path,
        detail_config_path=detail_config_path,
        qc_dir=qc_dir,
        renders_dir=renders_dir,
        outputs_root=outputs_root,
        white_bg_dir=white_bg_dir,
    )
