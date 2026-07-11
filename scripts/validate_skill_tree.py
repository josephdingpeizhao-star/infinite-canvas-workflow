from __future__ import annotations

import json
import shutil
import hashlib
from datetime import datetime, timezone
from pathlib import Path


REQUIRED_SKILLS = [
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


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def read_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def file_hash(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def collect_tree_files(root: Path) -> dict[str, str | None]:
    if not root.is_dir():
        return {}
    files: dict[str, str | None] = {}
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        files[str(path.relative_to(root))] = file_hash(path)
    return files


def compare_skill_trees(agents_root: Path, codex_root: Path) -> dict:
    agents_files = collect_tree_files(agents_root)
    codex_files = collect_tree_files(codex_root)
    agents_set = set(agents_files)
    codex_set = set(codex_files)
    common = sorted(agents_set & codex_set)
    changed = [
        {"path": path, "agents_sha256": agents_files[path], "codex_sha256": codex_files[path]}
        for path in common
        if agents_files[path] != codex_files[path]
    ]
    return {
        "status": "mirrored_ok" if agents_files == codex_files else "needs_manual_review",
        "missing_in_agents": sorted(codex_set - agents_set),
        "extra_in_agents": sorted(agents_set - codex_set),
        "changed_files": changed,
        "agents_file_count": len(agents_files),
        "codex_file_count": len(codex_files),
    }


def resolve_skill_roots(root: Path) -> dict:
    agents_root = root / ".agents" / "skills"
    codex_root = root / ".codex" / "skills"
    result = {
        "primary_skill_tree": str(agents_root.relative_to(root)),
        "agents_skill_tree": {
            "path": str(agents_root.relative_to(root)),
            "exists": agents_root.is_dir(),
            "role": "primary_skill_tree",
        },
        "codex_skill_tree": {
            "path": str(codex_root.relative_to(root)),
            "exists": codex_root.is_dir(),
            "role": None,
        },
        "mirror_status": None,
        "copy_actions": [],
        "needs_manual_review": [],
        "comparison": None,
    }

    if agents_root.is_dir() and codex_root.is_dir():
        comparison = compare_skill_trees(agents_root, codex_root)
        result["comparison"] = comparison
        result["mirror_status"] = comparison["status"]
        result["codex_skill_tree"]["role"] = "legacy_skill_tree"
        if comparison["status"] == "needs_manual_review":
            result["needs_manual_review"].append(
                {
                    "path": str(codex_root.relative_to(root)),
                    "reason": "both .agents/skills and .codex/skills exist but content hashes differ",
                }
            )
    elif not agents_root.is_dir() and codex_root.is_dir():
        agents_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(codex_root, agents_root)
        result["agents_skill_tree"]["exists"] = True
        result["codex_skill_tree"]["role"] = "legacy_skill_tree"
        result["mirror_status"] = "copied_from_legacy"
        result["copy_actions"].append(
            {
                "source": str(codex_root.relative_to(root)),
                "target": str(agents_root.relative_to(root)),
                "reason": ".agents/skills was missing and .codex/skills existed",
            }
        )
    elif agents_root.is_dir() and not codex_root.is_dir():
        result["mirror_status"] = "agents_only"
        result["codex_skill_tree"]["role"] = "absent_legacy_skill_tree"
    else:
        result["mirror_status"] = "missing_all_skill_trees"
        result["needs_manual_review"].append(
            {
                "path": ".agents/skills",
                "reason": "neither .agents/skills nor .codex/skills exists",
            }
        )

    return result


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_markdown(path: Path, report: dict) -> None:
    roots = report["skill_roots"]
    lines = [
        "# Skill Tree Report",
        "",
        f"- status: {report['status']}",
        f"- checked_at: {report['checked_at']}",
        f"- primary_skill_tree: {roots['primary_skill_tree']}",
        f"- codex_skill_tree_role: {roots['codex_skill_tree']['role']}",
        f"- mirror_status: {roots['mirror_status']}",
        f"- required_skill_count: {len(report['skills'])}",
        f"- missing_skill_count: {len(report['missing_skills'])}",
        f"- missing_skill_md_count: {len(report['missing_skill_md'])}",
        f"- missing_references_dir_count: {len(report['missing_references_dir'])}",
        f"- skill_tree_copy_action_count: {len(roots['copy_actions'])}",
        f"- skill_tree_needs_manual_review_count: {len(roots['needs_manual_review'])}",
        f"- moved_migration_file_count: {len(report['migration_files']['moved'])}",
        f"- needs_manual_review_count: {len(report['migration_files']['needs_manual_review'])}",
        "",
        "## Skill Trees",
        "",
        f"- .agents/skills: exists={roots['agents_skill_tree']['exists']}, role={roots['agents_skill_tree']['role']}",
        f"- .codex/skills: exists={roots['codex_skill_tree']['exists']}, role={roots['codex_skill_tree']['role']}",
        f"- mirror_status: {roots['mirror_status']}",
        "",
        "## Skill Tree Copy Actions",
        "",
    ]
    if roots["copy_actions"]:
        for item in roots["copy_actions"]:
            lines.append(f"- {item['source']} -> {item['target']}: {item['reason']}")
    else:
        lines.append("- None")
    lines.extend([
        "",
        "## Skill Tree Manual Review",
        "",
    ])
    if roots["needs_manual_review"]:
        for item in roots["needs_manual_review"]:
            lines.append(f"- {item['path']}: {item['reason']}")
    else:
        lines.append("- None")
    lines.extend([
        "",
        "## Missing Skills",
        "",
    ])
    lines.extend(f"- {item}" for item in report["missing_skills"] or ["None"])
    lines.extend(["", "## Missing SKILL.md", ""])
    lines.extend(f"- {item}" for item in report["missing_skill_md"] or ["None"])
    lines.extend(["", "## Missing references/", ""])
    lines.extend(f"- {item}" for item in report["missing_references_dir"] or ["None"])
    lines.extend(["", "## Moved Migration Files", ""])
    if report["migration_files"]["moved"]:
        for item in report["migration_files"]["moved"]:
            lines.append(f"- {item['source']} -> {item['target']}")
    else:
        lines.append("- None")
    lines.extend(["", "## Needs Manual Review", ""])
    if report["migration_files"]["needs_manual_review"]:
        for item in report["migration_files"]["needs_manual_review"]:
            lines.append(f"- {item['path']}: {item['reason']}")
    else:
        lines.append("- None")
    lines.extend(["", "## Checked Skills", ""])
    for item in report["skills"]:
        lines.append(
            f"- {item['skill']}: dir={item['skill_dir_exists']}, "
            f"SKILL.md={item['skill_md_exists']}, references={item['references_dir_exists']}"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def inspect_migration_files(root: Path) -> dict:
    archive_dir = root / "_archive" / "migrated_skill_md"
    archive_dir.mkdir(parents=True, exist_ok=True)
    result = {
        "moved": [],
        "needs_manual_review": [],
        "not_found": [],
    }

    for skill in REQUIRED_SKILLS:
        candidate = root / f"（{skill}）SKILL.md"
        if not candidate.exists():
            result["not_found"].append(str(candidate.relative_to(root)))
            continue

        target_skill_md = root / ".agents" / "skills" / skill / "SKILL.md"
        if not target_skill_md.is_file():
            target_skill_md = root / ".codex" / "skills" / skill / "SKILL.md"
        candidate_bytes = read_bytes(candidate)
        target_bytes = read_bytes(target_skill_md)
        if candidate_bytes is not None and target_bytes is not None and candidate_bytes == target_bytes:
            archive_target = archive_dir / candidate.name
            if archive_target.exists():
                result["needs_manual_review"].append(
                    {
                        "path": str(candidate.relative_to(root)),
                        "reason": f"archive target already exists: {archive_target.relative_to(root)}",
                    }
                )
                continue
            shutil.move(str(candidate), str(archive_target))
            result["moved"].append(
                {
                    "source": str(candidate.relative_to(root)),
                    "target": str(archive_target.relative_to(root)),
                    "reason": "byte-identical to corresponding primary Skill SKILL.md",
                }
            )
        else:
            result["needs_manual_review"].append(
                {
                    "path": str(candidate.relative_to(root)),
                    "reason": "not byte-identical to corresponding Skill SKILL.md or target file missing",
                }
            )

    return result


def main() -> int:
    root = project_root()
    skill_roots = resolve_skill_roots(root)
    skills_root = root / skill_roots["primary_skill_tree"]
    reports_dir = root / "reports"

    skills = []
    missing_skills = []
    missing_skill_md = []
    missing_references_dir = []

    for skill in REQUIRED_SKILLS:
        skill_dir = skills_root / skill
        skill_md = skill_dir / "SKILL.md"
        references_dir = skill_dir / "references"
        item = {
            "skill": skill,
            "skill_dir": str(skill_dir.relative_to(root)),
            "skill_dir_exists": skill_dir.is_dir(),
            "skill_md": str(skill_md.relative_to(root)),
            "skill_md_exists": skill_md.is_file(),
            "references_dir": str(references_dir.relative_to(root)),
            "references_dir_exists": references_dir.is_dir(),
        }
        skills.append(item)
        if not item["skill_dir_exists"]:
            missing_skills.append(skill)
        if not item["skill_md_exists"]:
            missing_skill_md.append(str(skill_md.relative_to(root)))
        if not item["references_dir_exists"]:
            missing_references_dir.append(str(references_dir.relative_to(root)))

    migration_files = inspect_migration_files(root)
    status = "pass"
    if missing_skills or missing_skill_md or missing_references_dir or migration_files["needs_manual_review"]:
        status = "fail"

    report = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "project_root": str(root),
        "skill_roots": skill_roots,
        "skills": skills,
        "missing_skills": missing_skills,
        "missing_skill_md": missing_skill_md,
        "missing_references_dir": missing_references_dir,
        "migration_files": migration_files,
    }
    if skill_roots["needs_manual_review"]:
        report["status"] = "fail"

    write_json(reports_dir / "skill_tree_report.json", report)
    write_markdown(reports_dir / "skill_tree_report.md", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
