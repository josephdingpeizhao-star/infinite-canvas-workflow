from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import detect_current_state
import workflow_doctor


PROTECTED_SCOPES = [
    "root business rule prompt files",
    "workflow JSON files",
    "scripts/",
    "schemas/",
    "manifests/*.template.json",
    ".agents/skills/",
    ".codex/skills/",
    "_archive/",
]


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def build_cleanup_plan(
    root: Path,
    *,
    abandoned_product_ids: list[str],
    abandoned_paths: list[str],
    include_all_historical_reports: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    selection = detect_current_state.startup_cleanup_selection(
        root,
        explicit_abandoned_product_ids=abandoned_product_ids,
        explicit_abandoned_paths=abandoned_paths,
        delete_historical_product_reports=include_all_historical_reports,
    )
    candidates = detect_current_state.startup_cleanup_candidates(
        root,
        abandoned_product_ids=selection["abandoned_product_ids"],
        abandoned_paths=selection["abandoned_paths"],
        delete_historical_product_reports=include_all_historical_reports,
        historical_report_product_ids=selection["historical_report_product_ids"],
    )
    return selection, candidates


def summarize_actions(actions: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "action_count": len(actions),
        "moved_to_recycle_bin_count": len([item for item in actions if item.get("status") == "moved_to_recycle_bin"]),
        "missing_before_cleanup_count": len([item for item in actions if item.get("status") == "missing_before_cleanup"]),
        "failed_count": len([item for item in actions if item.get("status") == "failed"]),
    }


def refresh_current_state(
    root: Path,
    *,
    selection: dict[str, Any],
    candidates: list[dict[str, Any]],
    actions: list[dict[str, Any]],
    validation_runs: list[dict[str, Any]],
) -> dict[str, Any]:
    validation_failed = [item for item in validation_runs if item["exit_code"] != 0 or item.get("report_status") != "pass"]
    report = detect_current_state.build_report(root)
    if isinstance(report.get("startup_hygiene"), dict):
        report["startup_hygiene"]["mode"] = "independent_recycle_bin_cleanup"
        report["startup_hygiene"]["cleanup_actions"] = actions
        report["startup_hygiene"]["safe_cleanup_candidate_count"] = len(
            [item for item in candidates if item.get("resolved_path")]
        )
        report["startup_hygiene"]["cleanup_selection"] = selection
    report["workflow_doctor"] = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "validation_runs": validation_runs,
        "validation_failed_count": len(validation_failed),
        "updated_reports": [
            "reports/current_state.json",
            "reports/current_state.md",
        ],
    }
    detect_current_state.write_json(root / "reports" / "current_state.json", report)
    detect_current_state.write_markdown(root / "reports" / "current_state.md", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Automatically clean stale product-batch residue through the guarded Recycle Bin workflow."
    )
    parser.add_argument(
        "--preview",
        action="store_true",
        help="Show the cleanup plan without moving files or refreshing current_state reports.",
    )
    parser.add_argument(
        "--abandoned-product-id",
        action="append",
        default=[],
        help="Optional explicit abandoned product ID. Can be repeated. If omitted, stale IDs are auto-detected.",
    )
    parser.add_argument(
        "--abandoned-path",
        action="append",
        default=[],
        help=(
            "Optional explicit legacy external workspace path. The path must prove ownership through "
            "its own manifests/batch_manifest.json."
        ),
    )
    parser.add_argument(
        "--include-all-historical-reports",
        action="store_true",
        help="Also include every product-specific historical report detected under reports/.",
    )
    args = parser.parse_args()

    root = project_root()
    selection, candidates = build_cleanup_plan(
        root,
        abandoned_product_ids=args.abandoned_product_id,
        abandoned_paths=args.abandoned_path,
        include_all_historical_reports=args.include_all_historical_reports,
    )

    actions: list[dict[str, Any]] = []
    validation_runs: list[dict[str, Any]] = []
    refreshed_report: dict[str, Any] | None = None
    if not args.preview:
        actions = workflow_doctor.apply_startup_cleanup(root, candidates)
        validation_runs = [workflow_doctor.run_script(root, script) for script in workflow_doctor.VALIDATION_SCRIPTS]
        refreshed_report = refresh_current_state(
            root,
            selection=selection,
            candidates=candidates,
            actions=actions,
            validation_runs=validation_runs,
        )

    action_summary = summarize_actions(actions)
    summary = {
        "status": "preview" if args.preview else ("pass" if action_summary["failed_count"] == 0 else "needs_review"),
        "applied": not args.preview,
        "delete_mode": "recycle_bin",
        "selection_mode": selection.get("mode"),
        "selected_abandoned_product_ids": selection.get("abandoned_product_ids", []),
        "selected_abandoned_paths": selection.get("abandoned_paths", []),
        "selected_historical_report_product_ids": selection.get("historical_report_product_ids", []),
        "skipped_protected_product_ids": selection.get("skipped_protected_product_ids", []),
        "protected_scopes": PROTECTED_SCOPES,
        "candidate_count": len(candidates),
        "candidate_actions": candidates if args.preview else [],
        **action_summary,
        "validation_failed_count": len(
            [item for item in validation_runs if item["exit_code"] != 0 or item.get("report_status") != "pass"]
        ),
        "current_state_status": refreshed_report.get("status") if refreshed_report else None,
        "updated_reports": ["reports/current_state.json", "reports/current_state.md"] if refreshed_report else [],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if summary["status"] == "needs_review" else 0


if __name__ == "__main__":
    raise SystemExit(main())
