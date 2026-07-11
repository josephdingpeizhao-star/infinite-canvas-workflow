from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ARCHITECTURE_PATH = "manifests/workflow_architecture.json"
SCHEMA_PATH = "schemas/workflow_architecture.schema.json"
REQUIRED_PRODUCT_ARTIFACTS = {
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
}
REQUIRED_HARD_RULE_PHRASES = [
    "ChatGPT is not the primary production execution environment.",
    "Codex must work from repository state",
    "ComfyUI executes image generation",
    "artifacts/{product_id}/",
    "manifest-declared external workspace",
    "JSON and Markdown reports",
    "Codex must use reports",
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


def add_check(checks: list[dict[str, Any]], name: str, passed: bool, detail: str) -> None:
    checks.append({"name": name, "status": "pass" if passed else "fail", "detail": detail})


def nested(data: dict[str, Any], *keys: str) -> Any:
    value: Any = data
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def validate_architecture(root: Path, data: dict[str, Any] | None) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    checks: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    path = root / ARCHITECTURE_PATH

    if not isinstance(data, dict):
        errors.append({"path": ARCHITECTURE_PATH, "message": "architecture manifest is missing or invalid"})
        add_check(checks, "architecture_manifest_valid", False, "manifest could not be parsed")
        return checks, errors

    add_check(checks, "artifact_type", data.get("artifact_type") == "workflow_architecture", "artifact_type must be workflow_architecture")
    if data.get("artifact_type") != "workflow_architecture":
        errors.append({"path": ARCHITECTURE_PATH, "message": "artifact_type must be workflow_architecture"})

    version_ok = isinstance(data.get("version"), str) and bool(data.get("version"))
    add_check(checks, "version", version_ok, "version must be a non-empty string")
    if not version_ok:
        errors.append({"path": ARCHITECTURE_PATH, "message": "version must be a non-empty string"})

    chatgpt_review_only = nested(data, "layers", "chatgpt_human_review", "production_execution_allowed") is False
    add_check(checks, "chatgpt_review_only", chatgpt_review_only, "ChatGPT production execution must be false")
    if not chatgpt_review_only:
        errors.append({"path": ARCHITECTURE_PATH, "message": "ChatGPT production execution must be disabled"})

    codex_repo_state = nested(data, "layers", "codex_control", "must_use_repository_state") is True
    add_check(checks, "codex_uses_repository_state", codex_repo_state, "Codex must use repository state")
    if not codex_repo_state:
        errors.append({"path": ARCHITECTURE_PATH, "message": "Codex must be configured to use repository state"})

    forbidden_state_source = nested(data, "layers", "codex_control", "forbidden_state_source") == "external_chat_memory"
    add_check(checks, "external_chat_memory_forbidden", forbidden_state_source, "external chat memory must be forbidden as production state")
    if not forbidden_state_source:
        errors.append({"path": ARCHITECTURE_PATH, "message": "external_chat_memory must be forbidden as production state"})

    comfyui_no_decisions = nested(data, "layers", "comfyui_execution", "decision_authority_allowed") is False
    add_check(checks, "comfyui_execution_only", comfyui_no_decisions, "ComfyUI decision authority must be false")
    if not comfyui_no_decisions:
        errors.append({"path": ARCHITECTURE_PATH, "message": "ComfyUI decision authority must be disabled"})

    base_path_ok = nested(data, "layers", "structured_artifacts", "base_path_pattern") == "artifacts/{product_id}/"
    add_check(checks, "artifact_base_path", base_path_ok, "structured artifacts must use artifacts/{product_id}/")
    if not base_path_ok:
        errors.append({"path": ARCHITECTURE_PATH, "message": "structured artifact base path must be artifacts/{product_id}/"})

    external_base_path_ok = nested(data, "layers", "structured_artifacts", "external_workspace_base_path_pattern") == "{workspace_root}/artifacts/"
    add_check(checks, "external_workspace_base_path", external_base_path_ok, "external workspace artifacts must be manifest-declared")
    if not external_base_path_ok:
        errors.append({"path": ARCHITECTURE_PATH, "message": "external workspace artifact base path must be {workspace_root}/artifacts/"})

    external_manifest_connection_ok = nested(data, "layers", "structured_artifacts", "external_workspace_manifest_connection") is True
    add_check(checks, "external_workspace_manifest_connection", external_manifest_connection_ok, "external workspace must be connected through batch manifests")
    if not external_manifest_connection_ok:
        errors.append({"path": ARCHITECTURE_PATH, "message": "external workspace must be connected through batch manifests"})

    declared_artifacts = nested(data, "layers", "structured_artifacts", "required_product_artifacts")
    declared_set = set(declared_artifacts) if isinstance(declared_artifacts, list) else set()
    missing_artifacts = sorted(REQUIRED_PRODUCT_ARTIFACTS - declared_set)
    add_check(checks, "required_product_artifacts", not missing_artifacts, "all required product artifacts must be declared")
    for artifact in missing_artifacts:
        errors.append({"path": ARCHITECTURE_PATH, "message": f"missing required product artifact: {artifact}"})

    reports_layer = nested(data, "layers", "reports_and_gates")
    reports_ok = (
        isinstance(reports_layer, dict)
        and reports_layer.get("requires_json_report") is True
        and reports_layer.get("requires_markdown_report") is True
        and reports_layer.get("codex_must_use_reports_for_progression") is True
    )
    add_check(checks, "reports_gate_progression", reports_ok, "JSON and Markdown reports must gate progression")
    if not reports_ok:
        errors.append({"path": ARCHITECTURE_PATH, "message": "reports_and_gates must require JSON, Markdown, and progression gating"})

    hard_rules = data.get("hard_rules")
    rules_text = "\n".join(hard_rules) if isinstance(hard_rules, list) else ""
    for phrase in REQUIRED_HARD_RULE_PHRASES:
        phrase_present = phrase in rules_text
        add_check(checks, f"hard_rule_phrase:{phrase}", phrase_present, f"hard_rules must include: {phrase}")
        if not phrase_present:
            errors.append({"path": ARCHITECTURE_PATH, "message": f"missing hard rule phrase: {phrase}"})

    add_check(checks, "architecture_manifest_exists", path.is_file(), "workflow architecture manifest must exist")
    return checks, errors


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_markdown(path: Path, report: dict[str, Any]) -> None:
    lines = [
        "# Workflow Architecture Report",
        "",
        f"- status: {report['status']}",
        f"- checked_at: {report['checked_at']}",
        f"- architecture_manifest: {report['architecture_manifest']}",
        f"- schema: {report['schema']}",
        f"- error_count: {len(report['errors'])}",
        "",
        "## Checks",
        "",
    ]
    for check in report["checks"]:
        lines.append(f"- {check['name']}: {check['status']} - {check['detail']}")
    lines.extend(["", "## Errors", ""])
    if report["errors"]:
        lines.extend(f"- {item['path']}: {item['message']}" for item in report["errors"])
    else:
        lines.append("- None")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    root = project_root()
    load_errors: list[dict[str, str]] = []
    schema = load_json(root / SCHEMA_PATH, load_errors)
    architecture = load_json(root / ARCHITECTURE_PATH, load_errors)
    checks, validation_errors = validate_architecture(root, architecture if isinstance(architecture, dict) else None)

    schema_ok = isinstance(schema, dict) and schema.get("$id") == "workflow_architecture.schema.json"
    add_check(checks, "schema_available", schema_ok, "workflow architecture schema must exist and declare the expected id")
    if not schema_ok:
        validation_errors.append({"path": SCHEMA_PATH, "message": "schema must exist and declare $id workflow_architecture.schema.json"})

    errors = load_errors + validation_errors
    report = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "project_root": str(root),
        "architecture_manifest": ARCHITECTURE_PATH,
        "schema": SCHEMA_PATH,
        "checks": checks,
        "errors": errors,
        "status": "pass" if not errors else "fail",
    }
    write_json(root / "reports" / "workflow_architecture_report.json", report)
    write_markdown(root / "reports" / "workflow_architecture_report.md", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
