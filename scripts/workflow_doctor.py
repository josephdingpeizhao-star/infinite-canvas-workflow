from __future__ import annotations

import argparse
import ctypes
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import detect_current_state


VALIDATION_SCRIPTS = [
    "scripts/validate_workflow_architecture.py",
    "scripts/validate_skill_tree.py",
    "scripts/validate_references.py",
    "scripts/validate_production_readiness.py",
]


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


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


def script_report_name(script: str) -> str | None:
    mapping = {
        "scripts/validate_workflow_architecture.py": "workflow_architecture_report",
        "scripts/validate_skill_tree.py": "skill_tree_report",
        "scripts/validate_references.py": "reference_check_report",
        "scripts/validate_production_readiness.py": "production_readiness_report",
    }
    return mapping.get(script)


def run_script(root: Path, script: str) -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, "-B", str(root / script)],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    report_name = script_report_name(script)
    result = {
        "script": script,
        "exit_code": completed.returncode,
        "report": f"reports/{report_name}.json" if report_name else None,
        "report_status": report_status(root, report_name) if report_name else None,
    }
    if completed.stderr.strip():
        result["stderr_tail"] = completed.stderr.strip()[-2000:]
    return result


def send_to_recycle_bin(path: Path) -> None:
    if sys.platform != "win32":
        raise RuntimeError("Recycle-bin cleanup is only supported on Windows in this workflow.")

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", ctypes.c_void_p),
            ("wFunc", ctypes.c_uint),
            ("pFrom", ctypes.c_wchar_p),
            ("pTo", ctypes.c_wchar_p),
            ("fFlags", ctypes.c_ushort),
            ("fAnyOperationsAborted", ctypes.c_bool),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", ctypes.c_wchar_p),
        ]

    operation = SHFILEOPSTRUCTW()
    operation.wFunc = 3  # FO_DELETE
    operation.pFrom = str(path.resolve()) + "\0\0"
    operation.fFlags = 0x40 | 0x10 | 0x400  # FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_NOERRORUI
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
    if result != 0 or operation.fAnyOperationsAborted:
        raise RuntimeError(f"Failed to move to Recycle Bin: {path} (code={result})")


def apply_startup_cleanup(root: Path, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    actions = []
    for candidate in candidates:
        path = Path(str(candidate["resolved_path"]))
        action = dict(candidate)
        action["status"] = "missing_before_cleanup" if not path.exists() else "pending"
        if path.exists():
            try:
                send_to_recycle_bin(path)
                action["status"] = "moved_to_recycle_bin"
            except Exception as exc:  # noqa: BLE001 - cleanup report must record exact failure.
                action["status"] = "failed"
                action["error"] = str(exc)
        actions.append(action)
    return actions


def main() -> int:
    parser = argparse.ArgumentParser(description="Run repository self-checks and refresh current state reports.")
    parser.add_argument(
        "--apply-startup-cleanup",
        action="store_true",
        help=(
            "Compatibility flag. Startup cleanup is now applied by default before checks unless "
            "--skip-startup-cleanup is supplied."
        ),
    )
    parser.add_argument(
        "--skip-startup-cleanup",
        action="store_true",
        help="Skip the automatic startup Recycle Bin cleanup preflight.",
    )
    parser.add_argument(
        "--abandoned-product-id",
        action="append",
        default=[],
        help="Product ID confirmed by the user as no longer used. Can be repeated.",
    )
    parser.add_argument(
        "--abandoned-path",
        action="append",
        default=[],
        help=(
            "Legacy external workspace path confirmed as no longer used. The path must be "
            "self-contained and prove batch ownership through its own manifests/batch_manifest.json."
        ),
    )
    parser.add_argument(
        "--delete-historical-product-reports",
        action="store_true",
        help="Move product-specific historical reports to the Recycle Bin.",
    )
    args = parser.parse_args()
    if args.apply_startup_cleanup and args.skip_startup_cleanup:
        parser.error("--apply-startup-cleanup and --skip-startup-cleanup cannot be used together")

    root = project_root()
    cleanup_actions: list[dict[str, Any]] = []
    cleanup_candidates: list[dict[str, Any]] = []
    cleanup_selection: dict[str, Any] | None = None
    cleanup_enabled = not args.skip_startup_cleanup
    if cleanup_enabled:
        cleanup_selection = detect_current_state.startup_cleanup_selection(
            root,
            explicit_abandoned_product_ids=args.abandoned_product_id,
            explicit_abandoned_paths=args.abandoned_path,
            delete_historical_product_reports=args.delete_historical_product_reports,
        )
        cleanup_candidates = detect_current_state.startup_cleanup_candidates(
            root,
            abandoned_product_ids=cleanup_selection["abandoned_product_ids"],
            abandoned_paths=cleanup_selection["abandoned_paths"],
            delete_historical_product_reports=args.delete_historical_product_reports,
            historical_report_product_ids=cleanup_selection["historical_report_product_ids"],
        )
        cleanup_actions = apply_startup_cleanup(root, cleanup_candidates)

    validation_runs = [run_script(root, script) for script in VALIDATION_SCRIPTS]
    validation_failed = [item for item in validation_runs if item["exit_code"] != 0 or item.get("report_status") != "pass"]

    report = detect_current_state.build_report(root)
    if isinstance(report.get("startup_hygiene"), dict):
        report["startup_hygiene"]["mode"] = "recycle_bin_cleanup" if cleanup_enabled else report["startup_hygiene"].get("mode")
        report["startup_hygiene"]["cleanup_actions"] = cleanup_actions
        report["startup_hygiene"]["safe_cleanup_candidate_count"] = len(
            [item for item in cleanup_candidates if item.get("resolved_path")]
        )
        if cleanup_selection is not None:
            report["startup_hygiene"]["cleanup_selection"] = cleanup_selection
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

    summary = {
        "status": "pass" if not validation_failed else "fail",
        "current_stage": report.get("current_stage"),
        "current_stage_judgment": report.get("current_stage_judgment"),
        "last_completed_stage": report.get("last_completed_stage"),
        "next_stage": report.get("next_stage"),
        "next_skill": report.get("next_skill"),
        "stage_plan_current_stage": (report.get("stage_plan") or {}).get("current_stage"),
        "validation_failed_count": len(validation_failed),
        "blocked_reasons": report.get("blocked_reasons"),
        "allowed_next_actions": report.get("allowed_next_actions"),
        "forbidden_next_actions": report.get("forbidden_next_actions"),
        "updated_reports": report["workflow_doctor"]["updated_reports"],
        "startup_cleanup": {
            "applied": cleanup_enabled,
            "selection_mode": cleanup_selection.get("mode") if cleanup_selection else None,
            "candidate_count": len(cleanup_candidates),
            "moved_to_recycle_bin_count": len([item for item in cleanup_actions if item.get("status") == "moved_to_recycle_bin"]),
            "failed_count": len([item for item in cleanup_actions if item.get("status") == "failed"]),
            "selected_abandoned_product_ids": cleanup_selection.get("abandoned_product_ids", []) if cleanup_selection else [],
            "selected_abandoned_paths": cleanup_selection.get("abandoned_paths", []) if cleanup_selection else [],
            "selected_historical_report_product_ids": cleanup_selection.get("historical_report_product_ids", []) if cleanup_selection else [],
            "skipped_protected_product_ids": cleanup_selection.get("skipped_protected_product_ids", []) if cleanup_selection else [],
        },
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not validation_failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
