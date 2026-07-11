from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOTS = [ROOT / ".agents" / "skills", ROOT / ".codex" / "skills"]


SLICE_DEFINITIONS: dict[str, dict[str, Any]] = {
    "workflow_architecture_boundary": {
        "source_file": "工作流总控规则.txt",
        "line_start": 1,
        "line_end": 12,
        "tags": ["workflow", "codex_boundary", "comfyui_boundary"],
    },
    "workflow_final_input_priority": {
        "source_file": "工作流总控规则.txt",
        "line_start": 62,
        "line_end": 90,
        "tags": ["product_lock", "angle_lock", "color_lock", "final_inputs"],
    },
    "workflow_variable_config_stage": {
        "source_file": "工作流总控规则.txt",
        "line_start": 176,
        "line_end": 200,
        "tags": ["variable_config", "stage_boundary"],
    },
    "workflow_final_prompt_stage": {
        "source_file": "工作流总控规则.txt",
        "line_start": 202,
        "line_end": 225,
        "tags": ["final_prompt", "render_entry", "stage_boundary"],
    },
    "workflow_qc_return_stage": {
        "source_file": "工作流总控规则.txt",
        "line_start": 233,
        "line_end": 251,
        "tags": ["qc", "repair_routing"],
    },
    "workflow_variable_vs_final": {
        "source_file": "工作流总控规则.txt",
        "line_start": 382,
        "line_end": 394,
        "tags": ["variable_config", "final_prompt", "boundary"],
    },
    "main_source_batch_and_color": {
        "source_file": "主图单张变量配置提示词生成.txt",
        "line_start": 1,
        "line_end": 41,
        "tags": ["main", "source_files", "batch_type", "color_lock"],
    },
    "main_angle_and_count": {
        "source_file": "主图单张变量配置提示词生成.txt",
        "line_start": 69,
        "line_end": 151,
        "tags": ["main", "angle_lock", "config_count"],
    },
    "main_prop_and_text_core": {
        "source_file": "主图单张变量配置提示词生成.txt",
        "line_start": 182,
        "line_end": 210,
        "tags": ["main", "prop_rules", "text_rules"],
    },
    "main_required_fields_core": {
        "source_file": "主图单张变量配置提示词生成.txt",
        "line_start": 212,
        "line_end": 340,
        "tags": ["main", "required_fields", "product_lock", "color_lock"],
    },
    "main_fixed_output_header_core": {
        "source_file": "主图单张变量配置提示词生成.txt",
        "line_start": 405,
        "line_end": 461,
        "tags": ["main", "final_prompt", "render_entry"],
    },
    "main_handheld_enable_rule": {
        "source_file": "主图单张变量配置提示词生成.txt",
        "line_start": 498,
        "line_end": 553,
        "tags": ["main", "handheld", "explicit_user_count", "config_count"],
    },
    "detail_source_batch_and_color": {
        "source_file": "详情图单张变量配置提示词生成.txt",
        "line_start": 1,
        "line_end": 41,
        "tags": ["detail", "source_files", "batch_type", "color_lock"],
    },
    "detail_module_plan": {
        "source_file": "详情图单张变量配置提示词生成.txt",
        "line_start": 67,
        "line_end": 85,
        "tags": ["detail", "module_plan", "config_count"],
    },
    "detail_angle_and_count": {
        "source_file": "详情图单张变量配置提示词生成.txt",
        "line_start": 87,
        "line_end": 171,
        "tags": ["detail", "angle_lock", "config_count"],
    },
    "detail_required_fields_core": {
        "source_file": "详情图单张变量配置提示词生成.txt",
        "line_start": 274,
        "line_end": 472,
        "tags": ["detail", "required_fields", "text_rules", "prop_rules"],
    },
    "detail_fixed_output_header_core": {
        "source_file": "详情图单张变量配置提示词生成.txt",
        "line_start": 509,
        "line_end": 573,
        "tags": ["detail", "final_prompt", "render_entry"],
    },
    "detail_handheld_enable_rule": {
        "source_file": "详情图单张变量配置提示词生成.txt",
        "line_start": 617,
        "line_end": 673,
        "tags": ["detail", "handheld", "explicit_user_count", "config_count"],
    },
    "realism_runtime_summary_source": {
        "source_file": "真实感约束.txt",
        "line_start": 244,
        "line_end": 286,
        "tags": ["realism", "source_summary"],
    },
    "prop_runtime_summary_source": {
        "source_file": "道具生成规则模块.txt",
        "line_start": 260,
        "line_end": 320,
        "tags": ["props", "source_summary"],
    },
    "qc_runtime_summary_source": {
        "source_file": "电商图片通用质检清单.txt",
        "line_start": 286,
        "line_end": 329,
        "tags": ["qc", "source_summary"],
    },
    "platform_detail_rules_full": {
        "source_file": "淘宝天猫详情页链路与平台规范模块.txt",
        "line_start": 1,
        "line_end": 76,
        "tags": ["platform", "detail"],
    },
    "product_info_supplement_rules_full": {
        "source_file": "商品信息补充清单提示词.txt",
        "line_start": 1,
        "line_end": 21,
        "tags": ["product_info", "detail"],
    },
}


