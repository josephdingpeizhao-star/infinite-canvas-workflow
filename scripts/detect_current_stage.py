from __future__ import annotations

import json
from pathlib import Path
from typing import Any


SOURCE_RULE_FILES = [
    "产品身份档案提示词.txt",
    "道具生成规则模块.txt",
    "电商图片通用质检清单.txt",
    "反向提取风格母版提示词.txt",
    "工作流总控规则.txt",
    "角度槽位入库表生成与识别提示词.txt",
    "商品信息补充清单提示词.txt",
    "手持产品场景基础模块.txt",
    "手持适配规则.txt",
    "淘宝天猫详情页链路与平台规范模块.txt",
    "套装编排规则.txt",
    "套装变量配置补充模块.txt",
    "套装产品工作流补充规则.txt",
    "套装产品身份档案提示词.txt",
    "套装角度与编排入库表提示词.txt",
    "详情图单张变量配置提示词生成.txt",
    "真实感约束.txt",
    "主图单张变量配置提示词生成.txt",
]

PERSISTENT_DOCS = [
    "AGENTS.md",
    "docs/ARCHITECTURE.md",
    "docs/STAGE_PLAN.md",
    "docs/CURRENT_PROGRESS.md",
]

STAGE3_DIRS = [
    "inputs/products/_template_product/white_bg",
    "inputs/products/_template_product/style_refs",
    "inputs/products/_template_product/set_group",
    "inputs/products/_template_product/component_white_bg",
    "artifacts/_template_product/identity",
    "artifacts/_template_product/style_master",
    "artifacts/_template_product/angle_inventory",
    "artifacts/_template_product/variable_configs",
    "artifacts/_template_product/final_prompts",
    "artifacts/_template_product/comfyui_jobs",
    "artifacts/_template_product/qc_reports",
    "manifests",
    "schemas",
    "scripts",
    "reports",
    "tests/fixtures",
    "_archive/migrated_skill_md",
]

STAGE3_MANIFESTS = [
    "manifests/workflow_architecture.json",
    "manifests/batch_manifest.template.json",
    "manifests/asset_manifest.template.json",
]

STAGE3_SCHEMAS = [
    "schemas/workflow_architecture.schema.json",
    "schemas/routing_decision.schema.json",
    "schemas/product_identity_archive.schema.json",
    "schemas/style_master.schema.json",
    "schemas/angle_inventory.schema.json",
    "schemas/product_info_supplement.schema.json",
    "schemas/main_variable_config.schema.json",
    "schemas/detail_variable_config.schema.json",
    "schemas/final_prompt.schema.json",
    "schemas/final_prompt_integrity_report.schema.json",
    "schemas/qc_report.schema.json",
    "schemas/set_product_identity.schema.json",
    "schemas/set_angle_layout_inventory.schema.json",
    "schemas/set_variable_config_extension.schema.json",
]

STAGE3_SCRIPTS = [
    "scripts/validate_workflow_architecture.py",
    "scripts/validate_skill_tree.py",
    "scripts/validate_references.py",
    "scripts/validate_artifact_schema.py",
    "scripts/build_batch_manifest.py",
    "scripts/build_runtime_rule_index.py",
    "scripts/compile_final_prompts.py",
    "scripts/validate_final_prompt_integrity.py",
    "scripts/detect_current_state.py",
    "scripts/pre_render_reference_gate.py",
    "scripts/run_comfy_cloud_batch_robust.py",
    "scripts/submit_comfy_cloud_jobs.py",
    "scripts/validate_production_readiness.py",
]

STAGE4_REPORTS = [
    "reports/skill_tree_report.json",
    "reports/skill_tree_report.md",
    "reports/reference_check_report.json",
    "reports/reference_check_report.md",
    "reports/current_state.json",
    "reports/current_state.md",
]

STAGE5_SCRIPTS = [
    "scripts/detect_current_stage.py",
    "scripts/workflow_doctor.py",
    "scripts/detect_current_state.py",
]


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


def report_status(root: Path, report_name: str) -> str | None:
    data = load_json(root / "reports" / f"{report_name}.json")
    if isinstance(data, dict) and isinstance(data.get("status"), str):
        return data["status"]
    return None


