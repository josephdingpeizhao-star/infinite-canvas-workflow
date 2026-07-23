"""Build QC-driven single-image repair work orders without generating images."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from executor_contract import ImageGenerationTask
from render_task_assembler import (
    ASPECT_TO_IMAGE_SIZE,
    NEGATIVE_PROMPT_SEPARATOR,
    OUTPUT_TYPE_TO_ASPECT,
    RenderTaskAssemblyError,
    _read_json,
    _reference_image,
    resolve_final_prompt_index_path,
)


ROOT = Path(__file__).resolve().parents[1]
ACTIONABLE_SEVERITIES = {"critical", "major"}
KNOWN_SEVERITIES = ACTIONABLE_SEVERITIES | {"needs_review"}
CONFIG_ID_PATTERN = re.compile(r"(?:main|detail)_[0-9]{2}")


@dataclass(frozen=True)
class RepairTarget:
    target_id: str
    repair_goal: str
    severity: str
    affected_asset: str
    return_stage: str
    issue_id: str


@dataclass(frozen=True)
class RepairWorkOrder:
    config_id: str
    affected_asset: str
    output_type: str
    targets: tuple[RepairTarget, ...]
    actionable_targets: tuple[RepairTarget, ...]
    review_targets: tuple[RepairTarget, ...]
    original_final_prompt: str
    repair_addendum: str
    task: ImageGenerationTask


@dataclass(frozen=True)
class RepairPlan:
    product_id: str
    report_path: Path
    report_sha256: str
    work_orders: tuple[RepairWorkOrder, ...]


@dataclass(frozen=True)
class RepairPreparation:
    report_found: bool
    report_valid: bool
    target_count: int
    actionable_target_count: int
    plan: RepairPlan | None
    error_code: str = ""

    def gate_facts(self, environment: Mapping[str, str]) -> dict[str, Any]:
        return {
            "report_found": self.report_found,
            "report_valid": self.report_valid,
            "target_count": self.target_count,
            "actionable_target_count": self.actionable_target_count,
            "render_enabled": environment.get("RENDER_ALLOW_REAL_EXECUTION") == "1",
            "api_key_configured": bool((environment.get("OPENAI_API_KEY") or "").strip()),
        }


def _first_path(value: Any, label: str) -> Path:
    if isinstance(value, list):
        value = value[0] if value else None
    if not isinstance(value, str) or not value.strip():
        raise RenderTaskAssemblyError(f"{label} 未声明路径")
    return Path(value)


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(parent.resolve(strict=False))
        return True
    except ValueError:
        return False


def _report_candidates(
    manifest: Mapping[str, Any],
    product_id: str,
    repo_reports_dir: Path,
) -> tuple[Path, Path]:
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, Mapping):
        raise RenderTaskAssemblyError("manifest.artifacts 缺失")
    qc_dir = _first_path(artifacts.get("qc_reports"), "artifacts.qc_reports")
    return (
        repo_reports_dir / f"{product_id}_qc_report.json",
        qc_dir / "qc_report.json",
    )


def _read_selected_report(
    manifest: Mapping[str, Any],
    product_id: str,
    repo_reports_dir: Path,
) -> tuple[Path | None, bytes | None, str]:
    try:
        repo_report, workspace_report = _report_candidates(manifest, product_id, repo_reports_dir)
    except RenderTaskAssemblyError:
        return None, None, "qc_report_invalid"
    existing = tuple(path for path in (repo_report, workspace_report) if path.is_file())
    if not existing:
        return None, None, "qc_report_missing"
    try:
        payloads = tuple(path.read_bytes() for path in existing)
    except OSError:
        return None, None, "qc_report_invalid"
    if len(payloads) == 2 and payloads[0] != payloads[1]:
        return existing[0], None, "qc_report_mismatch"
    return existing[0], payloads[0], ""


def _target_rows(report: Any, product_id: str) -> tuple[list[dict[str, Any]] | None, str]:
    if (
        not isinstance(report, dict)
        or report.get("artifact_type") != "qc_report"
        or report.get("product_id") != product_id
        or report.get("adds_new_generation_direction") is not False
        or not isinstance(report.get("issues"), list)
        or not isinstance(report.get("repair_targets"), list)
    ):
        return None, "qc_report_invalid"
    issues: dict[str, dict[str, Any]] = {}
    for issue in report["issues"]:
        if not isinstance(issue, dict):
            return None, "qc_report_invalid"
        issue_id = issue.get("issue_id")
        if not isinstance(issue_id, str) or not issue_id or issue_id in issues:
            return None, "qc_report_invalid"
        issues[issue_id] = issue
    seen_targets: set[str] = set()
    for target in report["repair_targets"]:
        if not isinstance(target, dict):
            return None, "qc_report_invalid"
        required = (
            "target_id",
            "repair_goal",
            "severity",
            "affected_asset",
            "return_stage",
            "issue_id",
        )
        if any(not isinstance(target.get(key), str) or not target[key].strip() for key in required):
            return None, "qc_report_invalid"
        target_id = target["target_id"]
        asset = target["affected_asset"]
        issue = issues.get(target["issue_id"])
        if (
            target_id in seen_targets
            or target["severity"] not in KNOWN_SEVERITIES
            or Path(asset).name != asset
            or not CONFIG_ID_PATTERN.fullmatch(Path(asset).stem)
            or not asset.endswith(".png")
            or issue is None
            or issue.get("affected_asset") != asset
        ):
            return None, "qc_report_invalid"
        seen_targets.add(target_id)
    return report["repair_targets"], ""


def _repair_target(value: Mapping[str, Any]) -> RepairTarget:
    return RepairTarget(
        target_id=str(value["target_id"]),
        repair_goal=str(value["repair_goal"]),
        severity=str(value["severity"]),
        affected_asset=str(value["affected_asset"]),
        return_stage=str(value["return_stage"]),
        issue_id=str(value["issue_id"]),
    )


def _repair_addendum(
    affected_asset: str,
    aspect: str,
    targets: tuple[RepairTarget, ...],
) -> str:
    goals = "\n".join(
        f"{index}. {target.repair_goal}" for index, target in enumerate(targets, start=1)
    )
    return (
        "\n\n--- QC 返修约束增补段 ---\n"
        "本次返修不新增生成方向，只修复以下已经确认的问题。\n"
        f"目标图：{affected_asset}\n"
        "必须同时完成：\n"
        f"{goals}\n"
        "保持不变：产品身份、颜色、结构与原有商品边界；原 final_prompt 和绑定参考图"
        f"确定的绑定角度；原画布比例 {aspect} 与页面任务；未被上述返修目标明确要求改变的"
        "文字、道具、构图和风格。\n"
        "绑定角度以原 final_prompt 与绑定参考图为准，不得沿用当前问题图中与其冲突的错误"
        "姿态。除此之外不得新增结构、卖点、规格、文字、道具或生成方向。"
    )


def _build_plan(
    manifest: Mapping[str, Any],
    report_path: Path,
    report_bytes: bytes,
    target_rows: list[dict[str, Any]],
) -> RepairPlan:
    product_id = str(manifest.get("product_id") or "")
    workspace = manifest.get("workspace")
    outputs = manifest.get("outputs")
    artifacts = manifest.get("artifacts")
    if not isinstance(workspace, Mapping) or not isinstance(outputs, Mapping) or not isinstance(artifacts, Mapping):
        raise RenderTaskAssemblyError("manifest 缺少 workspace、outputs 或 artifacts")
    outputs_root = _first_path(workspace.get("outputs_root"), "workspace.outputs_root")
    repaired_dir = _first_path(outputs.get("repaired"), "outputs.repaired")
    final_dir = _first_path(artifacts.get("final_prompts"), "artifacts.final_prompts")
    if final_dir.suffix.lower() == ".json":
        final_dir = final_dir.parent
    if not _is_within(repaired_dir, outputs_root):
        raise RenderTaskAssemblyError("repaired 目录必须位于 workspace.outputs_root 内")
    if repaired_dir.exists() and not repaired_dir.is_dir():
        raise RenderTaskAssemblyError("repaired 路径不是目录")

    index_path = resolve_final_prompt_index_path(manifest)
    if not _is_within(index_path, final_dir):
        raise RenderTaskAssemblyError("最终提示词索引不在声明的提示词目录内")
    index = _read_json(index_path, "最终提示词索引")
    items = index.get("items")
    if (
        index.get("artifact_type") != "final_prompt_index"
        or index.get("product_id") != product_id
        or index.get("uses_upstream_prompt_files_as_visual_requirements") is not False
        or not isinstance(items, list)
        or index.get("prompt_count") != len(items)
        or not items
    ):
        raise RenderTaskAssemblyError("最终提示词索引契约无效")

    grouped: dict[str, list[RepairTarget]] = {}
    for row in target_rows:
        target = _repair_target(row)
        grouped.setdefault(target.affected_asset, []).append(target)

    work_orders: list[RepairWorkOrder] = []
    matched_assets: set[str] = set()
    for item in items:
        if not isinstance(item, Mapping):
            raise RenderTaskAssemblyError("最终提示词索引项结构无效")
        config_id = str(item.get("config_id") or "")
        affected_asset = f"{config_id}.png"
        if affected_asset not in grouped:
            continue
        output_type = str(item.get("output_type") or "")
        if (
            not CONFIG_ID_PATTERN.fullmatch(config_id)
            or output_type not in OUTPUT_TYPE_TO_ASPECT
            or not config_id.startswith(f"{output_type}_")
        ):
            raise RenderTaskAssemblyError("最终提示词索引输出类型无效")
        prompt_value = item.get("final_prompt_path")
        if not isinstance(prompt_value, str) or not prompt_value:
            raise RenderTaskAssemblyError("最终提示词索引缺少提示词路径")
        prompt_path = Path(prompt_value)
        if prompt_path.suffix.lower() != ".json" or not _is_within(prompt_path, final_dir):
            raise RenderTaskAssemblyError("最终提示词路径越界")
        document = _read_json(prompt_path, f"最终提示词 {config_id}")
        variable = document.get("variable_config")
        if (
            document.get("product_id") != product_id
            or document.get("artifact_type") != "final_prompt"
            or document.get("uses_upstream_prompt_files_as_visual_requirements") is not False
            or not isinstance(variable, Mapping)
            or variable.get("config_id") != config_id
            or variable.get("output_type") != output_type
        ):
            raise RenderTaskAssemblyError("最终提示词与索引不一致")
        positive = document.get("final_prompt")
        negative = document.get("negative_prompt")
        if not isinstance(positive, str) or not positive.strip() or not isinstance(negative, str) or not negative.strip():
            raise RenderTaskAssemblyError("最终提示词正文或 negative_prompt 为空")
        targets = tuple(grouped[affected_asset])
        actionable = tuple(
            target for target in targets if target.severity in ACTIONABLE_SEVERITIES
        )
        review = tuple(target for target in targets if target.severity == "needs_review")
        if not actionable:
            continue
        aspect = OUTPUT_TYPE_TO_ASPECT[output_type]
        addendum = _repair_addendum(affected_asset, aspect, actionable)
        reference = _reference_image(manifest, str(item.get("bound_reference") or ""))
        output_path = repaired_dir / affected_asset
        if not _is_within(output_path, repaired_dir):
            raise RenderTaskAssemblyError("返修输出路径越界")
        if output_path.is_symlink() or (output_path.exists() and not output_path.is_file()):
            raise RenderTaskAssemblyError("返修输出路径已被非普通文件占用")
        work_orders.append(
            RepairWorkOrder(
                config_id=config_id,
                affected_asset=affected_asset,
                output_type=output_type,
                targets=targets,
                actionable_targets=actionable,
                review_targets=review,
                original_final_prompt=positive,
                repair_addendum=addendum,
                task=ImageGenerationTask(
                    prompt=positive + addendum + NEGATIVE_PROMPT_SEPARATOR + negative,
                    output_path=output_path,
                    reference_images=(reference,),
                    size=ASPECT_TO_IMAGE_SIZE[aspect],
                    output_format="png",
                ),
            )
        )
        matched_assets.add(affected_asset)
    if matched_assets != set(grouped):
        raise RenderTaskAssemblyError("QC 返修目标与最终提示词索引不一致")
    return RepairPlan(
        product_id=product_id,
        report_path=report_path,
        report_sha256=hashlib.sha256(report_bytes).hexdigest(),
        work_orders=tuple(work_orders),
    )


def prepare_repair_plan(
    manifest: Mapping[str, Any],
    manifest_path: Path,
    *,
    repo_reports_dir: Path | None = None,
) -> RepairPreparation:
    """Read and validate the report, then build an in-memory repair plan."""

    del manifest_path  # retained in the public API for CLI symmetry and future auditing
    product_id = str(manifest.get("product_id") or "")
    if not product_id:
        return RepairPreparation(False, False, 0, 0, None, "qc_report_invalid")
    report_path, report_bytes, selection_error = _read_selected_report(
        manifest,
        product_id,
        repo_reports_dir or ROOT / "reports",
    )
    if selection_error:
        return RepairPreparation(
            report_path is not None,
            False,
            0,
            0,
            None,
            selection_error,
        )
    assert report_path is not None and report_bytes is not None
    try:
        report = json.loads(report_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return RepairPreparation(True, False, 0, 0, None, "qc_report_invalid")
    target_rows, validation_error = _target_rows(report, product_id)
    if validation_error or target_rows is None:
        return RepairPreparation(True, False, 0, 0, None, validation_error)
    target_count = len(target_rows)
    actionable_count = sum(
        1 for target in target_rows if target["severity"] in ACTIONABLE_SEVERITIES
    )
    if target_count == 0:
        return RepairPreparation(True, True, 0, 0, None, "repair_targets_empty")
    if actionable_count == 0:
        return RepairPreparation(
            True,
            True,
            target_count,
            0,
            None,
            "repair_targets_not_actionable",
        )
    try:
        plan = _build_plan(manifest, report_path, report_bytes, target_rows)
    except (OSError, ValueError, RenderTaskAssemblyError):
        return RepairPreparation(
            True,
            False,
            target_count,
            actionable_count,
            None,
            "repair_plan_invalid",
        )
    if not plan.work_orders:
        return RepairPreparation(
            True,
            True,
            target_count,
            actionable_count,
            None,
            "repair_targets_not_actionable",
        )
    return RepairPreparation(
        True,
        True,
        target_count,
        actionable_count,
        plan,
    )
