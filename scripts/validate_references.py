from __future__ import annotations

import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path


REFERENCE_MAP = {
    "workflow-router": [
        "工作流总控规则.txt",
    ],
    "product-identity-archive": [
        "产品身份档案提示词.txt",
    ],
    "style-master-extractor": [
        "反向提取风格母版提示词.txt",
    ],
    "angle-inventory": [
        "角度槽位入库表生成与识别提示词.txt",
    ],
    "main-variable-config": [
        "主图单张变量配置提示词生成.txt",
        "工作流总控规则.txt",
        "真实感约束.txt",
        "道具生成规则模块.txt",
        "电商图片通用质检清单.txt",
    ],
    "detail-variable-config": [
        "详情图单张变量配置提示词生成.txt",
        "工作流总控规则.txt",
        "真实感约束.txt",
        "道具生成规则模块.txt",
        "淘宝天猫详情页链路与平台规范模块.txt",
        "商品信息补充清单提示词.txt",
        "电商图片通用质检清单.txt",
    ],
    "final-prompt-compiler": [
        "工作流总控规则.txt",
        "真实感约束.txt",
        "道具生成规则模块.txt",
        "淘宝天猫详情页链路与平台规范模块.txt",
        "电商图片通用质检清单.txt",
    ],
    "qc-inspector": [
        "电商图片通用质检清单.txt",
        "工作流总控规则.txt",
        "真实感约束.txt",
    ],
    "set-product-identity": [
        "套装产品身份档案提示词.txt",
        "套装产品工作流补充规则.txt",
    ],
    "set-angle-layout-inventory": [
        "套装角度与编排入库表提示词.txt",
        "套装编排规则.txt",
        "套装产品工作流补充规则.txt",
    ],
    "set-variable-config-extension": [
        "套装产品工作流补充规则.txt",
        "套装变量配置补充模块.txt",
        "套装编排规则.txt",
        "套装产品身份档案提示词.txt",
        "套装角度与编排入库表提示词.txt",
    ],
}

SET_SKILLS = {
    "set-product-identity",
    "set-angle-layout-inventory",
    "set-variable-config-extension",
}

RUNTIME_PACKAGE_SKILLS = {
    "main-variable-config",
    "detail-variable-config",
    "final-prompt-compiler",
    "qc-inspector",
}

REQUIRED_RUNTIME_SLICE_KEYS = {
    "slice_id",
    "source_file",
    "line_start",
    "line_end",
    "tags",
    "audit_ref",
    "text",
}