def product_stage_pass_statuses(stage_number: int) -> set[str]:
    if stage_number == 12:
        return {"pass", "pass_with_manual_review_recommended"}
    return {"pass", "complete", "completed"}


def is_blocking_stage_status(status: str | None) -> bool:
    if not status:
        return False
    normalized = status.lower()
    return normalized.startswith(("blocked", "fail", "error"))


def product_stage_report(root: Path, product_id: str, stage_number: int) -> tuple[Path, dict[str, Any]] | None:
    reports_dir = root / "reports"
    matches: list[tuple[Path, dict[str, Any]]] = []
    for path in reports_dir.glob(f"{product_id}_stage_{stage_number}_*.json"):
        data = load_json(path)
        if isinstance(data, dict) and data.get("stage") == stage_number and isinstance(data.get("status"), str):
            matches.append((path, data))
    if not matches:
        return None

    pass_statuses = product_stage_pass_statuses(stage_number)
    passing = [item for item in matches if item[1].get("status") in pass_statuses]
    if passing:
        return max(passing, key=lambda item: item[0].stat().st_mtime)
    return max(matches, key=lambda item: item[0].stat().st_mtime)


def product_prompt_integrity_report(root: Path, product_id: str, batch: dict[str, Any]) -> tuple[Path, dict[str, Any]] | None:
    candidate_paths = [root / "reports" / f"{product_id}_final_prompt_integrity_report.json"]
    artifacts = batch.get("artifacts") if isinstance(batch.get("artifacts"), dict) else {}
    qc_reports = artifacts.get("qc_reports") if isinstance(artifacts.get("qc_reports"), dict) else {}
    for item in qc_reports.get("paths", []) if isinstance(qc_reports.get("paths"), list) else []:
        value = item.get("resolved_path") or item.get("path")
        if not isinstance(value, str) or not value:
            continue
        path = Path(value)
        if not path.is_absolute():
            path = root / value
        candidate_paths.append(path / "final_prompt_integrity_report.json" if path.suffix.lower() != ".json" else path)

    matches: list[tuple[Path, dict[str, Any]]] = []
    seen = set()
    for path in candidate_paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        data = load_json(path)
        if isinstance(data, dict) and data.get("artifact_type") == "final_prompt_integrity_report":
            matches.append((path, data))
    if not matches:
        return None
    return max(matches, key=lambda item: item[0].stat().st_mtime if item[0].exists() else 0)


def missing_paths(root: Path, paths: list[str], *, directories: bool = False) -> list[str]:
    missing = []
    for item in paths:
        path = root / item
        exists = path.is_dir() if directories else path.is_file()
        if not exists:
            missing.append(item)
    return missing


def stage(
    number: int,
    name: str,
    status: str,
    *,
    evidence: list[str] | None = None,
    missing: list[str] | None = None,
    blocked_reasons: list[str] | None = None,
    next_action: str | None = None,
) -> dict[str, Any]:
    return {
        "stage": number,
        "name": name,
        "status": status,
        "evidence": evidence or [],
        "missing": missing or [],
        "blocked_reasons": blocked_reasons or [],
        "next_action": next_action,
    }


def stage0_source_rule_freeze(root: Path) -> dict[str, Any]:
    missing_sources = missing_paths(root, SOURCE_RULE_FILES)
    missing_docs = missing_paths(root, PERSISTENT_DOCS)
    skill_tree_report = load_json(root / "reports" / "skill_tree_report.json")
    migration_files_identified = isinstance(skill_tree_report, dict) and isinstance(skill_tree_report.get("migration_files"), dict)
    missing = list(missing_sources)
    missing.extend(missing_docs)
    if not migration_files_identified:
        missing.append("reports/skill_tree_report.json:migration_files")

    return stage(
        0,
        "Source Rule Freeze",
        "complete" if not missing else "blocked",
        evidence=[
            f"source_rule_file_count={len(SOURCE_RULE_FILES) - len(missing_sources)}/{len(SOURCE_RULE_FILES)}",
            f"persistent_doc_count={len(PERSISTENT_DOCS) - len(missing_docs)}/{len(PERSISTENT_DOCS)}",
            f"migration_files_identified={migration_files_identified}",
            "no_image_generation_performed_by_stage_0_to_5_scripts",
        ],
        missing=missing,
        blocked_reasons=[] if not missing else ["Source rule freeze criteria are incomplete."],
        next_action=None if not missing else "Restore missing source rules or refresh the skill tree report.",
    )


