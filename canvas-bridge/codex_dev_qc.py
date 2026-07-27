"""Offline validation and response assembly for the codex-dev QC step."""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from category_recipes import (
    DEFAULT_CATEGORY_KEY,
    CategoryRecipeError,
    load_category_recipe,
    load_manifest_category,
)
from codex_dev_downstream import (
    artifact_file_under_root,
    load_typed_artifact,
    write_json_exclusive,
)
from executor_contract import ExecutorExecutionError


MAIN_CONFIG_IDS = tuple(f"main_{index:02d}" for index in range(1, 7))
DETAIL_CONFIG_IDS = tuple(f"detail_{index:02d}" for index in range(1, 9))
QC_CONFIG_IDS = MAIN_CONFIG_IDS + DETAIL_CONFIG_IDS
COMMON_ASSET_CHECK_ITEMS = (
    "product_identity",
    "product_color",
    "product_angle",
    "page_task",
    "composition",
    "realism",
    "props",
    "text",
    "size_ratio",
    "style_consistency",
    "platform_spec",
    "ai_artifacts",
)
HANDHELD_CHECK_ITEM = "handheld"
SUMMARY_CHECK_ITEMS = (
    "main_set_consistency",
    "detail_module_chain",
    "batch_style_consistency",
    "batch_platform_readiness",
)
QC_STATUSES = frozenset({"pass", "fail", "needs_review", "not_applicable"})
QC_SEVERITIES = frozenset({"critical", "major", "minor", "needs_review"})
QC_RETURN_STAGES = frozenset(
    {
        "product_identity",
        "angle_inventory",
        "style_master",
        "variable_config",
        "realism",
        "props",
        "final_prompt",
        "export_postprocess",
    }
)
ATTACHMENT_BATCH_LIMIT_BYTES = 20 * 1024 * 1024
WHOLE_REQUEST_LIMIT_BYTES = 28 * 1024 * 1024
QC_REPORT_REQUIRED_FIELDS = frozenset(
    {
        "product_id",
        "artifact_type",
        "checked_assets",
        "results",
        "issues",
        "repair_targets",
        "adds_new_generation_direction",
    }
)
QC_REPORT_FORBIDDEN_FIELDS = frozenset(
    {
        "new_generation_direction",
        "creative_direction",
        "generation_prompt",
        "final_prompt",
    }
)
QC_SUPPORTED_REFERENCE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp"})


@dataclass(frozen=True)
class QcRuleDocument:
    name: str
    path: Path
    text: str


@dataclass(frozen=True)
class QcAsset:
    asset_id: str
    config_id: str
    output_type: str
    render_path: Path
    reference_path: Path
    final_prompt_path: Path
    handheld: bool
    width: int
    height: int


@dataclass(frozen=True)
class QcBatch:
    index: int
    assets: tuple[QcAsset, ...]


@dataclass(frozen=True)
class QcPlan:
    product_id: str
    output_path: Path
    assets: tuple[QcAsset, ...]
    batches: tuple[QcBatch, ...]
    rule_documents: tuple[QcRuleDocument, ...]
    documents: Mapping[str, Any]


class QcTransportCorruption(Exception):
    """A response can be retried because transport damage is structurally evident."""


def build_qc_summary_prompt(
    plan: QcPlan,
    chunks: tuple[Mapping[str, Any], ...],
    *,
    repair: bool = False,
) -> str:
    checked_assets = [asset.asset_id for asset in plan.assets]
    if repair:
        return (
            "上一条第 8 批全批总结疑似在传输中截断或出现 U+FFFD。"
            "请重新发送完整 JSON 对象，不要解释、不要 Markdown，业务判断不得改变。"
            f"chunk_index 必须为 8，chunk_count 必须为 8，checked_assets 必须严格为 "
            f"{json.dumps(checked_assets, ensure_ascii=False)}。"
        )
    if len(chunks) != 7:
        raise ExecutorExecutionError("codex-dev 无法构建完整的 QC 全批总结")
    shape = {
        "chunk_index": 8,
        "chunk_count": 8,
        "checked_assets": checked_assets,
        "results": [
            {
                "check_item": SUMMARY_CHECK_ITEMS[0],
                "status": "pass",
                "notes": "简短证据",
            }
        ],
        "issues": [],
        "repair_targets": [],
    }
    return f"""这是单品批次 {plan.product_id} 的第 8/8 批全批总结，不附加图片。
只基于下方已通过结构校验的前 7 批结果复核全批一致性，不得推翻逐图结论，不得新增生成方向。
必须且只返回这些总结检查项，各一次：{json.dumps(SUMMARY_CHECK_ITEMS, ensure_ascii=False)}。
status 只允许 {json.dumps(sorted(QC_STATUSES), ensure_ascii=False)}；severity 只允许 {json.dumps(sorted(QC_SEVERITIES), ensure_ascii=False)}。
只返回一个 JSON 对象，不要 Markdown、代码围栏或解释。顶层只允许 chunk_index、chunk_count、checked_assets、results、issues、repair_targets。
results 每项只允许 check_item、status、notes。issues 与 repair_targets 的字段及枚举沿用前 7 批，affected_asset 必须指向本批 14 张图之一，ID 不得与前 7 批重复。
不得返回 new_generation_direction、creative_direction、generation_prompt、final_prompt 或其他字段。

【前 7 批已校验结果】
{json.dumps(chunks, ensure_ascii=False, sort_keys=True)}

【返回结构示例；只示意字段，不代表实际结论】
{json.dumps(shape, ensure_ascii=False, sort_keys=True)}
"""


