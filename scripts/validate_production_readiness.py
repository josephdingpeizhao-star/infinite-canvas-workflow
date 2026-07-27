from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REQUIRED_DIRS = [
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

REQUIRED_DOCS = [
    "AGENTS.md",
    "docs/ARCHITECTURE.md",
    "docs/STAGE_PLAN.md",
    "docs/CURRENT_PROGRESS.md",
]

REQUIRED_MANIFESTS = {
    "manifests/workflow_architecture.json": {
        "top_level": [
            "artifact_type",
            "version",
            "summary",
            "layers",
            "hard_rules",
        ],
        "layers": [
            "chatgpt_human_review",
            "codex_control",
            "comfyui_execution",
            "structured_artifacts",
            "reports_and_gates",
        ],
        "required_product_artifacts": [
            "batch_manifest",
            "asset_manifest",
            "product_identity_archive",
            "style_master",
            "angle_inventory",
            "main_variable_configs",
            "detail_variable_configs",
            "final_prompts",
            "comfyui_jobs",
            "qc_reports",
        ],
    },
    "manifests/batch_manifest.template.json": {
        "top_level": [
            "batch_id",
            "product_id",
            "batch_type",
            "user_declared_set_product",
            "requested_outputs",
            "current_stage",
            "next_skill",
            "workspace",
            "inputs",
            "drafts",
            "artifacts",
            "outputs",
            "missing_required_artifacts",
            "blocked_reasons",
            "notes",
        ],
        "workspace": [
            "mode",
            "root",
            "layout",
            "manifests_root",
            "inputs_root",
            "drafts_root",
            "artifacts_root",
            "outputs_root",
        ],
        "inputs": [
            "white_bg_images",
            "style_reference_images",
            "set_group_images",
            "component_white_bg_images",
        ],
        "drafts": [
            "product_identity_draft",
            "style_master_draft",
        ],
        "artifacts": [
            "asset_manifest",
            "product_identity_archive",
            "style_master",
            "angle_inventory",
            "main_variable_configs",
            "detail_variable_configs",
            "set_product_identity",
            "set_angle_layout_inventory",
            "final_prompts",
            "comfyui_jobs",
            "qc_reports",
        ],
        "outputs": [
            "renders",
            "repaired",
        ],
    },
    "manifests/asset_manifest.template.json": {
        "asset_fields": [
            "asset_id",
            "file_path",
            "asset_role",
            "is_single_product_white_bg",
            "is_set_group_shot",
            "is_style_reference",
            "bound_angle_slot",
            "component_id",
            "notes",
        ],
    },
}

REQUIRED_SCHEMAS = [
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

REQUIRED_SCRIPTS = [
    "scripts/validate_workflow_architecture.py",
    "scripts/validate_skill_tree.py",
    "scripts/validate_references.py",
    "scripts/validate_artifact_schema.py",
    "scripts/build_batch_manifest.py",
    "scripts/build_runtime_rule_index.py",
    "scripts/compile_final_prompts.py",
    "scripts/validate_final_prompt_integrity.py",
    "scripts/detect_current_state.py",
    "scripts/detect_current_stage.py",
    "scripts/pre_render_reference_gate.py",
    "scripts/run_comfy_cloud_batch_robust.py",
    "scripts/submit_comfy_cloud_jobs.py",
    "scripts/workflow_doctor.py",
    "scripts/validate_production_readiness.py",
]

REQUIRED_REPORTS = [
    "reports/workflow_architecture_report.json",
    "reports/workflow_architecture_report.md",
    "reports/skill_tree_report.json",
    "reports/skill_tree_report.md",
    "reports/reference_check_report.json",
    "reports/reference_check_report.md",
    "reports/current_state.json",
    "reports/current_state.md",
]


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def load_json(path: Path, errors: list[dict[str, str]]) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        errors.append({"path": str(path), "message": "file not found"})
    except json.JSONDecodeError as exc:
        errors.append({"path": str(path), "message": f"invalid JSON: {exc}"})
    return None


def missing_keys(data: dict[str, Any], keys: list[str]) -> list[str]:
    return [key for key in keys if key not in data]


def check_dirs(root: Path, errors: list[dict[str, str]], warnings: list[dict[str, str]]) -> list[dict[str, Any]]:
    results = []
    for directory in REQUIRED_DIRS:
        path = root / directory
        exists = path.is_dir()
        gitkeep_exists = (path / ".gitkeep").is_file()
        results.append({"path": directory, "exists": exists, "gitkeep_exists": gitkeep_exists})
        if not exists:
            errors.append({"path": directory, "message": "required directory missing"})
        elif directory not in {"manifests", "schemas", "scripts", "reports"} and not gitkeep_exists:
            warnings.append({"path": directory, "message": "template or archive directory has no .gitkeep"})
    return results


def check_manifests(root: Path, errors: list[dict[str, str]]) -> list[dict[str, Any]]:
    results = []
    for manifest_path, requirements in REQUIRED_MANIFESTS.items():
        path = root / manifest_path
        data = load_json(path, errors)
        item = {"path": manifest_path, "exists": path.is_file(), "valid_json": data is not None}
        if data is None:
            results.append(item)
            continue

        if manifest_path.endswith("workflow_architecture.json"):
            top_missing = missing_keys(data, requirements["top_level"])
            layers = data.get("layers") if isinstance(data.get("layers"), dict) else {}
            layer_missing = missing_keys(layers, requirements["layers"])
            required_artifacts = (layers.get("structured_artifacts") or {}).get("required_product_artifacts")
            declared_artifacts = set(required_artifacts) if isinstance(required_artifacts, list) else set()
            missing_artifacts = sorted(set(requirements["required_product_artifacts"]) - declared_artifacts)
            item.update(
                {
                    "missing_top_level_keys": top_missing,
                    "missing_layer_keys": layer_missing,
                    "missing_required_product_artifacts": missing_artifacts,
                    "chatgpt_production_execution_allowed": (layers.get("chatgpt_human_review") or {}).get("production_execution_allowed"),
                    "codex_must_use_repository_state": (layers.get("codex_control") or {}).get("must_use_repository_state"),
                    "comfyui_decision_authority_allowed": (layers.get("comfyui_execution") or {}).get("decision_authority_allowed"),
                    "reports_gate_progression": (layers.get("reports_and_gates") or {}).get("codex_must_use_reports_for_progression"),
                }
            )
            for key in top_missing:
                errors.append({"path": manifest_path, "message": f"missing top-level key: {key}"})
            for key in layer_missing:
                errors.append({"path": manifest_path, "message": f"missing architecture layer: {key}"})
            for key in missing_artifacts:
                errors.append({"path": manifest_path, "message": f"missing required product artifact: {key}"})
            if data.get("artifact_type") != "workflow_architecture":
                errors.append({"path": manifest_path, "message": "artifact_type must be workflow_architecture"})
            if (layers.get("chatgpt_human_review") or {}).get("production_execution_allowed") is not False:
                errors.append({"path": manifest_path, "message": "ChatGPT production execution must be disabled"})
            if (layers.get("codex_control") or {}).get("must_use_repository_state") is not True:
                errors.append({"path": manifest_path, "message": "Codex must use repository state"})
            if (layers.get("codex_control") or {}).get("forbidden_state_source") != "external_chat_memory":
                errors.append({"path": manifest_path, "message": "external_chat_memory must be forbidden as production state"})
            if (layers.get("comfyui_execution") or {}).get("decision_authority_allowed") is not False:
                errors.append({"path": manifest_path, "message": "ComfyUI decision authority must be disabled"})
            if (layers.get("structured_artifacts") or {}).get("base_path_pattern") != "artifacts/{product_id}/":
                errors.append({"path": manifest_path, "message": "structured artifact base path must be artifacts/{product_id}/"})
            if (layers.get("structured_artifacts") or {}).get("external_workspace_base_path_pattern") != "{workspace_root}/artifacts/":
                errors.append({"path": manifest_path, "message": "external workspace artifact base path must be {workspace_root}/artifacts/"})
            if (layers.get("structured_artifacts") or {}).get("external_workspace_manifest_connection") is not True:
                errors.append({"path": manifest_path, "message": "external workspace must be connected through batch manifests"})
            reports_layer = layers.get("reports_and_gates") or {}
            if (
                reports_layer.get("requires_json_report") is not True
                or reports_layer.get("requires_markdown_report") is not True
                or reports_layer.get("codex_must_use_reports_for_progression") is not True
            ):
                errors.append({"path": manifest_path, "message": "reports_and_gates must require JSON, Markdown, and progression gating"})

        if manifest_path.endswith("batch_manifest.template.json"):
            top_missing = missing_keys(data, requirements["top_level"])
            workspace_missing = missing_keys(data.get("workspace", {}), requirements["workspace"])
            inputs_missing = missing_keys(data.get("inputs", {}), requirements["inputs"])
            drafts_missing = missing_keys(data.get("drafts", {}), requirements["drafts"])
            artifacts_missing = missing_keys(data.get("artifacts", {}), requirements["artifacts"])
            outputs_missing = missing_keys(data.get("outputs", {}), requirements["outputs"])
            item.update(
                {
                    "missing_top_level_keys": top_missing,
                    "missing_workspace_keys": workspace_missing,
                    "missing_input_keys": inputs_missing,
                    "missing_draft_keys": drafts_missing,
                    "missing_artifact_keys": artifacts_missing,
                    "missing_output_keys": outputs_missing,
                    "batch_type_default_single": data.get("batch_type") == "single",
                    "user_declared_set_product_default_false": data.get("user_declared_set_product") is False,
                }
            )
            for key in top_missing:
                errors.append({"path": manifest_path, "message": f"missing top-level key: {key}"})
            for key in workspace_missing:
                errors.append({"path": manifest_path, "message": f"missing workspace key: {key}"})
            for key in inputs_missing:
                errors.append({"path": manifest_path, "message": f"missing inputs key: {key}"})
            for key in drafts_missing:
                errors.append({"path": manifest_path, "message": f"missing drafts key: {key}"})
            for key in artifacts_missing:
                errors.append({"path": manifest_path, "message": f"missing artifacts key: {key}"})
            for key in outputs_missing:
                errors.append({"path": manifest_path, "message": f"missing outputs key: {key}"})
            if data.get("batch_type") != "single":
                errors.append({"path": manifest_path, "message": "batch_type default must be single"})
            if data.get("user_declared_set_product") is not False:
                errors.append({"path": manifest_path, "message": "user_declared_set_product default must be false"})

        if manifest_path.endswith("asset_manifest.template.json"):
            assets = data.get("assets")
            first_asset = assets[0] if isinstance(assets, list) and assets else {}
            asset_missing = missing_keys(first_asset, requirements["asset_fields"])
            item.update({"asset_count": len(assets) if isinstance(assets, list) else 0, "missing_asset_fields": asset_missing})
            if not isinstance(assets, list):
                errors.append({"path": manifest_path, "message": "assets must be an array"})
            elif not assets:
                errors.append({"path": manifest_path, "message": "assets must contain one template item"})
            for key in asset_missing:
                errors.append({"path": manifest_path, "message": f"missing asset field: {key}"})

        results.append(item)
    return results


def property_const(schema: dict[str, Any], property_name: str) -> Any:
    return schema.get("properties", {}).get(property_name, {}).get("const")


def check_schemas(root: Path, errors: list[dict[str, str]]) -> list[dict[str, Any]]:
    results = []
    for schema_path in REQUIRED_SCHEMAS:
        path = root / schema_path
        schema = load_json(path, errors)
        item = {"path": schema_path, "exists": path.is_file(), "valid_json": schema is not None}
        if schema is None:
            results.append(item)
            continue

        required = schema.get("required", [])
        properties = schema.get("properties", {})
        item.update(
            {
                "schema_declared": "$schema" in schema,
                "type": schema.get("type"),
                "required": required,
            }
        )
        if "$schema" not in schema:
            errors.append({"path": schema_path, "message": "missing $schema declaration"})
        if schema.get("type") != "object":
            errors.append({"path": schema_path, "message": "top-level schema type must be object"})

        if schema_path.endswith("routing_decision.schema.json"):
            needed = [
                "batch_type",
                "current_stage",
                "next_skill",
                "available_artifacts",
                "missing_required_artifacts",
                "allowed_skills",
                "forbidden_skills",
            ]
            for key in missing_keys({key: True for key in required}, needed):
                errors.append({"path": schema_path, "message": f"routing_decision missing required key: {key}"})

        if schema_path.endswith(
            (
                "main_variable_config.schema.json",
                "detail_variable_config.schema.json",
            )
        ):
            count_property = properties.get("config_count") or {}
            configs_property = properties.get("configs") or {}
            if (
                count_property.get("type") != "integer"
                or count_property.get("minimum") != 1
                or count_property.get("maximum") != 30
                or configs_property.get("minItems") != 1
                or configs_property.get("maxItems") != 30
            ):
                errors.append(
                    {
                        "path": schema_path,
                        "message": (
                            "variable config count and configs must both allow "
                            "the manifest-driven range 1 through 30"
                        ),
                    }
                )

        if schema_path.endswith("final_prompt.schema.json"):
            if "upstream_artifacts" not in required or "variable_config" not in required:
                errors.append({"path": schema_path, "message": "final_prompt must require upstream_artifacts and variable_config"})
            if property_const(schema, "uses_upstream_prompt_files_as_visual_requirements") is not False:
                errors.append({"path": schema_path, "message": "final_prompt must forbid upstream prompt files as final visual requirements"})

        if schema_path.endswith("final_prompt_integrity_report.schema.json"):
            if property_const(schema, "artifact_type") != "final_prompt_integrity_report":
                errors.append({"path": schema_path, "message": "final_prompt_integrity_report must declare the expected artifact_type"})
            if property_const(schema, "image_generation_performed") is not False:
                errors.append({"path": schema_path, "message": "final_prompt_integrity_report must forbid image generation"})
            if property_const(schema, "comfyui_execution_performed") is not False:
                errors.append({"path": schema_path, "message": "final_prompt_integrity_report must forbid ComfyUI execution"})

        if schema_path.endswith("qc_report.schema.json"):
            if property_const(schema, "adds_new_generation_direction") is not False:
                errors.append({"path": schema_path, "message": "qc_report must forbid new generation direction"})

        if schema_path.endswith("workflow_architecture.schema.json"):
            layers_required = (properties.get("layers") or {}).get("required", [])
            needed = [
                "chatgpt_human_review",
                "codex_control",
                "comfyui_execution",
                "structured_artifacts",
                "reports_and_gates",
            ]
            for key in missing_keys({key: True for key in layers_required}, needed):
                errors.append({"path": schema_path, "message": f"workflow_architecture missing required layer: {key}"})

        if schema_path.endswith(
            (
                "set_product_identity.schema.json",
                "set_angle_layout_inventory.schema.json",
                "set_variable_config_extension.schema.json",
            )
        ):
            has_set_gate_fields = "user_declared_set_product" in properties and "explicit_set_request" in properties
            if not has_set_gate_fields or "anyOf" not in schema:
                errors.append({"path": schema_path, "message": "set schema must require user declaration or explicit set request"})

        results.append(item)
    return results


def check_files(root: Path, paths: list[str], errors: list[dict[str, str]]) -> list[dict[str, Any]]:
    results = []
    for file_path in paths:
        path = root / file_path
        exists = path.is_file()
        results.append({"path": file_path, "exists": exists, "size": path.stat().st_size if exists else 0})
        if not exists:
            errors.append({"path": file_path, "message": "required file missing"})
    return results


def check_previous_reports(root: Path, errors: list[dict[str, str]]) -> dict[str, Any]:
    result = {}
    for report_name in ("workflow_architecture_report", "skill_tree_report", "reference_check_report"):
        path = root / "reports" / f"{report_name}.json"
        report_errors: list[dict[str, str]] = []
        data = load_json(path, report_errors)
        result[report_name] = {
            "path": str(path.relative_to(root)),
            "exists": path.is_file(),
            "status": data.get("status") if isinstance(data, dict) else None,
        }
        if report_errors:
            errors.extend(report_errors)
        elif not isinstance(data, dict) or data.get("status") != "pass":
            errors.append({"path": str(path.relative_to(root)), "message": "previous validation report is not pass"})
        elif report_name == "workflow_architecture_report":
            if data.get("architecture_manifest") != "manifests/workflow_architecture.json":
                errors.append({"path": str(path.relative_to(root)), "message": "workflow architecture report points to an unexpected manifest"})
        elif report_name == "skill_tree_report":
            roots = data.get("skill_roots", {})
            if roots.get("primary_skill_tree") not in {".agents\\skills", ".agents/skills"}:
                errors.append({"path": str(path.relative_to(root)), "message": "skill tree primary must be .agents/skills"})
            codex_root = roots.get("codex_skill_tree", {})
            if codex_root.get("exists") and codex_root.get("role") != "legacy_skill_tree":
                errors.append({"path": str(path.relative_to(root)), "message": ".codex/skills must be marked legacy_skill_tree when present"})
            if roots.get("mirror_status") == "needs_manual_review":
                errors.append({"path": str(path.relative_to(root)), "message": "skill trees need manual review"})
        elif report_name == "reference_check_report":
            roots = data.get("skill_roots", {})
            if roots.get("primary_skill_tree") not in {".agents\\skills", ".agents/skills"}:
                errors.append({"path": str(path.relative_to(root)), "message": "reference check primary must be .agents/skills"})
            codex_root = roots.get("codex_skill_tree", {})
            if codex_root.get("exists") and codex_root.get("role") != "legacy_skill_tree":
                errors.append({"path": str(path.relative_to(root)), "message": ".codex/skills must be marked legacy_skill_tree in reference check when present"})
    return result


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_markdown(path: Path, report: dict[str, Any]) -> None:
    lines = [
        "# Production Readiness Report",
        "",
        f"- status: {report['status']}",
        f"- checked_at: {report['checked_at']}",
        f"- error_count: {len(report['errors'])}",
        f"- warning_count: {len(report['warnings'])}",
        "",
        "## Errors",
        "",
    ]
    if report["errors"]:
        lines.extend(f"- {item['path']}: {item['message']}" for item in report["errors"])
    else:
        lines.append("- None")
    lines.extend(["", "## Warnings", ""])
    if report["warnings"]:
        lines.extend(f"- {item['path']}: {item['message']}" for item in report["warnings"])
    else:
        lines.append("- None")
    lines.extend(["", "## Previous Reports", ""])
    for name, item in report["previous_reports"].items():
        lines.append(f"- {name}: {item['status']}")
    lines.extend(["", "## Checked File Groups", ""])
    lines.append(f"- directories: {len(report['directories'])}")
    lines.append(f"- manifests: {len(report['manifests'])}")
    lines.append(f"- schemas: {len(report['schemas'])}")
    lines.append(f"- documents: {len(report['documents'])}")
    lines.append(f"- scripts: {len(report['scripts'])}")
    lines.append(f"- reports: {len(report['reports'])}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    root = project_root()
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []

    report = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "project_root": str(root),
        "directories": check_dirs(root, errors, warnings),
        "manifests": check_manifests(root, errors),
        "schemas": check_schemas(root, errors),
        "documents": check_files(root, REQUIRED_DOCS, errors),
        "scripts": check_files(root, REQUIRED_SCRIPTS, errors),
        "reports": check_files(root, REQUIRED_REPORTS, errors),
        "previous_reports": check_previous_reports(root, errors),
        "errors": errors,
        "warnings": warnings,
    }
    report["status"] = "pass" if not errors else "fail"

    write_json(root / "reports" / "production_readiness_report.json", report)
    write_markdown(root / "reports" / "production_readiness_report.md", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
