from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


RISK_PATTERNS = {
    "multi_instance": ["重复壶身", "重复壶体配件", "重复", "双件", "多件", "多个", "多壶", "多壶体配件"],
    "separated_components": ["分离壶体配件", "分离壶盖", "分离滤网", "分离组件", "分离"],
    "stacked_structure": ["堆叠", "层叠"],
    "structure_auxiliary_only": ["结构辅助", "辅助结构", "辅助观察", "不作为套装件数依据"],
    "low_identity_confidence": ["主体识别风险", "建议重拍"],
}

MAJOR_CATEGORIES = {"multi_instance", "stacked_structure", "low_identity_confidence"}


class ScriptError(Exception):
    pass


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ScriptError(f"file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ScriptError(f"invalid JSON in {path}: {exc}") from exc


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def first_path(value: Any) -> Path | None:
    if isinstance(value, str) and value:
        return Path(value)
    if isinstance(value, list):
        for item in value:
            path = first_path(item)
            if path is not None:
                return path
    return None


def artifact_dir(batch_manifest: dict[str, Any], key: str) -> Path | None:
    artifacts = batch_manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        return None
    return first_path(artifacts.get(key))


def default_job_manifest(batch_manifest: dict[str, Any]) -> Path:
    path = artifact_dir(batch_manifest, "comfyui_jobs")
    if path is None:
        raise ScriptError("batch manifest does not declare artifacts.comfyui_jobs")
    return path / "comfyui_job_manifest.json"


def default_angle_inventory(batch_manifest: dict[str, Any]) -> Path:
    path = artifact_dir(batch_manifest, "angle_inventory")
    if path is None:
        raise ScriptError("batch manifest does not declare artifacts.angle_inventory")
    return path / "angle_inventory.json"


def default_report_path(batch_manifest: dict[str, Any]) -> Path:
    path = artifact_dir(batch_manifest, "qc_reports")
    if path is None:
        raise ScriptError("batch manifest does not declare artifacts.qc_reports")
    return path / "pre_render_reference_gate_report.json"


def text_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(text_value(item) for item in value)
    if isinstance(value, dict):
        return " ".join(text_value(item) for item in value.values())
    return ""


def build_angle_lookup(angle_inventory: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    lookup: dict[str, list[dict[str, Any]]] = {}

    for asset in angle_inventory.get("image_assets", []):
        if not isinstance(asset, dict):
            continue
        file_path = str(asset.get("file_path", ""))
        if not file_path:
            continue
        key = Path(file_path).name.lower()
        lookup.setdefault(key, []).append(
            {
                "source": "image_assets",
                "asset_id": asset.get("asset_id"),
                "file_path": file_path,
                "text": text_value(asset),
                "record": asset,
            }
        )

    for slot in angle_inventory.get("angle_slots", []):
        if not isinstance(slot, dict):
            continue
        file_name = str(slot.get("file_name", ""))
        if not file_name:
            continue
        key = Path(file_name).name.lower()
        lookup.setdefault(key, []).append(
            {
                "source": "angle_slots",
                "asset_id": slot.get("source_asset_id"),
                "file_path": file_name,
                "text": text_value(slot),
                "record": slot,
            }
        )

    return lookup


def find_risk_matches(text: str) -> dict[str, list[str]]:
    matches: dict[str, list[str]] = {}
    for category, terms in RISK_PATTERNS.items():
        found = [term for term in terms if term in text]
        if found:
            matches[category] = found
    return matches


def flatten_matches(matches_by_record: list[dict[str, list[str]]]) -> tuple[list[str], list[str]]:
    categories: list[str] = []
    terms: list[str] = []
    for matches in matches_by_record:
        for category, found_terms in matches.items():
            if category not in categories:
                categories.append(category)
            for term in found_terms:
                if term not in terms:
                    terms.append(term)
    return categories, terms


def issue_severity(categories: list[str]) -> str:
    return "major" if any(category in MAJOR_CATEGORIES for category in categories) else "needs_review"


def inspect_jobs(
    *,
    batch_manifest: dict[str, Any],
    job_manifest: dict[str, Any],
    angle_inventory: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    lookup = build_angle_lookup(angle_inventory)
    issues: list[dict[str, Any]] = []
    manual_review_targets: list[dict[str, Any]] = []
    checked_assets: list[str] = []
    is_single_product = (
        batch_manifest.get("batch_type", "single") == "single"
        and batch_manifest.get("user_declared_set_product") is not True
    )

    for job in job_manifest.get("jobs", []):
        if not isinstance(job, dict):
            continue
        reference = str(job.get("required_product_reference", ""))
        if not reference:
            continue
        checked_assets.append(reference)
        if not is_single_product:
            continue

        key = Path(reference).name.lower()
        evidence_records = lookup.get(key, [])
        matches_by_record = [find_risk_matches(item.get("text", "")) for item in evidence_records]
        matches_by_record = [item for item in matches_by_record if item]
        categories, terms = flatten_matches(matches_by_record)
        if not categories:
            continue

        severity = issue_severity(categories)
        job_id = str(job.get("job_id", "unknown_job"))
        evidence = [
            {
                "source": item.get("source"),
                "asset_id": item.get("asset_id"),
                "file_path": item.get("file_path"),
                "notes": item.get("record", {}).get("notes"),
                "camera_angle": item.get("record", {}).get("camera_angle"),
                "risk_notes": item.get("record", {}).get("risk_notes"),
                "inventory_result": item.get("record", {}).get("inventory_result"),
            }
            for item in evidence_records
        ]
        issues.append(
            {
                "issue_id": f"pre_render_reference_{job_id}",
                "severity": severity,
                "description": (
                    "Single-product render job uses a reference flagged by angle inventory as "
                    "multi-instance, separated-component, stacked, structure-only, or low-identity risk."
                ),
                "affected_asset": reference,
                "job_id": job_id,
                "output_type": job.get("output_type"),
                "risk_categories": categories,
                "matched_terms": terms,
                "angle_inventory_evidence": evidence,
                "required_action": (
                    "Review before rendering. Prefer a clean single-product reference when one exists; "
                    "otherwise keep this reference with the recorded risk and do not infer set quantity."
                ),
            }
        )
        manual_review_targets.append(
            {
                "target_id": job_id,
                "reference": reference,
                "severity": severity,
                "reason": ", ".join(categories),
            }
        )

    return issues, manual_review_targets, sorted(set(checked_assets))


def build_report(
    *,
    batch_manifest_path: Path,
    job_manifest_path: Path,
    angle_inventory_path: Path,
    batch_manifest: dict[str, Any],
    job_manifest: dict[str, Any],
    angle_inventory: dict[str, Any],
) -> dict[str, Any]:
    issues, manual_review_targets, checked_assets = inspect_jobs(
        batch_manifest=batch_manifest,
        job_manifest=job_manifest,
        angle_inventory=angle_inventory,
    )
    job_count = len(job_manifest.get("jobs", [])) if isinstance(job_manifest.get("jobs"), list) else 0
    is_single_product = (
        batch_manifest.get("batch_type", "single") == "single"
        and batch_manifest.get("user_declared_set_product") is not True
    )
    gate_status = "needs_review" if issues else "pass"
    if not is_single_product:
        gate_status = "not_applicable"

    return {
        "product_id": str(batch_manifest.get("product_id") or job_manifest.get("product_id") or ""),
        "artifact_type": "qc_report",
        "status": "needs_review" if issues else "pass",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "batch_manifest": str(batch_manifest_path),
        "job_manifest": str(job_manifest_path),
        "angle_inventory": str(angle_inventory_path),
        "gate_name": "pre_render_reference_gate",
        "gate_scope": "single_product_reference_risk",
        "checked_assets": checked_assets,
        "results": [
            {
                "check_item": "render_entry_count_preserved",
                "status": "pass",
                "notes": f"No render jobs were removed or rewritten; job_count={job_count}.",
            },
            {
                "check_item": "single_product_reference_risk_gate",
                "status": gate_status,
                "notes": f"flagged_reference_job_count={len(issues)}.",
            },
            {
                "check_item": "original_reference_preservation",
                "status": "pass",
                "notes": "This gate only records risk and keeps original render reference paths unchanged.",
            },
            {
                "check_item": "generation_boundary",
                "status": "pass",
                "notes": "No image generation, ComfyUI submission, final prompt rewrite, or repair prompt generation was performed.",
            },
        ],
        "issues": issues,
        "repair_targets": [],
        "manual_review_targets": manual_review_targets,
        "adds_new_generation_direction": False,
        "notes": (
            "Risk references are flagged for downgrade or manual review only. The gate does not delete jobs, "
            "does not reduce delivery count, and does not replace original render references."
        ),
    }


def write_markdown(path: Path, report: dict[str, Any]) -> None:
    lines = [
        "# Pre-Render Reference Gate Report",
        "",
        f"- product_id: {report['product_id']}",
        f"- status: {report['status']}",
        f"- checked_at: {report['checked_at']}",
        f"- checked_asset_count: {len(report['checked_assets'])}",
        f"- issue_count: {len(report['issues'])}",
        "- image_generation_performed: false",
        "- comfyui_execution_performed: false",
        "",
        "## Results",
        "",
    ]
    lines.extend(
        f"- {item['check_item']}: {item['status']} ({item.get('notes', '')})"
        for item in report["results"]
    )
    lines.extend(["", "## Issues", ""])
    if report["issues"]:
        for item in report["issues"]:
            lines.append(
                f"- {item['issue_id']}: {item['severity']} | {item['job_id']} | "
                f"{', '.join(item['risk_categories'])} | {item['affected_asset']}"
            )
    else:
        lines.append("- None")
    lines.extend(["", "## Manual Review Targets", ""])
    if report["manual_review_targets"]:
        for item in report["manual_review_targets"]:
            lines.append(f"- {item['target_id']}: {item['severity']} | {item['reference']}")
    else:
        lines.append("- None")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Flag risky render references before ComfyUI submission.")
    parser.add_argument("--batch-manifest", required=True)
    parser.add_argument("--job-manifest", default=None)
    parser.add_argument("--angle-inventory", default=None)
    parser.add_argument("--output-report", default=None)
    parser.add_argument("--output-markdown", default=None)
    parser.add_argument("--repo-report-dir", default=None)
    parser.add_argument("--repo-report-prefix", default=None)
    args = parser.parse_args()

    batch_manifest_path = Path(args.batch_manifest)
    batch_manifest = load_json(batch_manifest_path)
    job_manifest_path = Path(args.job_manifest) if args.job_manifest else default_job_manifest(batch_manifest)
    angle_inventory_path = Path(args.angle_inventory) if args.angle_inventory else default_angle_inventory(batch_manifest)
    output_report = Path(args.output_report) if args.output_report else default_report_path(batch_manifest)
    output_markdown = Path(args.output_markdown) if args.output_markdown else output_report.with_suffix(".md")

    report = build_report(
        batch_manifest_path=batch_manifest_path,
        job_manifest_path=job_manifest_path,
        angle_inventory_path=angle_inventory_path,
        batch_manifest=batch_manifest,
        job_manifest=load_json(job_manifest_path),
        angle_inventory=load_json(angle_inventory_path),
    )

    write_json(output_report, report)
    write_markdown(output_markdown, report)

    repo_json_path: Path | None = None
    repo_md_path: Path | None = None
    if args.repo_report_dir:
        prefix = args.repo_report_prefix or f"{report['product_id']}_pre_render_reference_gate_report"
        repo_json_path = Path(args.repo_report_dir) / f"{prefix}.json"
        repo_md_path = Path(args.repo_report_dir) / f"{prefix}.md"
        write_json(repo_json_path, report)
        write_markdown(repo_md_path, report)

    summary = {
        "status": report["status"],
        "product_id": report["product_id"],
        "checked_asset_count": len(report["checked_assets"]),
        "issue_count": len(report["issues"]),
        "output_report": str(output_report),
        "output_markdown": str(output_markdown),
        "repo_report": str(repo_json_path) if repo_json_path else None,
        "repo_markdown": str(repo_md_path) if repo_md_path else None,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ScriptError as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False, indent=2))
        raise SystemExit(2)