def parse_qc_summary_response(
    text: str,
    plan: QcPlan,
    *,
    prior_chunks: tuple[Mapping[str, Any], ...],
) -> dict[str, Any]:
    if len(prior_chunks) != 7:
        raise ExecutorExecutionError("codex-dev 无法校验不完整的 QC 全批总结")
    value = _parse_qc_json_object(text, "QC 全批总结")
    allowed_top = {
        "chunk_index",
        "chunk_count",
        "checked_assets",
        "results",
        "issues",
        "repair_targets",
    }
    expected_assets = [asset.asset_id for asset in plan.assets]
    if set(value) != allowed_top:
        raise ExecutorExecutionError("codex-dev 收到的 QC 全批总结包含未声明字段")
    if (
        value.get("chunk_index") != 8
        or value.get("chunk_count") != 8
        or value.get("checked_assets") != expected_assets
    ):
        raise ExecutorExecutionError("codex-dev 收到的 QC 全批总结身份无效")
    results = value.get("results")
    issues = value.get("issues")
    targets = value.get("repair_targets")
    if not isinstance(results, list) or not isinstance(issues, list) or not isinstance(targets, list):
        raise ExecutorExecutionError("codex-dev 收到的 QC 全批总结列表无效")
    observed_checks: list[str] = []
    for result in results:
        if not isinstance(result, Mapping) or set(result) != {"check_item", "status", "notes"}:
            raise ExecutorExecutionError("codex-dev 收到的 QC 全批检查项字段无效")
        check_item = result.get("check_item")
        if (
            check_item not in SUMMARY_CHECK_ITEMS
            or result.get("status") not in QC_STATUSES
            or not isinstance(result.get("notes"), str)
        ):
            raise ExecutorExecutionError("codex-dev 收到的 QC 全批检查项内容无效")
        observed_checks.append(str(check_item))
    if len(observed_checks) != len(set(observed_checks)) or set(observed_checks) != set(SUMMARY_CHECK_ITEMS):
        raise ExecutorExecutionError("codex-dev 收到的 QC 全批检查项覆盖无效")

    asset_ids = set(expected_assets)
    allowed_categories = set(COMMON_ASSET_CHECK_ITEMS) | {
        HANDHELD_CHECK_ITEM,
        *SUMMARY_CHECK_ITEMS,
    }
    prior_issue_ids = _prior_ids(prior_chunks, "issues", "issue_id")
    prior_target_ids = _prior_ids(prior_chunks, "repair_targets", "target_id")
    local_issue_ids: set[str] = set()
    for issue in issues:
        if not isinstance(issue, Mapping) or set(issue) != {
            "issue_id",
            "severity",
            "description",
            "affected_asset",
            "category",
        }:
            raise ExecutorExecutionError("codex-dev 收到的 QC 全批问题字段无效")
        issue_id = issue.get("issue_id")
        if (
            not isinstance(issue_id, str)
            or not issue_id.strip()
            or issue_id in prior_issue_ids
            or issue_id in local_issue_ids
            or issue.get("severity") not in QC_SEVERITIES
            or not isinstance(issue.get("description"), str)
            or not str(issue.get("description")).strip()
            or issue.get("affected_asset") not in asset_ids
            or issue.get("category") not in allowed_categories
        ):
            raise ExecutorExecutionError("codex-dev 收到的 QC 全批问题内容无效")
        local_issue_ids.add(issue_id)

    local_target_ids: set[str] = set()
    for target in targets:
        if not isinstance(target, Mapping) or set(target) != {
            "target_id",
            "repair_goal",
            "severity",
            "affected_asset",
            "return_stage",
            "issue_id",
        }:
            raise ExecutorExecutionError("codex-dev 收到的 QC 全批返修目标字段无效")
        target_id = target.get("target_id")
        if (
            not isinstance(target_id, str)
            or not target_id.strip()
            or target_id in prior_target_ids
            or target_id in local_target_ids
            or target.get("severity") not in QC_SEVERITIES
            or not isinstance(target.get("repair_goal"), str)
            or not str(target.get("repair_goal")).strip()
            or target.get("affected_asset") not in asset_ids
            or target.get("return_stage") not in QC_RETURN_STAGES
            or target.get("issue_id") not in local_issue_ids
        ):
            raise ExecutorExecutionError("codex-dev 收到的 QC 全批返修目标内容无效")
        local_target_ids.add(target_id)
    return dict(value)


def assemble_qc_report(
    plan: QcPlan,
    chunks: tuple[Mapping[str, Any], ...],
    summary: Mapping[str, Any],
) -> dict[str, Any]:
    if len(chunks) != 7:
        raise ExecutorExecutionError("codex-dev 无法装配不完整的 QC 报告")
    expected_assets = [asset.asset_id for asset in plan.assets]
    for index, (batch, chunk) in enumerate(zip(plan.batches, chunks), start=1):
        if (
            batch.index != index
            or chunk.get("chunk_index") != index
            or chunk.get("chunk_count") != 8
            or chunk.get("checked_assets") != [asset.asset_id for asset in batch.assets]
        ):
            raise ExecutorExecutionError("codex-dev 无法装配身份异常的 QC 报告")
    if (
        summary.get("chunk_index") != 8
        or summary.get("chunk_count") != 8
        or summary.get("checked_assets") != expected_assets
    ):
        raise ExecutorExecutionError("codex-dev 无法装配身份异常的 QC 报告")
    report = {
        "product_id": plan.product_id,
        "artifact_type": "qc_report",
        "checked_assets": expected_assets,
        "results": [
            dict(item)
            for chunk in (*chunks, summary)
            for item in chunk["results"]
        ],
        "issues": [
            dict(item)
            for chunk in (*chunks, summary)
            for item in chunk["issues"]
        ],
        "repair_targets": [
            dict(item)
            for chunk in (*chunks, summary)
            for item in chunk["repair_targets"]
        ],
        "adds_new_generation_direction": False,
        "notes": "QC 仅判断现有 14 张图片并给出返修归因；未新增生成方向。",
    }
    if "\ufffd" in json.dumps(report, ensure_ascii=False):
        raise ExecutorExecutionError("codex-dev 拒绝写入包含损坏字符的 QC 报告")
    _validate_qc_report_contract(plan, report)
    return report


