from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from final_prompt_integrity_fixtures import (
    FinalPromptBundle,
    build_final_prompt_bundle,
    read_json,
    write_json,
)


@dataclass(frozen=True)
class QcRepairFixture:
    bundle: FinalPromptBundle
    workspace_report: Path
    repo_report: Path
    repo_reports_dir: Path

    @property
    def repaired_dir(self) -> Path:
        return self.bundle.outputs_root / "repaired"


def _repair_targets() -> list[dict[str, str]]:
    definitions = (
        ("main_01.png", 1, "major", "variable_config"),
        ("main_02.png", 6, "critical", "realism"),
        ("main_05.png", 1, "major", "final_prompt"),
        ("detail_01.png", 1, "major", "final_prompt"),
        ("detail_02.png", 1, "major", "final_prompt"),
        ("detail_04.png", 2, "major", "final_prompt"),
        ("detail_05.png", 4, "major", "final_prompt"),
        ("detail_06.png", 2, "major", "final_prompt"),
    )
    targets: list[dict[str, str]] = []
    ordinal = 0
    for asset, count, severity, return_stage in definitions:
        for asset_ordinal in range(1, count + 1):
            ordinal += 1
            current_severity = severity
            current_stage = return_stage
            if asset == "detail_06.png" and asset_ordinal == 1:
                current_severity = "needs_review"
                current_stage = "angle_inventory"
            elif asset == "main_02.png" and asset_ordinal <= 4:
                current_stage = "variable_config" if asset_ordinal <= 2 else "realism"
            targets.append(
                {
                    "target_id": f"repair_{ordinal:03d}",
                    "repair_goal": f"仅修复 {asset} 的 fixture 问题 {asset_ordinal}，其他约束保持不变。",
                    "severity": current_severity,
                    "affected_asset": asset,
                    "return_stage": current_stage,
                    "issue_id": f"issue_{ordinal:03d}",
                }
            )
    return targets


def qc_report(product_id: str = "fixture_product") -> dict[str, Any]:
    targets = _repair_targets()
    return {
        "product_id": product_id,
        "artifact_type": "qc_report",
        "checked_assets": [
            *(f"main_{index:02d}.png" for index in range(1, 7)),
            *(f"detail_{index:02d}.png" for index in range(1, 9)),
        ],
        "results": [],
        "issues": [
            {
                "issue_id": target["issue_id"],
                "affected_asset": target["affected_asset"],
                "category": "fixture",
                "severity": target["severity"],
                "description": f"fixture issue for {target['affected_asset']}",
            }
            for target in targets
        ],
        "repair_targets": targets,
        "adds_new_generation_direction": False,
        "notes": "fixture QC only",
    }


def build_qc_repair_fixture(root: Path) -> QcRepairFixture:
    bundle = build_final_prompt_bundle(root)
    workspace_report = bundle.qc_dir / "qc_report.json"
    repo_reports_dir = root / "repo_reports"
    repo_report = repo_reports_dir / f"{bundle.manifest['product_id']}_qc_report.json"
    report = qc_report(str(bundle.manifest["product_id"]))
    write_json(workspace_report, report)
    write_json(repo_report, report)
    return QcRepairFixture(
        bundle=bundle,
        workspace_report=workspace_report,
        repo_report=repo_report,
        repo_reports_dir=repo_reports_dir,
    )


def rewrite_both_reports(fixture: QcRepairFixture, report: dict[str, Any]) -> None:
    write_json(fixture.workspace_report, report)
    write_json(fixture.repo_report, report)


def read_fixture_report(fixture: QcRepairFixture) -> dict[str, Any]:
    return read_json(fixture.workspace_report)