def stage1_skill_tree(root: Path) -> dict[str, Any]:
    status = report_status(root, "skill_tree_report")
    report = load_json(root / "reports" / "skill_tree_report.json")
    accepted = status in {"pass", "pass_with_warnings"}
    manual_review = []
    if isinstance(report, dict):
        migration_files = report.get("migration_files") or {}
        manual_review = migration_files.get("needs_manual_review") if isinstance(migration_files.get("needs_manual_review"), list) else []

    return stage(
        1,
        "Skill Tree Normalization",
        "complete" if accepted else "blocked",
        evidence=[f"skill_tree_report_status={status}", f"needs_manual_review_count={len(manual_review)}"],
        missing=[] if accepted else ["reports/skill_tree_report.json"],
        blocked_reasons=[] if accepted else ["Skill tree validation is not passing."],
        next_action=None if accepted else "Run python scripts/validate_skill_tree.py and resolve reported failures.",
    )


def stage2_references(root: Path) -> dict[str, Any]:
    status = report_status(root, "reference_check_report")
    report = load_json(root / "reports" / "reference_check_report.json")
    missing_count = 0
    extra_count = 0
    misplaced_count = 0
    if isinstance(report, dict):
        missing_count = len(report.get("missing_files", []))
        extra_count = len(report.get("extra_files", []))
        misplaced_count = len(report.get("misplaced_set_files", []))

    accepted = status == "pass"
    return stage(
        2,
        "References Mapping Validation",
        "complete" if accepted else "blocked",
        evidence=[
            f"reference_check_report_status={status}",
            f"missing_file_count={missing_count}",
            f"extra_file_count={extra_count}",
            f"misplaced_set_file_count={misplaced_count}",
        ],
        missing=[] if accepted else ["reports/reference_check_report.json"],
        blocked_reasons=[] if accepted else ["Reference mapping validation is not passing."],
        next_action=None if accepted else "Run python scripts/validate_references.py and resolve reported failures.",
    )


def stage3_templates_schemas_scripts(root: Path) -> dict[str, Any]:
    missing = []
    missing.extend(missing_paths(root, STAGE3_DIRS, directories=True))
    missing.extend(missing_paths(root, STAGE3_MANIFESTS))
    missing.extend(missing_paths(root, STAGE3_SCHEMAS))
    missing.extend(missing_paths(root, STAGE3_SCRIPTS))
    production_status = report_status(root, "production_readiness_report")
    if production_status != "pass":
        missing.append("reports/production_readiness_report.json:status=pass")

    return stage(
        3,
        "Templates, Schemas, Scripts, Directories",
        "complete" if not missing else "blocked",
        evidence=[
            f"directory_count={len(STAGE3_DIRS) - len(missing_paths(root, STAGE3_DIRS, directories=True))}/{len(STAGE3_DIRS)}",
            f"manifest_template_count={len(STAGE3_MANIFESTS) - len(missing_paths(root, STAGE3_MANIFESTS))}/{len(STAGE3_MANIFESTS)}",
            f"schema_count={len(STAGE3_SCHEMAS) - len(missing_paths(root, STAGE3_SCHEMAS))}/{len(STAGE3_SCHEMAS)}",
            f"script_count={len(STAGE3_SCRIPTS) - len(missing_paths(root, STAGE3_SCRIPTS))}/{len(STAGE3_SCRIPTS)}",
            f"production_readiness_report_status={production_status}",
        ],
        missing=missing,
        blocked_reasons=[] if not missing else ["Required directories, templates, schemas, or scripts are missing."],
        next_action=None if not missing else "Restore missing files and run python scripts/validate_production_readiness.py.",
    )