def write_qc_report_exclusive(plan: QcPlan, report: Mapping[str, Any]) -> Path:
    _validate_qc_report_contract(plan, report)
    if "\ufffd" in json.dumps(report, ensure_ascii=False):
        raise ExecutorExecutionError("codex-dev 拒绝写入包含损坏字符的 QC 报告")
    try:
        return write_json_exclusive(plan.output_path, report, "QC 报告")
    except ExecutorExecutionError:
        if plan.output_path.exists():
            raise ExecutorExecutionError("正式 QC 报告已存在，codex-dev 不会覆盖") from None
        raise


def _validate_qc_report_contract(plan: QcPlan, report: Mapping[str, Any]) -> None:
    allowed_fields = QC_REPORT_REQUIRED_FIELDS | {"notes"}
    if not isinstance(report, Mapping) or not QC_REPORT_REQUIRED_FIELDS.issubset(report):
        raise ExecutorExecutionError("codex-dev 无法写入 schema 不完整的 QC 报告")
    if not set(report).issubset(allowed_fields) or QC_REPORT_FORBIDDEN_FIELDS.intersection(report):
        raise ExecutorExecutionError("codex-dev 无法写入 schema 越界的 QC 报告")
    if (
        report.get("product_id") != plan.product_id
        or report.get("artifact_type") != "qc_report"
        or report.get("checked_assets") != [asset.asset_id for asset in plan.assets]
        or report.get("adds_new_generation_direction") is not False
    ):
        raise ExecutorExecutionError("codex-dev 无法写入身份异常的 QC 报告")
    results = report.get("results")
    issues = report.get("issues")
    targets = report.get("repair_targets")
    if not isinstance(results, list) or not isinstance(issues, list) or not isinstance(targets, list):
        raise ExecutorExecutionError("codex-dev 无法写入 schema 异常的 QC 报告")
    for result in results:
        if (
            not isinstance(result, Mapping)
            or not isinstance(result.get("check_item"), str)
            or result.get("status") not in QC_STATUSES
            or ("notes" in result and not isinstance(result.get("notes"), str))
        ):
            raise ExecutorExecutionError("codex-dev 无法写入 schema 异常的 QC 报告")
    for issue in issues:
        if (
            not isinstance(issue, Mapping)
            or not isinstance(issue.get("issue_id"), str)
            or issue.get("severity") not in QC_SEVERITIES
            or not isinstance(issue.get("description"), str)
            or ("affected_asset" in issue and issue.get("affected_asset") not in report["checked_assets"])
        ):
            raise ExecutorExecutionError("codex-dev 无法写入 schema 异常的 QC 报告")
    for target in targets:
        if (
            not isinstance(target, Mapping)
            or not isinstance(target.get("target_id"), str)
            or not isinstance(target.get("repair_goal"), str)
            or ("severity" in target and target.get("severity") not in QC_SEVERITIES)
        ):
            raise ExecutorExecutionError("codex-dev 无法写入 schema 异常的 QC 报告")


def _load_qc_report_schema(repository_root: Path) -> dict[str, Any]:
    schema_path = repository_root.resolve() / "schemas" / "qc_report.schema.json"
    schema = _read_json(schema_path, "QC 报告 schema")
    properties = schema.get("properties")
    try:
        status_enum = set(properties["results"]["items"]["properties"]["status"]["enum"])
        issue_severity_enum = set(properties["issues"]["items"]["properties"]["severity"]["enum"])
        target_severity_enum = set(
            properties["repair_targets"]["items"]["properties"]["severity"]["enum"]
        )
        forbidden_fields = {
            item["required"][0] for item in schema["not"]["anyOf"]
        }
    except (KeyError, TypeError, IndexError):
        raise ExecutorExecutionError("codex-dev QC 报告 schema 合同不匹配") from None
    if (
        schema.get("$id") != "qc_report.schema.json"
        or schema.get("type") != "object"
        or set(schema.get("required") or ()) != QC_REPORT_REQUIRED_FIELDS
        or not isinstance(properties, Mapping)
        or properties.get("artifact_type", {}).get("const") != "qc_report"
        or properties.get("adds_new_generation_direction", {}).get("const") is not False
        or status_enum != QC_STATUSES
        or issue_severity_enum != QC_SEVERITIES
        or target_severity_enum != QC_SEVERITIES
        or forbidden_fields != QC_REPORT_FORBIDDEN_FIELDS
    ):
        raise ExecutorExecutionError("codex-dev QC 报告 schema 合同不匹配")
    return schema