FORBIDDEN_RUNTIME_SLICE_KEYS = {
    "source_path",
    "source_sha256",
}


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def resolve_skill_root(root: Path) -> dict:
    agents_root = root / ".agents" / "skills"
    codex_root = root / ".codex" / "skills"
    if agents_root.is_dir():
        primary = agents_root
        source_status = "agents_primary"
    elif codex_root.is_dir():
        primary = codex_root
        source_status = "codex_legacy_fallback"
    else:
        primary = agents_root
        source_status = "missing_all_skill_trees"

    return {
        "primary_skill_tree": str(primary.relative_to(root)),
        "source_status": source_status,
        "agents_skill_tree": {
            "path": str(agents_root.relative_to(root)),
            "exists": agents_root.is_dir(),
            "role": "primary_skill_tree" if agents_root.is_dir() else None,
        },
        "codex_skill_tree": {
            "path": str(codex_root.relative_to(root)),
            "exists": codex_root.is_dir(),
            "role": "legacy_skill_tree" if codex_root.is_dir() else "absent_legacy_skill_tree",
        },
    }


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def file_sha256(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def source_text(root: Path, source_file: str, line_start: int, line_end: int) -> str | None:
    try:
        lines = (root / source_file).read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    if line_start < 1 or line_end > len(lines) or line_start > line_end:
        return None
    return "\n".join(lines[line_start - 1 : line_end])


def validate_runtime_packages(root: Path, skills_root: Path) -> tuple[list[dict], list[dict]]:
    checks: list[dict] = []
    errors: list[dict] = []
    audit_path = root / "reports" / "runtime_rule_index.json"
    audit_index = load_json(audit_path)
    audit_slices = {}
    if isinstance(audit_index, dict):
        audit_slices = {item.get("slice_id"): item for item in audit_index.get("slices", []) if isinstance(item, dict)}
    else:
        errors.append({"path": str(audit_path.relative_to(root)), "message": "runtime audit index missing or invalid JSON"})

    for skill in sorted(RUNTIME_PACKAGE_SKILLS):
        package_path = skills_root / skill / "references" / "runtime_rule_slices" / f"{skill}.runtime_rule_slices.json"
        package = load_json(package_path)
        item_errors: list[str] = []
        slice_count = 0
        runtime_bytes = package_path.stat().st_size if package_path.is_file() else 0

        if not isinstance(package, dict):
            item_errors.append("runtime package missing or invalid JSON")
        else:
            if package.get("artifact_type") != "runtime_rule_slice_package":
                item_errors.append("artifact_type must be runtime_rule_slice_package")
            if package.get("runtime_package_version") != "2-slim":
                item_errors.append("runtime_package_version must be 2-slim")
            if package.get("audit_index_path") != "reports/runtime_rule_index.json":
                item_errors.append("audit_index_path must point to reports/runtime_rule_index.json")
            slices = package.get("slices")
            if not isinstance(slices, list) or not slices:
                item_errors.append("slices must be a non-empty array")
                slices = []
            slice_count = len(slices)

            for runtime_item in slices:
                if not isinstance(runtime_item, dict):
                    item_errors.append("runtime slice must be an object")
                    continue
                missing_keys = sorted(REQUIRED_RUNTIME_SLICE_KEYS - set(runtime_item))
                forbidden_keys = sorted(FORBIDDEN_RUNTIME_SLICE_KEYS & set(runtime_item))
                if missing_keys:
                    item_errors.append(f"{runtime_item.get('slice_id', '<unknown>')}: missing runtime keys {missing_keys}")
                if forbidden_keys:
                    item_errors.append(f"{runtime_item.get('slice_id', '<unknown>')}: repeated audit metadata {forbidden_keys}")
                slice_id = runtime_item.get("slice_id")
                audit_item = audit_slices.get(slice_id)
                if not isinstance(audit_item, dict):
                    item_errors.append(f"{slice_id}: missing from audit index")
                    continue
                for key in ("source_file", "line_start", "line_end", "text"):
                    if runtime_item.get(key) != audit_item.get(key):
                        item_errors.append(f"{slice_id}: runtime {key} does not match audit index")
                expected_text = source_text(
                    root,
                    str(runtime_item.get("source_file", "")),
                    int(runtime_item.get("line_start", 0)),
                    int(runtime_item.get("line_end", 0)),
                )
                if expected_text is None:
                    item_errors.append(f"{slice_id}: source line range cannot be read")
                elif runtime_item.get("text") != expected_text:
                    item_errors.append(f"{slice_id}: text does not match source rule lines")
                source_file = audit_item.get("source_file")
                if source_file:
                    current_hash = file_sha256(root / str(source_file))
                    if current_hash and audit_item.get("source_sha256") != current_hash:
                        item_errors.append(f"{slice_id}: audit source_sha256 is stale")
                if "source_path" not in audit_item or "source_sha256" not in audit_item:
                    item_errors.append(f"{slice_id}: audit index missing full source metadata")

        status = "pass" if not item_errors else "fail"
        checks.append(
            {
                "skill": skill,
                "package": str(package_path.relative_to(root)),
                "status": status,
                "runtime_package_version": package.get("runtime_package_version") if isinstance(package, dict) else None,
                "slice_count": slice_count,
                "runtime_bytes": runtime_bytes,
                "errors": item_errors,
            }
        )
        for message in item_errors:
            errors.append({"path": str(package_path.relative_to(root)), "message": message})

    return checks, errors


def write_markdown(path: Path, report: dict) -> None:
    roots = report["skill_roots"]
    lines = [
        "# Reference Check Report",
        "",
        f"- status: {report['status']}",
        f"- checked_at: {report['checked_at']}",
        f"- primary_skill_tree: {roots['primary_skill_tree']}",
        f"- source_status: {roots['source_status']}",
        f"- codex_skill_tree_role: {roots['codex_skill_tree']['role']}",
        f"- checked_skill_count: {len(report['skills'])}",
        f"- missing_file_count: {len(report['missing_files'])}",
        f"- extra_file_count: {len(report['extra_files'])}",
        f"- misplaced_set_file_count: {len(report['misplaced_set_files'])}",
        f"- runtime_package_error_count: {len(report['runtime_package_errors'])}",
        "",
        "## Missing Files",
        "",
    ]
    lines.extend(f"- {item['skill']}: {item['file']}" for item in report["missing_files"] or [])
    if not report["missing_files"]:
        lines.append("- None")
    lines.extend(["", "## Extra Files", ""])
    lines.extend(f"- {item['skill']}: {item['file']}" for item in report["extra_files"] or [])
    if not report["extra_files"]:
        lines.append("- None")
    lines.extend(["", "## Misplaced Set Files", ""])
    lines.extend(f"- {item['skill']}: {item['file']}" for item in report["misplaced_set_files"] or [])
    if not report["misplaced_set_files"]:
        lines.append("- None")
    lines.extend(["", "## Per Skill", ""])
    for item in report["skills"]:
        lines.append(
            f"- {item['skill']}: expected={len(item['expected_files'])}, "
            f"actual={len(item['actual_files'])}, missing={len(item['missing_files'])}, "
            f"extra={len(item['extra_files'])}"
        )
    lines.extend(["", "## Runtime Packages", ""])
    for item in report["runtime_package_checks"]:
        lines.append(
            f"- {item['skill']}: status={item['status']}, version={item['runtime_package_version']}, "
            f"slices={item['slice_count']}, bytes={item['runtime_bytes']}"
        )
        for error in item["errors"]:
            lines.append(f"  - {error}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    root = project_root()
    skill_roots = resolve_skill_root(root)
    skills_root = root / skill_roots["primary_skill_tree"]
    reports_dir = root / "reports"

    skills = []
    missing_files = []
    extra_files = []
    misplaced_set_files = []
    missing_reference_dirs = []

    for skill, expected_list in REFERENCE_MAP.items():
        references_dir = skills_root / skill / "references"
        expected = set(expected_list)
        if references_dir.is_dir():
            actual = {path.name for path in references_dir.iterdir() if path.is_file()}
        else:
            actual = set()
            missing_reference_dirs.append(str(references_dir.relative_to(root)))

        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        misplaced = sorted(file_name for file_name in actual if skill not in SET_SKILLS and "套装" in file_name)

        for file_name in missing:
            missing_files.append({"skill": skill, "file": file_name})
        for file_name in extra:
            extra_files.append({"skill": skill, "file": file_name})
        for file_name in misplaced:
            misplaced_set_files.append({"skill": skill, "file": file_name})

        skills.append(
            {
                "skill": skill,
                "references_dir": str(references_dir.relative_to(root)),
                "references_dir_exists": references_dir.is_dir(),
                "expected_files": expected_list,
                "actual_files": sorted(actual),
                "missing_files": missing,
                "extra_files": extra,
                "misplaced_set_files": misplaced,
            }
        )

    runtime_package_checks, runtime_package_errors = validate_runtime_packages(root, skills_root)

    status = "pass"
    if missing_files or extra_files or misplaced_set_files or missing_reference_dirs or runtime_package_errors:
        status = "fail"

    report = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "project_root": str(root),
        "skill_roots": skill_roots,
        "skills": skills,
        "missing_reference_dirs": missing_reference_dirs,
        "missing_files": missing_files,
        "extra_files": extra_files,
        "misplaced_set_files": misplaced_set_files,
        "runtime_package_checks": runtime_package_checks,
        "runtime_package_errors": runtime_package_errors,
    }

    write_json(reports_dir / "reference_check_report.json", report)
    write_markdown(reports_dir / "reference_check_report.md", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if status == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