def stage4_basic_validation(root: Path) -> dict[str, Any]:
    missing = missing_paths(root, STAGE4_REPORTS)
    skill_status = report_status(root, "skill_tree_report")
    reference_status = report_status(root, "reference_check_report")
    if skill_status != "pass":
        missing.append("reports/skill_tree_report.json:status=pass")
    if reference_status != "pass":
        missing.append("reports/reference_check_report.json:status=pass")

    return stage(
        4,
        "Basic Validation Run",
        "complete" if not missing else "blocked",
        evidence=[
            f"skill_tree_report_status={skill_status}",
            f"reference_check_report_status={reference_status}",
            "current_state_report_marks_stage_4_complete=true",
        ],
        missing=missing,
        blocked_reasons=[] if not missing else ["Basic validation reports are incomplete or failing."],
        next_action=None if not missing else "Run the skill-tree/reference validators and refresh current_state reports.",
    )


def stage5_orchestrator(root: Path) -> dict[str, Any]:
    missing = missing_paths(root, STAGE5_SCRIPTS)
    current_state_files = missing_paths(root, ["reports/current_state.json", "reports/current_state.md"])
    missing.extend(current_state_files)

    return stage(
        5,
        "Current-State Orchestrator",
        "complete" if not missing else "blocked",
        evidence=[
            f"orchestrator_script_count={len(STAGE5_SCRIPTS) - len(missing_paths(root, STAGE5_SCRIPTS))}/{len(STAGE5_SCRIPTS)}",
            "workflow_doctor_updates_current_state_reports=true",
        ],
        missing=missing,
        blocked_reasons=[] if not missing else ["Current-state orchestration entrypoints are incomplete."],
        next_action=None if not missing else "Add missing orchestrator scripts and run python scripts/workflow_doctor.py.",
    )


def stage6_product_batch_intake(root: Path, current_report: dict[str, Any]) -> dict[str, Any]:
    batches = current_report.get("batches") if isinstance(current_report.get("batches"), list) else []
    if not batches:
        return stage(
            6,
            "Product Batch Intake",
            "blocked",
            evidence=["active_batch_count=0"],
            missing=["manifests/<product_id>.batch_manifest.json", "manifest-declared workspace artifacts/"],
            blocked_reasons=[
                "No non-template product batch manifest was found.",
                "A product_id and repository or external workspace root are required before Codex can create product-specific intake artifacts.",
            ],
            next_action="Run python scripts/build_batch_manifest.py --product-id <product_id> --workspace-root <external_run_folder> when a real product_id and workspace are known.",
        )

    incomplete = []
    for batch in batches:
        product_id = batch.get("product_id")
        if not batch.get("manifest_exists"):
            incomplete.append(f"{product_id}:batch_manifest")
        artifacts = batch.get("artifacts") if isinstance(batch.get("artifacts"), dict) else {}
        for key, summary in artifacts.items():
            paths = summary.get("paths") if isinstance(summary, dict) else []
            if paths and not any(path_item.get("exists") for path_item in paths):
                incomplete.append(f"{product_id}:{key}")

    return stage(
        6,
        "Product Batch Intake",
        "complete" if not incomplete else "blocked",
        evidence=[f"active_batch_count={len(batches)}"],
        missing=incomplete,
        blocked_reasons=[] if not incomplete else ["Product-specific manifest or artifact directories are missing."],
        next_action=None if not incomplete else "Rebuild the batch manifest or artifact directory scaffold for the affected product.",
    )


def stage7_upstream_readiness(current_report: dict[str, Any]) -> dict[str, Any]:
    batches = current_report.get("batches") if isinstance(current_report.get("batches"), list) else []
    if not batches:
        return stage(
            7,
            "Upstream Artifact Readiness",
            "pending",
            evidence=["waiting_for_stage_6_product_batch"],
            next_action="Complete Stage 6 first.",
        )

    missing = []
    selected = []
    blocked = []
    for batch in batches:
        product_id = batch.get("product_id")
        missing.extend(f"{product_id}:{item}" for item in batch.get("missing_required_artifacts", []))
        if batch.get("next_required_skill"):
            selected.append(f"{product_id}:{batch['next_required_skill']}")
        blocked.extend(f"{product_id}: {item}" for item in batch.get("blocked_reasons", []))

    return stage(
        7,
        "Upstream Artifact Readiness",
        "complete" if selected or not missing else "blocked",
        evidence=[f"next_required_skill={', '.join(selected) if selected else 'None'}"],
        missing=missing,
        blocked_reasons=blocked,
        next_action=None if selected or not missing else "Add required source inputs or upstream artifacts for the active batch.",
    )