def parse_qc_batch_response(
    text: str,
    batch: QcBatch,
    *,
    prior_chunks: tuple[Mapping[str, Any], ...] = (),
) -> dict[str, Any]:
    """Parse and validate one two-asset QC response."""

    value = _parse_qc_json_object(text, "QC 批次")
    allowed_top = {
        "chunk_index",
        "chunk_count",
        "checked_assets",
        "results",
        "issues",
        "repair_targets",
    }
    if set(value) != allowed_top:
        raise ExecutorExecutionError("codex-dev 收到的 QC 批次包含未声明字段")
    expected_assets = [asset.asset_id for asset in batch.assets]
    if (
        value.get("chunk_index") != batch.index
        or value.get("chunk_count") != 8
        or value.get("checked_assets") != expected_assets
    ):
        raise ExecutorExecutionError("codex-dev 收到的 QC 批次身份无效")

    results = value.get("results")
    issues = value.get("issues")
    repair_targets = value.get("repair_targets")
    if not isinstance(results, list) or not isinstance(issues, list) or not isinstance(repair_targets, list):
        raise ExecutorExecutionError("codex-dev 收到的 QC 批次列表无效")

    expected_checks: dict[str, set[str]] = {}
    for asset in batch.assets:
        checks = set(COMMON_ASSET_CHECK_ITEMS)
        if asset.handheld:
            checks.add(HANDHELD_CHECK_ITEM)
        expected_checks[asset.asset_id] = checks
    observed_checks: dict[str, list[str]] = {asset_id: [] for asset_id in expected_assets}
    for result in results:
        if not isinstance(result, Mapping) or set(result) != {
            "affected_asset",
            "check_item",
            "status",
            "notes",
        }:
            raise ExecutorExecutionError("codex-dev 收到的 QC 检查项字段无效")
        asset_id = result.get("affected_asset")
        check_item = result.get("check_item")
        status = result.get("status")
        notes = result.get("notes")
        if (
            not isinstance(asset_id, str)
            or asset_id not in expected_checks
            or not isinstance(check_item, str)
            or check_item not in expected_checks[asset_id]
            or status not in QC_STATUSES
            or not isinstance(notes, str)
        ):
            raise ExecutorExecutionError("codex-dev 收到的 QC 检查项内容无效")
        observed_checks[asset_id].append(check_item)
    for asset_id, expected in expected_checks.items():
        observed = observed_checks[asset_id]
        if len(observed) != len(set(observed)) or set(observed) != expected:
            raise ExecutorExecutionError("codex-dev 收到的 QC 检查项覆盖无效")

    prior_issue_ids = _prior_ids(prior_chunks, "issues", "issue_id")
    prior_target_ids = _prior_ids(prior_chunks, "repair_targets", "target_id")
    local_issue_ids: set[str] = set()
    for issue in issues:
        if not isinstance(issue, Mapping) or set(issue) != {
            "issue_id",
            "severity",
            "description",
            "affected_asset",
            "category",
        }:
            raise ExecutorExecutionError("codex-dev 收到的 QC 问题字段无效")
        issue_id = issue.get("issue_id")
        asset_id = issue.get("affected_asset")
        category = issue.get("category")
        if (
            not isinstance(issue_id, str)
            or not issue_id.strip()
            or issue_id in prior_issue_ids
            or issue_id in local_issue_ids
            or issue.get("severity") not in QC_SEVERITIES
            or not isinstance(issue.get("description"), str)
            or not str(issue.get("description")).strip()
            or asset_id not in expected_checks
            or category not in expected_checks[str(asset_id)]
        ):
            raise ExecutorExecutionError("codex-dev 收到的 QC 问题内容无效")
        local_issue_ids.add(issue_id)

    local_target_ids: set[str] = set()
    for target in repair_targets:
        if not isinstance(target, Mapping) or set(target) != {
            "target_id",
            "repair_goal",
            "severity",
            "affected_asset",
            "return_stage",
            "issue_id",
        }:
            raise ExecutorExecutionError("codex-dev 收到的 QC 返修目标字段无效")
        target_id = target.get("target_id")
        if (
            not isinstance(target_id, str)
            or not target_id.strip()
            or target_id in prior_target_ids
            or target_id in local_target_ids
            or target.get("severity") not in QC_SEVERITIES
            or not isinstance(target.get("repair_goal"), str)
            or not str(target.get("repair_goal")).strip()
            or target.get("affected_asset") not in expected_checks
            or target.get("return_stage") not in QC_RETURN_STAGES
            or target.get("issue_id") not in local_issue_ids
        ):
            raise ExecutorExecutionError("codex-dev 收到的 QC 返修目标内容无效")
        local_target_ids.add(target_id)
    return dict(value)


def _prior_ids(
    chunks: tuple[Mapping[str, Any], ...], list_key: str, id_key: str
) -> set[str]:
    identifiers: set[str] = set()
    for chunk in chunks:
        items = chunk.get(list_key)
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, Mapping) and isinstance(item.get(id_key), str):
                identifiers.add(str(item[id_key]))
    return identifiers


def _parse_qc_json_object(text: str, label: str) -> dict[str, Any]:
    candidate = text.strip()
    if "\ufffd" in candidate:
        raise QcTransportCorruption
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError:
        if _is_probable_truncated_json(candidate):
            raise QcTransportCorruption from None
        raise ExecutorExecutionError(f"codex-dev 收到的{label}不是有效 JSON") from None
    if not isinstance(value, dict):
        raise ExecutorExecutionError(f"codex-dev 收到的{label}根对象无效")
    return value


def _is_probable_truncated_json(candidate: str) -> bool:
    if not candidate.startswith("{"):
        return False
    depth = 0
    in_string = False
    escaped = False
    for character in candidate:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
        elif character in "]}":
            depth -= 1
            if depth < 0:
                return False
    return depth > 0 or in_string or escaped