SKILL_PACKAGES: dict[str, list[str]] = {
    "main-variable-config": [
        "workflow_architecture_boundary",
        "workflow_variable_config_stage",
        "workflow_variable_vs_final",
        "main_source_batch_and_color",
        "main_angle_and_count",
        "main_prop_and_text_core",
        "main_required_fields_core",
        "main_handheld_enable_rule",
        "realism_runtime_summary_source",
        "prop_runtime_summary_source",
        "qc_runtime_summary_source",
    ],
    "detail-variable-config": [
        "workflow_architecture_boundary",
        "workflow_variable_config_stage",
        "workflow_variable_vs_final",
        "detail_source_batch_and_color",
        "detail_module_plan",
        "detail_angle_and_count",
        "detail_required_fields_core",
        "detail_handheld_enable_rule",
        "realism_runtime_summary_source",
        "prop_runtime_summary_source",
        "platform_detail_rules_full",
        "product_info_supplement_rules_full",
        "qc_runtime_summary_source",
    ],
    "final-prompt-compiler": [
        "workflow_architecture_boundary",
        "workflow_final_input_priority",
        "workflow_final_prompt_stage",
        "workflow_variable_vs_final",
        "main_fixed_output_header_core",
        "detail_fixed_output_header_core",
        "realism_runtime_summary_source",
        "prop_runtime_summary_source",
        "platform_detail_rules_full",
        "qc_runtime_summary_source",
    ],
    "qc-inspector": [
        "workflow_architecture_boundary",
        "workflow_qc_return_stage",
        "workflow_variable_vs_final",
        "realism_runtime_summary_source",
        "qc_runtime_summary_source",
    ],
}


def read_source(source_file: str) -> tuple[str, str, list[str]]:
    path = ROOT / source_file
    text = path.read_text(encoding="utf-8")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return str(path), digest, text.splitlines()


def build_slice(slice_id: str) -> dict[str, Any]:
    definition = SLICE_DEFINITIONS[slice_id]
    source_path, source_sha256, lines = read_source(definition["source_file"])
    start = int(definition["line_start"])
    end = int(definition["line_end"])
    if start < 1 or end > len(lines) or start > end:
        raise ValueError(f"invalid slice {slice_id}: {definition['source_file']}:{start}-{end}")
    return {
        "slice_id": slice_id,
        "source_file": definition["source_file"],
        "source_path": source_path,
        "source_sha256": source_sha256,
        "line_start": start,
        "line_end": end,
        "tags": definition["tags"],
        "text": "\n".join(lines[start - 1 : end]),
    }


def runtime_slice(item: dict[str, Any]) -> dict[str, Any]:
    """Return the slim runtime form; full path/hash metadata stays in the audit index."""
    return {
        "slice_id": item["slice_id"],
        "source_file": item["source_file"],
        "line_start": item["line_start"],
        "line_end": item["line_end"],
        "tags": item["tags"],
        "audit_ref": f"reports/runtime_rule_index.json#/slices/{item['slice_id']}",
        "text": item["text"],
    }


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_markdown(path: Path, package: dict[str, Any]) -> None:
    lines = [
        f"# {package['skill']} Runtime Rule Slices",
        "",
        "This package contains exact source slices plus minimal runtime metadata. Full source paths and hashes stay in `reports/runtime_rule_index.json` for audit.",
        "",
    ]
    for item in package["slices"]:
        lines.extend(
            [
                f"## {item['slice_id']}",
                "",
                f"- source: `{item['source_file']}:{item['line_start']}-{item['line_end']}`",
                f"- audit_ref: `{item['audit_ref']}`",
                "",
                "```text",
                item["text"],
                "```",
                "",
            ]
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    all_slices = {slice_id: build_slice(slice_id) for slice_id in SLICE_DEFINITIONS}
    index = {
        "artifact_type": "runtime_rule_index",
        "runtime_package_version": "2-slim",
        "generated_at": generated_at,
        "rewrite_allowed": False,
        "contains_only_source_slices": True,
        "source_rules_unchanged": True,
        "audit_metadata_scope": "full_source_paths_and_hashes",
        "slices": list(all_slices.values()),
        "skill_packages": SKILL_PACKAGES,
    }
    write_json(ROOT / "reports" / "runtime_rule_index.json", index)

    report_md = [
        "# Runtime Rule Index",
        "",
        "This index is generated from exact line slices of the original rule files. It does not rewrite, summarize, or replace the source rules.",
        "",
    ]
    for skill, slice_ids in SKILL_PACKAGES.items():
        report_md.append(f"## {skill}")
        for slice_id in slice_ids:
            item = all_slices[slice_id]
            report_md.append(f"- `{slice_id}` -> `{item['source_file']}:{item['line_start']}-{item['line_end']}`")
        report_md.append("")
    (ROOT / "reports" / "runtime_rule_index.md").write_text("\n".join(report_md), encoding="utf-8")

    for skill, slice_ids in SKILL_PACKAGES.items():
        package = {
            "artifact_type": "runtime_rule_slice_package",
            "runtime_package_version": "2-slim",
            "skill": skill,
            "generated_at": generated_at,
            "rewrite_allowed": False,
            "contains_only_source_slices": True,
            "source_rules_unchanged": True,
            "audit_index_path": "reports/runtime_rule_index.json",
            "audit_metadata_omitted_from_runtime_package": [
                "source_path",
                "source_sha256",
            ],
            "slices": [runtime_slice(all_slices[slice_id]) for slice_id in slice_ids],
        }
        for root in SKILL_ROOTS:
            out_dir = root / skill / "references" / "runtime_rule_slices"
            write_json(out_dir / f"{skill}.runtime_rule_slices.json", package)
            write_markdown(out_dir / f"{skill}.runtime_rule_slices.md", package)

    print(json.dumps({"status": "created", "slice_count": len(all_slices), "skill_package_count": len(SKILL_PACKAGES)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