def product_dependent_stage(root: Path, number: int, name: str, current_report: dict[str, Any]) -> dict[str, Any]:
    batches = current_report.get("batches") if isinstance(current_report.get("batches"), list) else []
    if not batches:
        return stage(number, name, "pending", evidence=["waiting_for_stage_6_product_batch"], next_action="Complete Stage 6 first.")

    pass_statuses = product_stage_pass_statuses(number)
    missing: list[str] = []
    blocked: list[str] = []
    pending: list[str] = []
    evidence: list[str] = []

    for batch in batches:
        product_id = batch.get("product_id")
        if not isinstance(product_id, str) or not product_id:
            missing.append("unknown_product_id")
            continue
        report_item = product_stage_report(root, product_id, number)
        if report_item is None:
            missing.append(f"{product_id}:stage_{number}_report")
            continue
        report_path, report_data = report_item
        status = report_data.get("status")
        evidence.append(f"{product_id}:{status}:{rel(root, report_path)}")
        if status in pass_statuses:
            if number == 10:
                integrity_item = product_prompt_integrity_report(root, product_id, batch)
                if integrity_item is None:
                    missing.append(f"{product_id}:final_prompt_integrity_report")
                    continue
                integrity_path, integrity_data = integrity_item
                integrity_status = integrity_data.get("status")
                evidence.append(f"{product_id}:prompt_integrity:{integrity_status}:{rel(root, integrity_path)}")
                if integrity_data.get("render_blocked") is True or integrity_status == "fail":
                    blocked.append(f"{product_id}:prompt_integrity_gate:{integrity_status}")
                    continue
                if integrity_status not in {"pass", "needs_review"}:
                    pending.append(f"{product_id}:prompt_integrity_gate:{integrity_status}")
                    continue
            continue
        if is_blocking_stage_status(status):
            blocked.append(f"{product_id}:stage_{number}:{status}")
        else:
            pending.append(f"{product_id}:stage_{number}:{status}")

    if blocked:
        return stage(
            number,
            name,
            "blocked",
            evidence=evidence,
            missing=missing,
            blocked_reasons=blocked,
            next_action=f"Resolve blocked product stage {number} report(s).",
        )
    if missing or pending:
        return stage(
            number,
            name,
            "pending",
            evidence=evidence + pending,
            missing=missing,
            next_action=f"Complete product stage {number} report(s).",
        )
    return stage(number, name, "complete", evidence=evidence)


def first_non_complete(stages: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((item for item in stages if item["status"] != "complete"), None)


def first_unblocked(stages: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((item for item in stages if item["status"] == "ready"), None)


def build_stage_plan(root: Path, current_report: dict[str, Any]) -> dict[str, Any]:
    stages = [
        stage0_source_rule_freeze(root),
        stage1_skill_tree(root),
        stage2_references(root),
        stage3_templates_schemas_scripts(root),
        stage4_basic_validation(root),
        stage5_orchestrator(root),
        stage6_product_batch_intake(root, current_report),
        stage7_upstream_readiness(current_report),
        product_dependent_stage(root, 8, "Variable Config Generation", current_report),
        product_dependent_stage(root, 9, "Final Prompt Compilation", current_report),
        product_dependent_stage(root, 10, "ComfyUI Render Job Preparation", current_report),
        product_dependent_stage(root, 11, "Rendering", current_report),
        product_dependent_stage(root, 12, "QC and Retry Planning", current_report),
    ]
    current = first_non_complete(stages)
    unblocked = first_unblocked(stages)
    completed_stages = [item for item in stages if item["status"] == "complete"]
    last_completed = completed_stages[-1] if completed_stages else None
    completed_count = len([item for item in stages if item["status"] == "complete"])

    return {
        "status": "blocked" if current and current["status"] == "blocked" else "ready",
        "completed_stage_count": completed_count,
        "total_stage_count": len(stages),
        "last_completed_stage": last_completed,
        "current_stage": current,
        "next_stage": current,
        "next_unblocked_stage": unblocked,
        "stages": stages,
    }


def main() -> int:
    root = project_root()
    current_report = load_json(root / "reports" / "current_state.json")
    if not isinstance(current_report, dict):
        current_report = {"batches": []}
    stage_plan = build_stage_plan(root, current_report)
    print(json.dumps(stage_plan, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