def qc_batch_attachment_paths(batch: QcBatch) -> tuple[Path, ...]:
    """Return render/reference pairs in the attachment order declared to the model."""

    return tuple(path for asset in batch.assets for path in (asset.render_path, asset.reference_path))


def build_qc_batch_prompt(plan: QcPlan, batch: QcBatch, *, repair: bool = False) -> str:
    """Build the JSON-only instruction for one two-asset QC batch."""

    checked_assets = [asset.asset_id for asset in batch.assets]
    if repair:
        return (
            f"上一条第 {batch.index} 批返回疑似在传输中截断或出现 U+FFFD。"
            "请在同一线程中重新发送完整 JSON 对象，不要解释、不要 Markdown，业务判断不得改变。"
            f"chunk_index 必须为 {batch.index}，chunk_count 必须为 8，"
            f"checked_assets 必须严格为 {json.dumps(checked_assets, ensure_ascii=False)}。"
        )

    asset_context = []
    handheld_by_asset: dict[str, bool] = {}
    for offset, asset in enumerate(batch.assets):
        asset_context.append(
            {
                "asset_id": asset.asset_id,
                "output_type": asset.output_type,
                "render_attachment": offset * 2 + 1,
                "bound_reference_attachment": offset * 2 + 2,
                "bound_reference": asset.reference_path.name,
                "canvas": {"width": asset.width, "height": asset.height},
                "handheld_check_required": asset.handheld,
                "variable_config": plan.documents[
                    f"{asset.output_type}_variable_configs"
                ]["configs"][int(asset.config_id.split("_")[1]) - 1],
                "final_prompt": plan.documents["final_prompts"][asset.config_id],
            }
        )
        handheld_by_asset[asset.asset_id] = asset.handheld

    result_shape = {
        "chunk_index": batch.index,
        "chunk_count": 8,
        "checked_assets": checked_assets,
        "results": [
            {
                "affected_asset": checked_assets[0],
                "check_item": COMMON_ASSET_CHECK_ITEMS[0],
                "status": "pass",
                "notes": "简短证据",
            }
        ],
        "issues": [
            {
                "issue_id": "issue_001",
                "severity": "major",
                "description": "问题描述",
                "affected_asset": checked_assets[0],
                "category": COMMON_ASSET_CHECK_ITEMS[0],
            }
        ],
        "repair_targets": [
            {
                "target_id": "repair_001",
                "repair_goal": "仅修复已识别问题",
                "severity": "major",
                "affected_asset": checked_assets[0],
                "return_stage": "variable_config",
                "issue_id": "issue_001",
            }
        ],
    }
    rules = "\n\n".join(
        f"【{document.name}】\n{document.text}" for document in plan.rule_documents
    )
    return f"""你正在对单品批次 {plan.product_id} 的第 {batch.index}/8 批执行只读 QC 判断。
本批只检查 {json.dumps(checked_assets, ensure_ascii=False)}；附件顺序与下方 asset_context 一一对应。
只判断现有图片是否满足正式上游约束，不生成图片、不修改图片、不改写最终提示词，不得新增生成方向。
不得虚构尺寸、容量、重量、材质、认证、品牌或型号；无法由图片与正式产物确认时使用 needs_review。
必须对每张图逐项返回这些固定类别且各一次：{json.dumps(COMMON_ASSET_CHECK_ITEMS, ensure_ascii=False)}。
handheld 仅对 handheld_check_required=true 的图片返回且必须返回一次；其他图片禁止返回 handheld 项。
status 只允许 {json.dumps(sorted(QC_STATUSES), ensure_ascii=False)}；severity 只允许 {json.dumps(sorted(QC_SEVERITIES), ensure_ascii=False)}。
issue_id、target_id 必须在整条线程中唯一；每个 repair_target.issue_id 必须引用本批 issue_id。
return_stage 只允许 {json.dumps(sorted(QC_RETURN_STAGES), ensure_ascii=False)}。
只返回一个 JSON 对象，不要 Markdown、代码围栏或解释。顶层只允许 chunk_index、chunk_count、checked_assets、results、issues、repair_targets。
results 每项只允许 affected_asset、check_item、status、notes；issues 每项只允许 issue_id、severity、description、affected_asset、category；repair_targets 每项只允许 target_id、repair_goal、severity、affected_asset、return_stage、issue_id。
若没有问题，issues 与 repair_targets 返回空数组。不得返回 new_generation_direction、creative_direction、generation_prompt、final_prompt 或其他字段。

【本批手持适用表】
{json.dumps(handheld_by_asset, ensure_ascii=False, sort_keys=True)}

【本批附件与正式约束】
{json.dumps(asset_context, ensure_ascii=False, sort_keys=True)}

【产品身份档案】
{json.dumps(plan.documents['product_identity_archive'], ensure_ascii=False, sort_keys=True)}

【风格母版】
{json.dumps(plan.documents['style_master'], ensure_ascii=False, sort_keys=True)}

【角度槽位入库表】
{json.dumps(plan.documents['angle_inventory'], ensure_ascii=False, sort_keys=True)}

【正式最终提示词索引】
{json.dumps(plan.documents['final_prompt_index'], ensure_ascii=False, sort_keys=True)}

【返回结构示例；只示意字段，不代表实际结论】
{json.dumps(result_shape, ensure_ascii=False, sort_keys=True)}

【QC Skill 与规则原文】
{rules}
"""


def _estimated_attachment_payload_bytes(path: Path) -> int:
    try:
        raw_size = path.stat().st_size
    except OSError:
        raise ExecutorExecutionError("codex-dev 无法读取 QC 附件大小") from None
    encoded_size = ((raw_size + 2) // 3) * 4
    return encoded_size + len(path.name.encode("utf-8")) + 128


def _validate_qc_batch_limits(plan: QcPlan) -> None:
    for batch in plan.batches:
        attachment_bytes = sum(
            _estimated_attachment_payload_bytes(path)
            for path in qc_batch_attachment_paths(batch)
        )
        if attachment_bytes > ATTACHMENT_BATCH_LIMIT_BYTES:
            raise ExecutorExecutionError("codex-dev QC 附件大小超过单批限制")
        prompt_bytes = len(build_qc_batch_prompt(plan, batch).encode("utf-8"))
        if attachment_bytes + prompt_bytes > WHOLE_REQUEST_LIMIT_BYTES:
            raise ExecutorExecutionError("codex-dev QC 请求大小超过整体限制")


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ExecutorExecutionError(f"codex-dev 无法读取有效的{label}") from None
    if not isinstance(value, dict):
        raise ExecutorExecutionError(f"codex-dev 无法读取有效的{label}")
    return value


def _workspace_root(manifest: Mapping[str, Any], key: str) -> Path:
    workspace = manifest.get("workspace")
    value = workspace.get(key) if isinstance(workspace, Mapping) else None
    if not isinstance(value, str) or not value.strip():
        raise ExecutorExecutionError(f"codex-dev 无法验证 manifest.workspace.{key}")
    try:
        return Path(value).resolve()
    except (OSError, RuntimeError, ValueError):
        raise ExecutorExecutionError(f"codex-dev 无法验证 manifest.workspace.{key}") from None


def _declared_directory(manifest: Mapping[str, Any], section: str, key: str, root: Path) -> Path:
    values = manifest.get(section)
    value = values.get(key) if isinstance(values, Mapping) else None
    if isinstance(value, list):
        declared = [item for item in value if isinstance(item, str) and item.strip()]
        if len(declared) != 1:
            raise ExecutorExecutionError(f"codex-dev 无法读取 {key} 位置")
        value = declared[0]
    if not isinstance(value, str) or not value.strip():
        raise ExecutorExecutionError(f"codex-dev 无法读取 {key} 位置")
    try:
        resolved = Path(value).resolve()
        if not resolved.is_relative_to(root):
            raise ExecutorExecutionError(f"{key} 位置不在声明的工作区根目录内")
        return resolved
    except ExecutorExecutionError:
        raise
    except (OSError, RuntimeError, ValueError):
        raise ExecutorExecutionError(f"codex-dev 无法验证 {key} 位置") from None


def _load_variable_configs(
    manifest: Mapping[str, Any],
    *,
    mode: str,
    product_id: str,
) -> tuple[dict[str, Any], dict[str, Mapping[str, Any]]]:
    expected_ids = MAIN_CONFIG_IDS if mode == "main" else DETAIL_CONFIG_IDS
    document, _path = load_typed_artifact(
        manifest,
        f"{mode}_variable_configs",
        f"{mode}_variable_configs.json",
        f"{mode}_variable_config",
        f"正式{'主图' if mode == 'main' else '详情图'}变量配置",
    )
    configs = document.get("configs")
    if document.get("config_count") != len(expected_ids) or not isinstance(configs, list):
        raise ExecutorExecutionError("codex-dev 检测到正式变量配置结构异常")
    by_id: dict[str, Mapping[str, Any]] = {}
    for item in configs:
        if not isinstance(item, Mapping):
            raise ExecutorExecutionError("codex-dev 检测到正式变量配置结构异常")
        config_id = item.get("config_id")
        if (
            not isinstance(config_id, str)
            or item.get("output_type") != mode
            or config_id in by_id
        ):
            raise ExecutorExecutionError("codex-dev 检测到正式变量配置结构异常")
        by_id[config_id] = item
    if tuple(by_id) != expected_ids or set(by_id) != set(expected_ids):
        raise ExecutorExecutionError("codex-dev 检测到正式变量配置覆盖异常")
    return document, by_id


def _handheld(config: Mapping[str, Any]) -> bool:
    overrides = config.get("per_image_overrides")
    declaration = overrides.get("手持交互声明") if isinstance(overrides, Mapping) else None
    if not isinstance(declaration, str) or not declaration.strip():
        raise ExecutorExecutionError("codex-dev 检测到手持交互声明缺失")
    if declaration.strip() == "本张图不启用手持场景":
        return False
    if "启用手持场景" not in declaration:
        raise ExecutorExecutionError("codex-dev 检测到手持交互声明无效")
    return True


def _png_size(path: Path) -> tuple[int, int]:
    try:
        header = path.read_bytes()[:24]
    except OSError:
        raise ExecutorExecutionError("codex-dev 无法读取有效的 QC 渲染图") from None
    if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise ExecutorExecutionError("codex-dev 无法读取有效的 QC 渲染图")
    width, height = struct.unpack(">II", header[16:24])
    if width <= 0 or height <= 0:
        raise ExecutorExecutionError("codex-dev 无法读取有效的 QC 渲染图")
    return width, height


def _validate_reference_image(path: Path) -> None:
    suffix = path.suffix.lower()
    if suffix not in QC_SUPPORTED_REFERENCE_SUFFIXES:
        raise ExecutorExecutionError("codex-dev 检测到 QC 附件格式无效")
    try:
        header = path.read_bytes()[:12]
    except OSError:
        raise ExecutorExecutionError("codex-dev 无法读取 QC 附件") from None
    valid = (
        (suffix in {".jpg", ".jpeg"} and header.startswith(b"\xff\xd8\xff"))
        or (suffix == ".png" and header.startswith(b"\x89PNG\r\n\x1a\n"))
        or (suffix == ".webp" and len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP")
    )
    if not valid:
        raise ExecutorExecutionError("codex-dev 检测到 QC 附件格式无效")


def _load_rule_documents(
    repository_root: Path,
    category_key: str = DEFAULT_CATEGORY_KEY,
) -> tuple[QcRuleDocument, ...]:
    skill_root = repository_root.resolve() / ".agents" / "skills" / "qc-inspector"
    try:
        recipe = load_category_recipe(repository_root, category_key)
    except CategoryRecipeError:
        raise ExecutorExecutionError("codex-dev 无法读取完整的 QC 规则") from None
    sources = (
        ("SKILL.md", skill_root / "SKILL.md", None),
        (
            "qc-inspector.runtime_rule_slices.json",
            None,
            json.dumps(
                recipe.runtime_packages["qc_runtime"],
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        ),
        ("电商图片通用质检清单.txt", None, recipe.qc_documents["qc_checklist"]),
        ("工作流总控规则.txt", None, recipe.qc_documents["qc_workflow"]),
        ("真实感约束.txt", None, recipe.qc_documents["qc_realism"]),
    )
    documents: list[QcRuleDocument] = []
    for name, path, recipe_text in sources:
        if path is not None:
            try:
                resolved = path.resolve()
                if not resolved.is_relative_to(repository_root.resolve()):
                    raise OSError
                text = resolved.read_text(encoding="utf-8")
            except (OSError, UnicodeError, RuntimeError, ValueError):
                raise ExecutorExecutionError("codex-dev 无法读取完整的 QC 规则") from None
        else:
            resolved = repository_root.resolve() / "categories" / category_key
            text = str(recipe_text)
        if not text.strip():
            raise ExecutorExecutionError("codex-dev 无法读取完整的 QC 规则")
        if name == "qc-inspector.runtime_rule_slices.json":
            try:
                runtime = json.loads(text)
            except json.JSONDecodeError:
                raise ExecutorExecutionError("codex-dev 无法读取完整的 QC 规则") from None
            slices = runtime.get("slices") if isinstance(runtime, Mapping) else None
            if (
                not isinstance(runtime, Mapping)
                or runtime.get("artifact_type") != "runtime_rule_slice_package"
                or runtime.get("skill") != "qc-inspector"
                or not isinstance(slices, list)
                or not slices
                or any(
                    not isinstance(item, Mapping)
                    or not isinstance(item.get("text"), str)
                    or not str(item.get("text")).strip()
                    for item in slices
                )
            ):
                raise ExecutorExecutionError("codex-dev 无法读取完整的 QC 规则")
        documents.append(QcRuleDocument(name=name, path=resolved, text=text))
    return tuple(documents)


def load_qc_plan(manifest: Mapping[str, Any], repository_root: Path) -> QcPlan:
    """Load and validate the inputs required for one QC execution."""

    product_id = str(manifest.get("product_id") or "").strip()
    if not product_id:
        raise ExecutorExecutionError("codex-dev 无法执行 QC：manifest.product_id 缺失")
    if manifest.get("batch_type") != "single":
        raise ExecutorExecutionError("codex-dev QC 当前只支持单品批次")

    try:
        category_recipe = load_manifest_category(repository_root, manifest)
    except CategoryRecipeError as exc:
        raise ExecutorExecutionError(f"codex-dev 无法加载产品品类配方：{exc}") from None
    schema = _load_qc_report_schema(repository_root)

    inputs_root = _workspace_root(manifest, "inputs_root")
    outputs_root = _workspace_root(manifest, "outputs_root")
    white_bg_dir = _declared_directory(manifest, "inputs", "white_bg_images", inputs_root)
    renders_dir = (outputs_root / "renders").resolve()
    if not renders_dir.is_relative_to(outputs_root):
        raise ExecutorExecutionError("QC 渲染图位置不在 manifest.workspace.outputs_root 内")
    expected_render_paths = {
        (renders_dir / f"{config_id}.png").resolve() for config_id in QC_CONFIG_IDS
    }
    try:
        actual_render_paths = {entry.resolve() for entry in renders_dir.iterdir()}
    except OSError:
        raise ExecutorExecutionError("codex-dev 检测到 QC 渲染图集合异常") from None
    if actual_render_paths != expected_render_paths or any(
        not path.is_file() or path.suffix.lower() != ".png"
        for path in actual_render_paths
    ):
        raise ExecutorExecutionError("codex-dev 检测到 QC 渲染图集合异常")

    output_path = artifact_file_under_root(manifest, "qc_reports", "qc_report.json")
    if output_path.exists():
        raise ExecutorExecutionError("正式 QC 报告已存在，codex-dev 不会覆盖")

    identity, _identity_path = load_typed_artifact(
        manifest,
        "product_identity_archive",
        "product_identity_archive.json",
        "product_identity_archive",
        "产品身份档案",
    )
    style_master, _style_path = load_typed_artifact(
        manifest,
        "style_master",
        "style_master.json",
        "style_master",
        "风格母版",
    )
    angle_inventory, _angle_path = load_typed_artifact(
        manifest,
        "angle_inventory",
        "angle_inventory.json",
        "angle_inventory",
        "角度槽位入库表",
    )
    main_document, main_configs = _load_variable_configs(
        manifest, mode="main", product_id=product_id
    )
    detail_document, detail_configs = _load_variable_configs(
        manifest, mode="detail", product_id=product_id
    )
    final_index, _index_path = load_typed_artifact(
        manifest,
        "final_prompts",
        "final_prompt_index.json",
        "final_prompt_index",
        "最终提示词索引",
    )
    items = final_index.get("items")
    if final_index.get("prompt_count") != len(QC_CONFIG_IDS) or not isinstance(items, list):
        raise ExecutorExecutionError("codex-dev 检测到最终提示词索引结构异常")

    configs = {**main_configs, **detail_configs}
    assets: list[QcAsset] = []
    final_prompt_documents: dict[str, dict[str, Any]] = {}
    observed_ids: list[str] = []
    for item in items:
        if not isinstance(item, Mapping):
            raise ExecutorExecutionError("codex-dev 检测到最终提示词索引结构异常")
        config_id = item.get("config_id")
        output_type = item.get("output_type")
        if not isinstance(config_id, str) or output_type not in {"main", "detail"}:
            raise ExecutorExecutionError("codex-dev 检测到最终提示词索引结构异常")
        observed_ids.append(config_id)
        expected_type = "main" if config_id in MAIN_CONFIG_IDS else "detail"
        if config_id not in configs or output_type != expected_type:
            raise ExecutorExecutionError("codex-dev 检测到最终提示词绑定关系异常")

        expected_prompt_path = artifact_file_under_root(
            manifest, "final_prompts", f"{config_id}_final_prompt.json"
        )
        declared_prompt_path = item.get("final_prompt_path")
        try:
            if not isinstance(declared_prompt_path, str) or Path(declared_prompt_path).resolve() != expected_prompt_path:
                raise ExecutorExecutionError("codex-dev 检测到最终提示词路径异常")
        except (OSError, RuntimeError, ValueError):
            raise ExecutorExecutionError("codex-dev 检测到最终提示词路径异常") from None
        prompt_document = _read_json(expected_prompt_path, "最终提示词")
        prompt_binding = prompt_document.get("variable_config")
        prompt_config_id = (
            prompt_binding.get("config_id")
            if isinstance(prompt_binding, Mapping)
            else prompt_document.get("config_id")
        )
        prompt_output_type = (
            prompt_binding.get("output_type")
            if isinstance(prompt_binding, Mapping)
            else prompt_document.get("output_type")
        )
        if (
            prompt_document.get("product_id") != product_id
            or prompt_document.get("artifact_type") != "final_prompt"
            or prompt_config_id != config_id
            or prompt_output_type != output_type
            or not isinstance(prompt_document.get("final_prompt"), str)
            or not str(prompt_document.get("final_prompt")).strip()
        ):
            raise ExecutorExecutionError("codex-dev 检测到最终提示词与当前商品不匹配")

        reference_name = item.get("bound_reference")
        if not isinstance(reference_name, str) or not reference_name.strip() or Path(reference_name).name != reference_name:
            raise ExecutorExecutionError("codex-dev 检测到绑定白底参考图异常")
        reference_path = (white_bg_dir / reference_name).resolve()
        if not reference_path.is_relative_to(white_bg_dir) or not reference_path.is_file():
            raise ExecutorExecutionError("codex-dev 检测到绑定白底参考图异常")
        _validate_reference_image(reference_path)
        if prompt_document.get("bound_reference") not in (None, reference_name):
            raise ExecutorExecutionError("codex-dev 检测到最终提示词绑定关系异常")

        render_path = (renders_dir / f"{config_id}.png").resolve()
        if not render_path.is_relative_to(renders_dir) or not render_path.is_file():
            raise ExecutorExecutionError("codex-dev 检测到 QC 渲染图缺失")
        width, height = _png_size(render_path)
        if (output_type == "main" and width != height) or (
            output_type == "detail" and width * 4 != height * 3
        ):
            raise ExecutorExecutionError("codex-dev 检测到 QC 渲染图画布比例异常")

        final_prompt_documents[config_id] = prompt_document
        assets.append(
            QcAsset(
                asset_id=f"{config_id}.png",
                config_id=config_id,
                output_type=output_type,
                render_path=render_path,
                reference_path=reference_path,
                final_prompt_path=expected_prompt_path,
                handheld=_handheld(configs[config_id]),
                width=width,
                height=height,
            )
        )

    if tuple(observed_ids) != QC_CONFIG_IDS or len(set(observed_ids)) != len(QC_CONFIG_IDS):
        raise ExecutorExecutionError("codex-dev 检测到最终提示词索引覆盖异常")
    batches = tuple(
        QcBatch(index=index // 2 + 1, assets=tuple(assets[index : index + 2]))
        for index in range(0, len(assets), 2)
    )
    plan = QcPlan(
        product_id=product_id,
        output_path=output_path,
        assets=tuple(assets),
        batches=batches,
        rule_documents=_load_rule_documents(repository_root, category_recipe.key),
        documents={
            "product_identity_archive": identity,
            "style_master": style_master,
            "angle_inventory": angle_inventory,
            "main_variable_configs": main_document,
            "detail_variable_configs": detail_document,
            "final_prompt_index": final_index,
            "final_prompts": final_prompt_documents,
            "qc_report_schema": schema,
        },
    )
    _validate_qc_batch_limits(plan)
    return plan
