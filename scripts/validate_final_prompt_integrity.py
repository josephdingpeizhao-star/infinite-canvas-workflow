from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]

SOURCE_RULE_FILES = [
    "产品身份档案提示词.txt",
    "道具生成规则模块.txt",
    "电商图片通用质检清单.txt",
    "反向提取风格母版提示词.txt",
    "工作流总控规则.txt",
    "角度槽位入库表生成与识别提示词.txt",
    "商品信息补充清单提示词.txt",
    "手持产品场景基础模块.txt",
    "手持适配规则.txt",
    "淘宝天猫详情页链路与平台规范模块.txt",
    "套装编排规则.txt",
    "套装变量配置补充模块.txt",
    "套装产品工作流补充规则.txt",
    "套装产品身份档案提示词.txt",
    "套装角度与编排入库表提示词.txt",
    "详情图单张变量配置提示词生成.txt",
    "真实感约束.txt",
    "主图单张变量配置提示词生成.txt",
]

NEGATION_MARKERS = (
    "不要",
    "不得",
    "禁止",
    "不允许",
    "不启用",
    "不能",
    "无",
    "没有",
    "非商品",
    "不作为",
    "不形成",
    "不得暗示",
    "不退化",
    "避免",
    "无法确认",
)

PRODUCT_BODY_MARKERS = (
    "商品本体",
    "产品本体",
    "商品组成",
    "产品组成",
    "组成部分",
    "随附",
    "配件",
    "套装",
    "销售件数",
    "销售套装",
    "一套",
    "包含",
    "配套",
)

COMPILER_PRODUCT_MARKERS = (
    "壶口",
    "壶口边缘",
    "壶身",
    "壶底",
    "壶柄",
    "提梁",
    "壶嘴",
    "出水口",
    "壶盖",
    "滤网",
    "密封圈",
    "刻度",
    "容量感",
    "底足",
    "托盘",
    "图案",
    "品牌字母",
)

STYLE_LITERAL_MARKERS = (
    "奶油",
    "米白",
    "浅蓝",
    "花材",
    "绿植",
    "布料",
    "桌面",
    "背景",
    "商业摄影",
)

GENERIC_TERM_STOPWORDS = {
    "一个",
    "一张",
    "本张",
    "产品",
    "商品",
    "本体",
    "道具",
    "背景",
    "弱道具",
    "非产品",
    "非商品",
    "生活化",
    "低干扰",
    "小面积",
    "真实",
    "清晰",
    "完整",
    "默认",
    "使用",
    "场景",
    "可加入",
    "加入",
    "生成",
    "渲染",
    "保留",
    "形成",
    "出现",
    "优先",
    "不得",
    "不要",
    "禁止",
    "不允许",
    "没有",
    "无法确认",
    "尺寸",
    "容量",
    "重量",
    "颜色",
    "材质",
    "结构",
    "风格",
    "文案",
    "文字",
    "镜头",
    "构图",
    "说明",
}

ONE_CHAR_TERMS = {"勺", "盖"}

DIMENSION_UNIT_RE = re.compile(
    r"(?<![A-Za-z0-9_])\d+(?:\.\d+)?\s*(?:cm|厘米|mm|毫米|mL|ml|ML|毫升|g|克|kg|公斤)\b",
    re.IGNORECASE,
)

HANDHELD_SCOPES = ("all", "main", "detail")

HANDHELD_DISABLED_MARKERS = (
    "不启用手持场景",
    "不启用手持",
    "不使用手持",
    "无手持",
    "非手持",
)

HANDHELD_ENABLED_MARKERS = (
    "手持",
    "手部",
    "静态握持",
    "动态拿起",
    "持握",
    "托举",
    "扶住",
    "托住",
)


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


def first_item(value: Any) -> str:
    if isinstance(value, list):
        if not value:
            raise ScriptError("expected non-empty list path")
        return str(value[0])
    if isinstance(value, str) and value:
        return value
    raise ScriptError("expected non-empty path value")


def artifact_file(value: Any, default_name: str) -> Path:
    path = Path(first_item(value))
    if path.suffix.lower() == ".json":
        return path
    return path / default_name


def artifact_dir(value: Any) -> Path:
    path = Path(first_item(value))
    if path.suffix.lower() == ".json":
        return path.parent
    return path


def default_identity_path(batch_manifest: dict[str, Any]) -> Path:
    return artifact_file(batch_manifest["artifacts"]["product_identity_archive"], "product_identity_archive.json")


def default_final_prompt_index(batch_manifest: dict[str, Any]) -> Path:
    return artifact_dir(batch_manifest["artifacts"]["final_prompts"]) / "final_prompt_index.json"


def default_job_manifest(batch_manifest: dict[str, Any]) -> Path:
    return artifact_dir(batch_manifest["artifacts"]["comfyui_jobs"]) / "comfyui_job_manifest.json"


def default_external_report(batch_manifest: dict[str, Any]) -> Path:
    return artifact_dir(batch_manifest["artifacts"]["qc_reports"]) / "final_prompt_integrity_report.json"


def text_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(text_value(item) for item in value)
    if isinstance(value, dict):
        return " ".join(text_value(item) for item in value.values())
    return ""


def split_sentences(text: str) -> list[str]:
    return [item.strip() for item in re.split(r"(?<=[。；;！!？?\n])", text) if item.strip()]


def contains_cjk(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", text))


def is_term(value: str) -> bool:
    term = value.strip(" ：:，,。；;、（）()【】[]「」'\"“”")
    if not term or term in GENERIC_TERM_STOPWORDS:
        return False
    if len(term) == 1 and term not in ONE_CHAR_TERMS:
        return False
    if len(term) > 18:
        return False
    return contains_cjk(term)


def clean_term(value: str) -> str:
    term = value.strip(" ：:，,。；;、（）()【】[]「」'\"“”")
    term = re.sub(r"^(?:把|添加|新增|生成|标注|输出|启用|改成|推断|作为|成为|变成)", "", term)
    term = re.sub(r"(?:为商品本体|作为商品本体|为产品本体|作为产品本体)$", "", term)
    term = re.sub(r"(?:等|等等|关系|形态|结构|标注|信息)$", "", term)
    return term.strip(" ：:，,。；;、（）()【】[]「」'\"“”")


def split_candidate_terms(fragment: str) -> list[str]:
    normalized = fragment
    normalized = re.sub(r"(?:以及|或者|或|和|与|及)", "、", normalized)
    normalized = re.sub(r"(?:不要|不得|禁止|不允许|没有|无|新增|添加)", "、", normalized)
    normalized = re.split(r"(?:为商品本体|作为商品本体|为产品本体|作为产品本体|作为销售|作为套装|不作为|不得暗示)", normalized)[0]
    terms = []
    for raw in re.split(r"[、，,；;。.\n\r/]+", normalized):
        term = clean_term(raw)
        if is_term(term):
            terms.append(term)
    return ordered_unique(terms)


def ordered_unique(values: Iterable[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def stable_suffix(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]


def negated_context(sentence: str, term: str) -> bool:
    index = sentence.find(term)
    if index < 0:
        return False
    before = sentence[max(0, index - 18) : index + len(term)]
    if any(marker in before for marker in NEGATION_MARKERS):
        return True
    if any(marker in sentence[: index + len(term)] for marker in NEGATION_MARKERS):
        return True
    if any(marker in sentence for marker in NEGATION_MARKERS):
        return True
    return any(marker in sentence for marker in ("非商品道具", "均为非商品道具", "不暗示套装", "不形成商品组成误解"))


def positive_contexts(text: str, term: str) -> list[str]:
    contexts = []
    for sentence in split_sentences(text):
        if term not in sentence:
            continue
        if negated_context(sentence, term):
            continue
        contexts.append(sentence)
    return contexts


def context_is_allowed_prop(sentence: str, term: str, allowed_prop_terms: set[str]) -> bool:
    if "道具" not in sentence and "背景" not in sentence and "前景" not in sentence:
        return False
    if term in allowed_prop_terms:
        return True
    return any(term in prop or prop in term for prop in allowed_prop_terms)


def extract_prohibited_terms(identity: dict[str, Any]) -> list[str]:
    values: list[str] = []
    values.extend(str(item) for item in identity.get("prohibited_inventions", []) if isinstance(item, str))
    if isinstance(identity.get("negative_prompt_constraints"), str):
        values.append(identity["negative_prompt_constraints"])
    values.extend(str(item) for item in identity.get("must_keep", []) if isinstance(item, str) and "无" in item)
    if isinstance(identity.get("product_lock_description"), str):
        values.append(identity["product_lock_description"])

    terms: list[str] = []
    for value in values:
        for match in re.finditer(r"(?:没有|无)([^。；;\n]+)", value):
            terms.extend(split_candidate_terms(match.group(1)))
        for match in re.finditer(r"改成([^。；;\n]+)", value):
            terms.extend(split_candidate_terms(match.group(1)))
        for match in re.finditer(r"新增([^。；;\n]+)", value):
            terms.extend(split_candidate_terms(match.group(1)))
        for match in re.finditer(r"添加([^。；;\n]+)", value):
            terms.extend(split_candidate_terms(match.group(1)))
        for match in re.finditer(r"生成([^。；;\n]+)", value):
            terms.extend(split_candidate_terms(match.group(1)))
        for match in re.finditer(r"启用([^。；;\n]+)", value):
            terms.extend(split_candidate_terms(match.group(1)))
    return ordered_unique(terms)


def extract_terms_from_value(value: Any) -> list[str]:
    text = text_value(value)
    text = re.sub(r"\d+(?:-\d+)?\s*个", "、", text)
    text = re.sub(r"(?:优先|少量|小面积|低干扰|弱|极弱|虚化|浅色|深色|生活化|真实|前景|中景|背景)", "、", text)
    terms = []
    for fragment in re.split(r"[：:，,、；;。.\n\r/（）()【】\[\] ]+", text):
        term = clean_term(fragment)
        if is_term(term):
            terms.append(term)
    return ordered_unique(terms)


def build_identity_terms(identity: dict[str, Any]) -> set[str]:
    values = [
        identity.get("product_name"),
        identity.get("product_category"),
        identity.get("core_shape"),
        identity.get("visual_proportions"),
        identity.get("color_and_material"),
        identity.get("texture_and_surface"),
        identity.get("pattern_and_decoration"),
        identity.get("structural_details"),
        identity.get("product_lock_description"),
        identity.get("negative_prompt_constraints"),
        identity.get("components"),
        identity.get("must_keep"),
    ]
    terms: set[str] = set()
    for value in values:
        terms.update(extract_terms_from_value(value))
    return terms


def add_issue(
    issues: list[dict[str, Any]],
    *,
    issue_id: str,
    severity: str,
    blocking: bool,
    description: str,
    affected_asset: str | None = None,
    job_id: str | None = None,
    evidence: dict[str, Any] | None = None,
    required_action: str | None = None,
) -> None:
    item: dict[str, Any] = {
        "issue_id": issue_id,
        "severity": severity,
        "blocking": blocking,
        "description": description,
    }
    if affected_asset:
        item["affected_asset"] = affected_asset
    if job_id:
        item["job_id"] = job_id
    if evidence:
        item["evidence"] = evidence
    if required_action:
        item["required_action"] = required_action
    issues.append(item)


def result_status(blocking: list[dict[str, Any]], warnings: list[dict[str, Any]]) -> str:
    if blocking:
        return "fail"
    if warnings:
        return "needs_review"
    return "pass"


def int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def output_canvas_type(job: dict[str, Any]) -> str:
    output_type = str(job.get("output_type", "")).lower()
    job_id = str(job.get("job_id", "")).lower()
    if output_type == "detail" or job_id.startswith("detail_"):
        return "detail"
    return "main"


def output_type_in_scope(output_type: str, scope: str) -> bool:
    return scope == "all" or output_type == scope


def handheld_enabled_from_text(text: str) -> bool:
    compact = re.sub(r"\s+", "", text)
    if not compact:
        return False
    if any(marker in compact for marker in HANDHELD_DISABLED_MARKERS):
        return False
    return any(marker in compact for marker in HANDHELD_ENABLED_MARKERS)


def config_handheld_enabled(config: dict[str, Any]) -> bool:
    declaration = str(config.get("手持交互声明", ""))
    return handheld_enabled_from_text(declaration)


def final_prompt_handheld_enabled(final_doc: dict[str, Any]) -> bool:
    final_prompt = str(final_doc.get("final_prompt", ""))
    match = re.search(r"手持[：:]\s*([^\n]+)", final_prompt)
    if match:
        return handheld_enabled_from_text(match.group(1))
    return handheld_enabled_from_text(final_prompt)


def inspect_job_canvas_ratios(
    *,
    job_manifest: dict[str, Any],
    job_manifest_path: Path,
    issues: list[dict[str, Any]],
) -> dict[str, Any]:
    summary = {
        "checked_job_count": 0,
        "main_job_count": 0,
        "detail_job_count": 0,
        "missing_dimension_count": 0,
        "invalid_ratio_count": 0,
    }
    for job in job_manifest.get("jobs", []):
        if not isinstance(job, dict):
            continue
        job_id = str(job.get("job_id") or "<unknown>")
        output_type = output_canvas_type(job)
        width = int_or_none(job.get("width"))
        height = int_or_none(job.get("height"))
        summary["checked_job_count"] += 1
        if output_type == "detail":
            summary["detail_job_count"] += 1
        else:
            summary["main_job_count"] += 1
        if width is None or height is None:
            summary["missing_dimension_count"] += 1
            add_issue(
                issues,
                issue_id=f"output_canvas_dimensions_missing_{job_id}",
                severity="critical",
                blocking=True,
                description="ComfyUI job manifest entry is missing width or height, so output canvas ratio cannot be enforced.",
                affected_asset=str(job_manifest_path),
                job_id=job_id,
                evidence={"width": job.get("width"), "height": job.get("height"), "output_type": output_type},
                required_action="Regenerate the job manifest with explicit width and height before rendering.",
            )
            continue
        if output_type == "main" and width != height:
            summary["invalid_ratio_count"] += 1
            add_issue(
                issues,
                issue_id=f"output_canvas_ratio_main_{job_id}",
                severity="critical",
                blocking=True,
                description="Main image job output canvas is not 1:1.",
                affected_asset=str(job_manifest_path),
                job_id=job_id,
                evidence={"width": width, "height": height, "required_rule": "width == height"},
                required_action="Set main image job dimensions to a 1:1 canvas, for example 1440x1440.",
            )
        if output_type == "detail" and width * 4 != height * 3:
            summary["invalid_ratio_count"] += 1
            add_issue(
                issues,
                issue_id=f"output_canvas_ratio_detail_{job_id}",
                severity="critical",
                blocking=True,
                description="Detail image job output canvas is not 3:4.",
                affected_asset=str(job_manifest_path),
                job_id=job_id,
                evidence={"width": width, "height": height, "required_rule": "width * 4 == height * 3"},
                required_action="Set detail image job dimensions to a 3:4 canvas, for example 1440x1920.",
            )
    return summary


def inspect_handheld_count_expectation(
    *,
    prompt_records: list[dict[str, str]],
    job_manifest: dict[str, Any],
    expected_handheld_count: int | None,
    expected_handheld_scope: str,
    issues: list[dict[str, Any]],
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "expected_handheld_count": expected_handheld_count,
        "expected_handheld_scope": expected_handheld_scope,
        "checked_prompt_count": 0,
        "checked_job_count": 0,
        "variable_config_handheld_count": 0,
        "final_prompt_handheld_count": 0,
        "job_manifest_handheld_count": 0,
        "variable_config_handheld_job_ids": [],
        "final_prompt_handheld_job_ids": [],
        "job_manifest_handheld_job_ids": [],
    }

    if expected_handheld_scope not in HANDHELD_SCOPES:
        add_issue(
            issues,
            issue_id="handheld_expected_scope_invalid",
            severity="critical",
            blocking=True,
            description="Expected handheld count scope is invalid.",
            evidence={"expected_handheld_scope": expected_handheld_scope, "allowed": HANDHELD_SCOPES},
            required_action="Use one of: all, main, detail.",
        )
        return summary

    if expected_handheld_count is not None and expected_handheld_count < 0:
        add_issue(
            issues,
            issue_id="handheld_expected_count_invalid",
            severity="critical",
            blocking=True,
            description="Expected handheld count cannot be negative.",
            evidence={"expected_handheld_count": expected_handheld_count},
            required_action="Pass a zero or positive expected handheld count.",
        )
        return summary

    for record in prompt_records:
        output_type = output_canvas_type(record)
        if not output_type_in_scope(output_type, expected_handheld_scope):
            continue
        summary["checked_prompt_count"] += 1
        job_id = str(record.get("job_id") or "<unknown>")
        path = Path(record["final_prompt_path"])
        try:
            final_doc = load_json(path)
            variable_config = resolved_variable_config(final_doc)
        except ScriptError:
            continue
        config_enabled = config_handheld_enabled(variable_config)
        prompt_enabled = final_prompt_handheld_enabled(final_doc)
        if config_enabled:
            summary["variable_config_handheld_job_ids"].append(job_id)
        if prompt_enabled:
            summary["final_prompt_handheld_job_ids"].append(job_id)
        if config_enabled != prompt_enabled:
            add_issue(
                issues,
                issue_id=f"handheld_declaration_mismatch_{job_id}",
                severity="critical",
                blocking=True,
                description="Variable config and final prompt disagree on whether this image is handheld.",
                affected_asset=str(path),
                job_id=job_id,
                evidence={
                    "variable_config_handheld": config_enabled,
                    "final_prompt_handheld": prompt_enabled,
                    "handheld_declaration": variable_config.get("手持交互声明"),
                },
                required_action="Recompile final prompts so handheld declarations are preserved from variable configs.",
            )

    for job in job_manifest.get("jobs", []):
        if not isinstance(job, dict):
            continue
        output_type = output_canvas_type(job)
        if not output_type_in_scope(output_type, expected_handheld_scope):
            continue
        summary["checked_job_count"] += 1
        job_id = str(job.get("job_id") or "<unknown>")
        path = Path(str(job.get("final_prompt_path", "")))
        try:
            final_doc = load_json(path)
        except ScriptError:
            continue
        if final_prompt_handheld_enabled(final_doc):
            summary["job_manifest_handheld_job_ids"].append(job_id)

    summary["variable_config_handheld_count"] = len(summary["variable_config_handheld_job_ids"])
    summary["final_prompt_handheld_count"] = len(summary["final_prompt_handheld_job_ids"])
    summary["job_manifest_handheld_count"] = len(summary["job_manifest_handheld_job_ids"])

    if expected_handheld_count is None:
        return summary

    if expected_handheld_count > summary["checked_job_count"]:
        add_issue(
            issues,
            issue_id="handheld_expected_count_exceeds_available_jobs",
            severity="critical",
            blocking=True,
            description="Expected handheld count is larger than the available job count in the selected scope.",
            evidence={
                "expected_handheld_count": expected_handheld_count,
                "expected_handheld_scope": expected_handheld_scope,
                "checked_job_count": summary["checked_job_count"],
            },
            required_action="Clarify whether handheld images should occupy the existing image count or require additional generated images.",
        )

    expected_checks = {
        "variable_config": summary["variable_config_handheld_count"],
        "final_prompt": summary["final_prompt_handheld_count"],
        "job_manifest": summary["job_manifest_handheld_count"],
    }
    for layer, actual_count in expected_checks.items():
        if actual_count == expected_handheld_count:
            continue
        add_issue(
            issues,
            issue_id=f"handheld_expected_count_mismatch_{layer}",
            severity="critical",
            blocking=True,
            description=f"{layer} handheld count does not match the explicit expected handheld count.",
            evidence={
                "expected_handheld_count": expected_handheld_count,
                "expected_handheld_scope": expected_handheld_scope,
                "actual_count": actual_count,
                "summary": summary,
            },
            required_action="Regenerate or repair variable configs, final prompts, and job manifest so the explicit handheld count is preserved.",
        )
    return summary


def parse_index_from_pointer(pointer: str) -> int | None:
    match = re.fullmatch(r"/configs/(\d+)/per_image_overrides", pointer)
    return int(match.group(1)) if match else None


def resolved_variable_config(final_doc: dict[str, Any]) -> dict[str, Any]:
    ref = final_doc.get("variable_config")
    if not isinstance(ref, dict):
        raise ScriptError("final prompt is missing variable_config reference")
    source_path = Path(str(ref.get("source_path", "")))
    if not source_path.is_file():
        raise ScriptError(f"variable config source not found: {source_path}")
    doc = load_json(source_path)
    common = doc.get("common_constraints")
    if not isinstance(common, dict):
        raise ScriptError(f"variable config has no common_constraints: {source_path}")
    pointer = (((ref.get("per_image_overrides_ref") or {}).get("json_pointer")) or "")
    index = parse_index_from_pointer(str(pointer))
    configs = doc.get("configs") if isinstance(doc.get("configs"), list) else []
    if index is None or index >= len(configs):
        raise ScriptError(f"invalid per_image_overrides_ref: {pointer}")
    override = configs[index].get("per_image_overrides")
    if not isinstance(override, dict):
        raise ScriptError(f"config override is not an object: {source_path} {pointer}")
    merged = dict(common)
    merged.update(override)
    return merged


def allowed_prop_terms_from_config(config: dict[str, Any]) -> set[str]:
    keys = [
        "道具生成",
        "道具关系",
        "风格贴合锚点调用",
        "背景层次配置",
        "内容物状态",
    ]
    terms: set[str] = set()
    for key in keys:
        terms.update(extract_terms_from_value(config.get(key)))
    return terms


def collect_prompt_paths(final_index: dict[str, Any], job_manifest: dict[str, Any]) -> tuple[list[dict[str, str]], list[str]]:
    records: list[dict[str, str]] = []
    index_paths: set[str] = set()
    job_paths: set[str] = set()

    for item in final_index.get("items", []):
        if not isinstance(item, dict):
            continue
        path = str(item.get("final_prompt_path", ""))
        if not path:
            continue
        index_paths.add(path)
        records.append(
            {
                "job_id": str(item.get("job_id") or Path(path).stem),
                "output_type": str(item.get("output_type") or ""),
                "final_prompt_path": path,
            }
        )

    for job in job_manifest.get("jobs", []):
        if not isinstance(job, dict):
            continue
        path = str(job.get("final_prompt_path", ""))
        if not path:
            continue
        job_paths.add(path)
        if path not in index_paths:
            records.append(
                {
                    "job_id": str(job.get("job_id") or Path(path).stem),
                    "output_type": str(job.get("output_type") or ""),
                    "final_prompt_path": path,
                }
            )

    missing_from_jobs = sorted(index_paths - job_paths)
    missing_from_index = sorted(job_paths - index_paths)
    mismatches = []
    if missing_from_jobs:
        mismatches.append(f"index_only={len(missing_from_jobs)}")
    if missing_from_index:
        mismatches.append(f"job_manifest_only={len(missing_from_index)}")

    deduped: list[dict[str, str]] = []
    seen = set()
    for record in records:
        path = record["final_prompt_path"]
        if path in seen:
            continue
        seen.add(path)
        deduped.append(record)
    return deduped, mismatches


def source_rule_name_hits(final_prompt: str) -> list[str]:
    hits = []
    for name in SOURCE_RULE_FILES:
        stem = Path(name).stem
        if name in final_prompt or stem in final_prompt:
            hits.append(name)
    return hits


def prompt_contains_source_rule_body(final_prompt: str) -> list[str]:
    hits = []
    if len(final_prompt) < 300:
        return hits
    for name in SOURCE_RULE_FILES:
        path = ROOT / name
        if not path.is_file():
            continue
        source = path.read_text(encoding="utf-8", errors="ignore")
        source = re.sub(r"\s+", "", source)
        prompt = re.sub(r"\s+", "", final_prompt)
        for start in range(0, max(0, len(source) - 180), 240):
            fragment = source[start : start + 180]
            if len(fragment) >= 160 and fragment in prompt:
                hits.append(name)
                break
    return hits


def add_product_id_source(
    sources: dict[str, dict[str, Any]],
    product_id: str,
    *,
    source_type: str,
    path: Path,
    current_product_id: str,
) -> None:
    if not product_id or product_id == current_product_id:
        return
    entry = sources.setdefault(
        product_id,
        {
            "product_id": product_id,
            "source_types": [],
            "paths": [],
        },
    )
    if source_type not in entry["source_types"]:
        entry["source_types"].append(source_type)
    path_text = str(path)
    if path_text not in entry["paths"]:
        entry["paths"].append(path_text)


def discover_other_product_id_sources(current_product_id: str, *, include_historical_reports: bool = False) -> dict[str, Any]:
    sources: dict[str, dict[str, Any]] = {}
    excluded_historical_sources: dict[str, dict[str, Any]] = {}
    for manifest in (ROOT / "manifests").glob("*.batch_manifest.json"):
        if manifest.name == "batch_manifest.template.json":
            continue
        data = None
        try:
            data = load_json(manifest)
        except ScriptError:
            pass
        if isinstance(data, dict) and isinstance(data.get("product_id"), str):
            add_product_id_source(
                sources,
                data["product_id"],
                source_type="active_batch_manifest",
                path=manifest,
                current_product_id=current_product_id,
            )
        add_product_id_source(
            sources,
            manifest.name.removesuffix(".batch_manifest.json"),
            source_type="active_batch_manifest_filename",
            path=manifest,
            current_product_id=current_product_id,
        )
    for report in (ROOT / "reports").glob("*_stage_*.json"):
        target = sources if include_historical_reports else excluded_historical_sources
        add_product_id_source(
            target,
            report.name.split("_stage_", 1)[0],
            source_type="historical_stage_report",
            path=report,
            current_product_id=current_product_id,
        )

    for entry in list(sources.values()) + list(excluded_historical_sources.values()):
        entry["source_types"] = sorted(entry["source_types"])
        entry["paths"] = sorted(entry["paths"])

    all_ids = sorted(sources)
    active_manifest_ids = sorted(
        product_id
        for product_id, entry in sources.items()
        if "active_batch_manifest" in entry["source_types"]
        or "active_batch_manifest_filename" in entry["source_types"]
    )
    historical_context_ids = sorted(
        product_id
        for product_id, entry in sources.items()
        if "historical_stage_report" in entry["source_types"] and product_id not in active_manifest_ids
    )
    excluded_historical_context_ids = sorted(excluded_historical_sources)
    return {
        "all_product_ids": all_ids,
        "active_manifest_product_ids": active_manifest_ids,
        "historical_context_product_ids": historical_context_ids,
        "excluded_historical_context_product_ids": excluded_historical_context_ids,
        "by_product_id": {product_id: sources[product_id] for product_id in all_ids},
        "excluded_by_product_id": {
            product_id: excluded_historical_sources[product_id] for product_id in excluded_historical_context_ids
        },
    }


def discover_other_product_ids(current_product_id: str) -> list[str]:
    return discover_other_product_id_sources(current_product_id)["all_product_ids"]


def inspect_prompt_doc(
    *,
    record: dict[str, str],
    final_doc: dict[str, Any],
    identity_doc: dict[str, Any],
    identity: dict[str, Any],
    prohibited_terms: list[str],
    other_product_ids: list[str],
    other_product_id_sources: dict[str, dict[str, Any]],
    issues: list[dict[str, Any]],
) -> set[str]:
    path = record["final_prompt_path"]
    job_id = record["job_id"]
    final_prompt = str(final_doc.get("final_prompt", ""))
    negative_prompt = str(final_doc.get("negative_prompt", ""))
    combined_prompt = f"{final_prompt}\n{negative_prompt}"
    allowed_prop_terms: set[str] = set()

    if final_doc.get("product_id") != identity_doc.get("product_id"):
        add_issue(
            issues,
            issue_id=f"product_id_mismatch_{job_id}",
            severity="critical",
            blocking=True,
            description="Final prompt product_id does not match the current product identity archive.",
            affected_asset=path,
            job_id=job_id,
            evidence={"final_prompt_product_id": final_doc.get("product_id"), "identity_product_id": identity_doc.get("product_id")},
            required_action="Recompile final prompts from the current batch manifest and identity archive.",
        )

    if final_doc.get("artifact_type") != "final_prompt":
        add_issue(
            issues,
            issue_id=f"artifact_type_invalid_{job_id}",
            severity="critical",
            blocking=True,
            description="Render entry points to an artifact that is not a final_prompt JSON document.",
            affected_asset=path,
            job_id=job_id,
        )

    if final_doc.get("uses_upstream_prompt_files_as_visual_requirements") is not False:
        add_issue(
            issues,
            issue_id=f"upstream_prompt_misuse_flag_{job_id}",
            severity="critical",
            blocking=True,
            description="Final prompt artifact allows upstream business prompt files to be used as visual requirements.",
            affected_asset=path,
            job_id=job_id,
            required_action="Recompile so final_prompt is derived from structured artifacts, not source rule prompt files.",
        )

    if not final_prompt.strip():
        add_issue(
            issues,
            issue_id=f"empty_final_prompt_{job_id}",
            severity="critical",
            blocking=True,
            description="Final prompt text is empty.",
            affected_asset=path,
            job_id=job_id,
        )

    try:
        variable_config = resolved_variable_config(final_doc)
        allowed_prop_terms = allowed_prop_terms_from_config(variable_config)
        allowed_prop_terms -= build_identity_terms(identity)
    except ScriptError as exc:
        add_issue(
            issues,
            issue_id=f"variable_config_unresolved_{job_id}",
            severity="critical",
            blocking=True,
            description="The gate could not resolve this final prompt's variable config reference.",
            affected_asset=path,
            job_id=job_id,
            evidence={"message": str(exc)},
            required_action="Repair final prompt variable_config references before rendering.",
        )

    expected_negative = identity.get("negative_prompt_constraints")
    if isinstance(expected_negative, str) and expected_negative.strip() and expected_negative.strip() != negative_prompt.strip():
        add_issue(
            issues,
            issue_id=f"negative_prompt_identity_mismatch_{job_id}",
            severity="major",
            blocking=True,
            description="Final prompt negative_prompt differs from the current product identity archive constraints.",
            affected_asset=path,
            job_id=job_id,
            required_action="Recompile final prompts so negative prompt constraints come from the current identity archive.",
        )

    product_lock = str(identity.get("product_lock_description", "")).strip()
    product_lock_present = bool(product_lock and product_lock in final_prompt)
    for phrase in identity.get("must_keep", []) if isinstance(identity.get("must_keep"), list) else []:
        phrase_text = str(phrase).strip()
        phrase_terms = extract_terms_from_value(phrase_text)
        phrase_covered = phrase_text in combined_prompt or product_lock_present or any(term in combined_prompt for term in phrase_terms)
        if phrase_text and not phrase_covered:
            add_issue(
                issues,
                issue_id=f"must_keep_not_explicit_{job_id}_{stable_suffix(phrase_text)}",
                severity="needs_review",
                blocking=False,
                description="A must_keep identity phrase is not explicitly present in the final prompt or negative prompt.",
                affected_asset=path,
                job_id=job_id,
                evidence={"must_keep": phrase_text},
                required_action="Review whether the product lock still preserves this identity requirement.",
            )

    for term in prohibited_terms:
        contexts = positive_contexts(combined_prompt, term)
        contexts = [context for context in contexts if not context_is_allowed_prop(context, term, allowed_prop_terms)]
        if not contexts:
            continue
        add_issue(
            issues,
            issue_id=f"prohibited_positive_{job_id}_{stable_suffix(term)}",
            severity="major",
            blocking=True,
            description="A term derived from prohibited_inventions or negative constraints appears in a positive prompt context.",
            affected_asset=path,
            job_id=job_id,
            evidence={"term": term, "contexts": contexts[:3]},
            required_action="Remove the positive invention or rephrase it as a prohibition before rendering.",
        )

    for prop_term in sorted(allowed_prop_terms):
        for sentence in positive_contexts(final_prompt, prop_term):
            if not any(marker in sentence for marker in PRODUCT_BODY_MARKERS):
                continue
            if negated_context(sentence, prop_term):
                continue
            add_issue(
                issues,
                issue_id=f"prop_as_product_body_{job_id}_{stable_suffix(prop_term)}",
                severity="major",
                blocking=True,
                description="A non-product prop appears to be described as product body, included accessory, or sales component.",
                affected_asset=path,
                job_id=job_id,
                evidence={"prop_term": prop_term, "context": sentence},
                required_action="Keep allowed props as scene/background items only, not product components.",
            )

    true_dimensions = identity.get("true_dimensions")
    confidence_text = text_value(true_dimensions).lower() if isinstance(true_dimensions, dict) else ""
    low_confidence = any(marker in confidence_text for marker in ("低", "无法确认", "unknown", "unconfirmed"))
    if low_confidence:
        unit_hits = DIMENSION_UNIT_RE.findall(final_prompt)
        if unit_hits:
            add_issue(
                issues,
                issue_id=f"low_confidence_exact_units_{job_id}",
                severity="major",
                blocking=True,
                description="Identity dimension/capacity/weight confidence is low or unconfirmed, but final prompt contains exact cm/ml/g-style units.",
                affected_asset=path,
                job_id=job_id,
                evidence={"unit_hits": unit_hits},
                required_action="Remove exact dimensions, capacity, or weight unless confirmed by the product identity archive.",
            )

    for other_id in other_product_ids:
        if other_id and other_id in combined_prompt:
            source_info = other_product_id_sources.get(other_id, {})
            source_types = source_info.get("source_types", [])
            source_scope = (
                "active_batch_manifest"
                if "active_batch_manifest" in source_types or "active_batch_manifest_filename" in source_types
                else "historical_context"
            )
            add_issue(
                issues,
                issue_id=f"other_product_id_residual_{job_id}_{other_id}",
                severity="major",
                blocking=True,
                description="Final prompt contains another known product_id, suggesting old product residue.",
                affected_asset=path,
                job_id=job_id,
                evidence={
                    "other_product_id": other_id,
                    "source_scope": source_scope,
                    "source_types": source_types,
                    "source_paths": source_info.get("paths", [])[:5],
                },
                required_action="Recompile from the current batch manifest and remove stale product residue.",
            )

    name_hits = source_rule_name_hits(final_prompt)
    body_hits = prompt_contains_source_rule_body(final_prompt)
    if name_hits or body_hits:
        add_issue(
            issues,
            issue_id=f"source_rule_prompt_misuse_{job_id}",
            severity="critical",
            blocking=True,
            description="Final prompt appears to contain source business rule prompt file names or long source prompt body fragments.",
            affected_asset=path,
            job_id=job_id,
            evidence={"source_rule_name_hits": name_hits, "source_rule_body_hits": body_hits},
            required_action="Use the compiled final prompt text only; do not pass upstream rule prompt files to image generation.",
        )

    return allowed_prop_terms


def string_literals(path: Path) -> list[dict[str, Any]]:
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ScriptError(f"cannot parse compiler source: {path}: {exc}") from exc
    literals: list[dict[str, Any]] = []

    class Visitor(ast.NodeVisitor):
        def visit_Constant(self, node: ast.Constant) -> Any:
            if isinstance(node.value, str) and contains_cjk(node.value):
                literals.append({"line": getattr(node, "lineno", None), "value": node.value})
            self.generic_visit(node)

    Visitor().visit(tree)
    return literals


def inspect_compiler(
    *,
    compiler_path: Path,
    identity: dict[str, Any],
    prohibited_terms: list[str],
    issues: list[dict[str, Any]],
) -> None:
    if not compiler_path.is_file():
        add_issue(
            issues,
            issue_id="compiler_missing",
            severity="critical",
            blocking=True,
            description="Final prompt compiler script does not exist.",
            affected_asset=str(compiler_path),
        )
        return

    identity_text = text_value(identity)
    identity_terms = build_identity_terms(identity)
    for literal in string_literals(compiler_path):
        value = str(literal["value"])
        line = literal.get("line")
        product_markers = [marker for marker in COMPILER_PRODUCT_MARKERS if marker in value]
        style_markers = [marker for marker in STYLE_LITERAL_MARKERS if marker in value]
        if not product_markers and not style_markers:
            continue

        conflicting_terms = []
        for term in prohibited_terms:
            if not positive_contexts(value, term):
                continue
            if term in identity_text:
                continue
            conflicting_terms.append(term)

        marker_conflicts = [
            marker
            for marker in product_markers
            if marker not in identity_text and marker not in identity_terms and positive_contexts(value, marker)
        ]
        if conflicting_terms or marker_conflicts:
            add_issue(
                issues,
                issue_id=f"compiler_product_hardcode_conflict_l{line}",
                severity="major",
                blocking=True,
                description="Compiler contains a product-specific hardcoded literal that conflicts with the current product identity.",
                affected_asset=str(compiler_path),
                evidence={
                    "line": line,
                    "literal_excerpt": value[:220],
                    "conflicting_terms": ordered_unique(conflicting_terms + marker_conflicts),
                },
                required_action="Move product-specific wording into identity or variable config artifacts before rendering.",
            )
            continue

        if product_markers:
            add_issue(
                issues,
                issue_id=f"compiler_product_specific_literal_l{line}",
                severity="needs_review",
                blocking=False,
                description="Compiler contains product-specific Chinese visual wording. It is non-blocking only because it does not conflict with the current identity.",
                affected_asset=str(compiler_path),
                evidence={"line": line, "markers": product_markers, "literal_excerpt": value[:220]},
                required_action="Keep product-specific product/body facts in product identity and variable config artifacts.",
            )
        elif style_markers and value not in identity_text:
            add_issue(
                issues,
                issue_id=f"compiler_style_literal_l{line}",
                severity="needs_review",
                blocking=False,
                description="Compiler contains hardcoded style fallback wording. Review if final prompts ever rely on this instead of style artifacts and variable configs.",
                affected_asset=str(compiler_path),
                evidence={"line": line, "markers": style_markers, "literal_excerpt": value[:220]},
            )


def build_report(
    *,
    batch_manifest_path: Path,
    identity_path: Path,
    final_prompt_index_path: Path,
    job_manifest_path: Path,
    compiler_path: Path,
    expected_handheld_count: int | None = None,
    expected_handheld_scope: str = "all",
) -> dict[str, Any]:
    batch_manifest = load_json(batch_manifest_path)
    identity_doc = load_json(identity_path)
    final_index = load_json(final_prompt_index_path)
    job_manifest = load_json(job_manifest_path)
    identity = identity_doc.get("identity") if isinstance(identity_doc.get("identity"), dict) else {}
    if not identity:
        raise ScriptError("product identity archive is missing identity object")

    issues: list[dict[str, Any]] = []
    product_id = str(batch_manifest.get("product_id") or identity_doc.get("product_id") or "")
    prohibited_terms = extract_prohibited_terms(identity)
    other_product_id_context = discover_other_product_id_sources(product_id)
    other_product_ids = other_product_id_context["all_product_ids"]
    other_product_id_sources = other_product_id_context["by_product_id"]
    prompt_records, path_mismatches = collect_prompt_paths(final_index, job_manifest)
    canvas_ratio_summary = inspect_job_canvas_ratios(
        job_manifest=job_manifest,
        job_manifest_path=job_manifest_path,
        issues=issues,
    )
    handheld_count_summary = inspect_handheld_count_expectation(
        prompt_records=prompt_records,
        job_manifest=job_manifest,
        expected_handheld_count=expected_handheld_count,
        expected_handheld_scope=expected_handheld_scope,
        issues=issues,
    )
    checked_assets = [record["final_prompt_path"] for record in prompt_records]
    all_allowed_prop_terms: set[str] = set()

    if final_index.get("uses_upstream_prompt_files_as_visual_requirements") is not False:
        add_issue(
            issues,
            issue_id="final_prompt_index_upstream_prompt_misuse",
            severity="critical",
            blocking=True,
            description="Final prompt index does not explicitly forbid upstream prompt files as visual requirements.",
            affected_asset=str(final_prompt_index_path),
            required_action="Rebuild the final prompt index with uses_upstream_prompt_files_as_visual_requirements=false.",
        )

    if path_mismatches:
        add_issue(
            issues,
            issue_id="final_prompt_index_job_manifest_mismatch",
            severity="critical",
            blocking=True,
            description="Final prompt index and ComfyUI job manifest do not reference the same final prompt set.",
            affected_asset=str(job_manifest_path),
            evidence={"mismatches": path_mismatches},
            required_action="Regenerate final prompts and job manifest before rendering.",
        )

    if not prompt_records:
        add_issue(
            issues,
            issue_id="no_final_prompt_records",
            severity="critical",
            blocking=True,
            description="No final prompt records were found in the final prompt index or job manifest.",
            affected_asset=str(final_prompt_index_path),
        )

    for record in prompt_records:
        path = Path(record["final_prompt_path"])
        if path.name in SOURCE_RULE_FILES or path.suffix.lower() != ".json":
            add_issue(
                issues,
                issue_id=f"render_entry_not_final_prompt_json_{record['job_id']}",
                severity="critical",
                blocking=True,
                description="Render job points to a source prompt file or non-JSON artifact instead of a compiled final_prompt JSON.",
                affected_asset=str(path),
                job_id=record["job_id"],
            )
            continue
        try:
            final_doc = load_json(path)
        except ScriptError as exc:
            add_issue(
                issues,
                issue_id=f"final_prompt_unreadable_{record['job_id']}",
                severity="critical",
                blocking=True,
                description="Final prompt JSON could not be read.",
                affected_asset=str(path),
                job_id=record["job_id"],
                evidence={"message": str(exc)},
            )
            continue
        all_allowed_prop_terms.update(
            inspect_prompt_doc(
                record=record,
                final_doc=final_doc,
                identity_doc=identity_doc,
                identity=identity,
                prohibited_terms=prohibited_terms,
                other_product_ids=other_product_ids,
                other_product_id_sources=other_product_id_sources,
                issues=issues,
            )
        )

    inspect_compiler(
        compiler_path=compiler_path,
        identity=identity,
        prohibited_terms=prohibited_terms,
        issues=issues,
    )

    blocking_issues = [item for item in issues if item.get("blocking") is True]
    warnings = [item for item in issues if item.get("blocking") is not True]
    status = result_status(blocking_issues, warnings)

    results = [
        {
            "check_item": "final_prompt_artifact_resolution",
            "status": "fail" if any(item["issue_id"].startswith(("final_prompt_unreadable", "no_final_prompt", "render_entry")) for item in blocking_issues) else "pass",
            "notes": f"checked_prompt_count={len(prompt_records)}.",
        },
        {
            "check_item": "upstream_prompt_files_not_used_as_final_prompts",
            "status": "fail" if any("source_rule_prompt_misuse" in item["issue_id"] or "upstream_prompt" in item["issue_id"] for item in blocking_issues) else "pass",
            "notes": "Final prompt text must be compiled prompt text, not an upstream business rule prompt file.",
        },
        {
            "check_item": "output_canvas_ratio",
            "status": "fail" if any(item["issue_id"].startswith("output_canvas_") for item in blocking_issues) else "pass",
            "notes": (
                f"checked_job_count={canvas_ratio_summary['checked_job_count']}, "
                f"main={canvas_ratio_summary['main_job_count']}, "
                f"detail={canvas_ratio_summary['detail_job_count']}, "
                f"missing_dimensions={canvas_ratio_summary['missing_dimension_count']}, "
                f"invalid_ratios={canvas_ratio_summary['invalid_ratio_count']}."
            ),
        },
        {
            "check_item": "explicit_handheld_count_preserved",
            "status": "fail" if any(item["issue_id"].startswith("handheld_") for item in blocking_issues) else "pass",
            "notes": (
                f"expected={handheld_count_summary['expected_handheld_count']}, "
                f"scope={handheld_count_summary['expected_handheld_scope']}, "
                f"variable_configs={handheld_count_summary['variable_config_handheld_count']}, "
                f"final_prompts={handheld_count_summary['final_prompt_handheld_count']}, "
                f"job_manifest={handheld_count_summary['job_manifest_handheld_count']}."
            ),
        },
        {
            "check_item": "product_identity_constraints",
            "status": "fail" if any(item["issue_id"].startswith(("prohibited_positive", "negative_prompt_identity_mismatch", "low_confidence_exact_units", "product_id_mismatch")) for item in blocking_issues) else ("needs_review" if any(item["issue_id"].startswith("must_keep_not_explicit") for item in warnings) else "pass"),
            "notes": "Checks must_keep surfacing, prohibited inventions, negative prompt constraints, and low-confidence exact unit usage.",
        },
        {
            "check_item": "prop_product_body_boundary",
            "status": "fail" if any(item["issue_id"].startswith("prop_as_product_body") for item in blocking_issues) else "pass",
            "notes": "Allowed scene props remain props/background and are not described as product body or sales components.",
        },
        {
            "check_item": "compiler_product_hardcoding",
            "status": "fail" if any(item["issue_id"].startswith("compiler_product_hardcode_conflict") for item in blocking_issues) else ("needs_review" if any(item["issue_id"].startswith("compiler_") for item in warnings) else "pass"),
            "notes": "Compiler literals are checked against the current identity archive; non-conflicting hardcoded fallbacks are reported as review warnings.",
        },
        {
            "check_item": "other_product_id_source_scope",
            "status": "pass",
            "notes": (
                f"active_manifest_ids={len(other_product_id_context['active_manifest_product_ids'])}, "
                f"historical_context_ids={len(other_product_id_context['historical_context_product_ids'])}, "
                f"excluded_historical_context_ids={len(other_product_id_context['excluded_historical_context_product_ids'])}. "
                "Historical report filenames are not used as current residue evidence by default."
            ),
        },
    ]

    return {
        "product_id": product_id,
        "artifact_type": "final_prompt_integrity_report",
        "gate_name": "final_prompt_integrity_gate",
        "status": status,
        "render_blocked": bool(blocking_issues),
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "batch_manifest": str(batch_manifest_path),
        "product_identity_archive": str(identity_path),
        "final_prompt_index": str(final_prompt_index_path),
        "job_manifest": str(job_manifest_path),
        "compiler": str(compiler_path),
        "checked_assets": checked_assets,
        "checked_prompt_count": len(prompt_records),
        "output_canvas_ratio_summary": canvas_ratio_summary,
        "handheld_count_summary": handheld_count_summary,
        "blocking_issue_count": len(blocking_issues),
        "warning_count": len(warnings),
        "results": results,
        "blocking_issues": blocking_issues,
        "warnings": warnings,
        "issues": issues,
        "derived_policy_terms": {
            "prohibited_terms": prohibited_terms,
            "allowed_prop_terms_seen": sorted(all_allowed_prop_terms),
            "other_known_product_ids": other_product_ids,
            "other_product_id_sources": other_product_id_context,
        },
        "image_generation_performed": False,
        "comfyui_execution_performed": False,
        "notes": "Gate is product-agnostic: it derives product constraints from the current batch manifest, identity archive, variable configs, final prompts, and compiler source.",
    }


def write_markdown(path: Path, report: dict[str, Any]) -> None:
    lines = [
        "# Final Prompt Integrity Report",
        "",
        f"- product_id: {report['product_id']}",
        f"- status: {report['status']}",
        f"- render_blocked: {str(report['render_blocked']).lower()}",
        f"- checked_at: {report['checked_at']}",
        f"- checked_prompt_count: {report['checked_prompt_count']}",
        f"- blocking_issue_count: {report['blocking_issue_count']}",
        f"- warning_count: {report['warning_count']}",
        "- image_generation_performed: false",
        "- comfyui_execution_performed: false",
        "",
        "## Results",
        "",
    ]
    for item in report["results"]:
        lines.append(f"- {item['check_item']}: {item['status']} ({item.get('notes', '')})")
    lines.extend(["", "## Blocking Issues", ""])
    if report["blocking_issues"]:
        for item in report["blocking_issues"]:
            lines.append(f"- {item['issue_id']}: {item['severity']} | {item.get('job_id', '-')} | {item['description']}")
    else:
        lines.append("- None")
    lines.extend(["", "## Warnings", ""])
    if report["warnings"]:
        for item in report["warnings"]:
            lines.append(f"- {item['issue_id']}: {item['severity']} | {item.get('job_id', '-')} | {item['description']}")
    else:
        lines.append("- None")
    lines.extend(["", "## Checked Assets", ""])
    if report["checked_assets"]:
        lines.extend(f"- {item}" for item in report["checked_assets"])
    else:
        lines.append("- None")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_report_files(
    *,
    report: dict[str, Any],
    output_report: Path,
    output_markdown: Path,
    repo_report_dir: Path | None,
    repo_report_prefix: str | None,
) -> tuple[Path, Path, Path | None, Path | None]:
    write_json(output_report, report)
    write_markdown(output_markdown, report)

    repo_json_path: Path | None = None
    repo_md_path: Path | None = None
    if repo_report_dir is not None:
        prefix = repo_report_prefix or f"{report['product_id']}_final_prompt_integrity_report"
        repo_json_path = repo_report_dir / f"{prefix}.json"
        repo_md_path = repo_report_dir / f"{prefix}.md"
        if repo_json_path.resolve() != output_report.resolve():
            write_json(repo_json_path, report)
        if repo_md_path.resolve() != output_markdown.resolve():
            write_markdown(repo_md_path, report)
    return output_report, output_markdown, repo_json_path, repo_md_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate final prompt integrity before ComfyUI job preparation or rendering.")
    parser.add_argument("--batch-manifest", required=True)
    parser.add_argument("--product-identity", default=None)
    parser.add_argument("--final-prompt-index", default=None)
    parser.add_argument("--job-manifest", default=None)
    parser.add_argument("--compiler", default=str(ROOT / "scripts" / "compile_final_prompts.py"))
    parser.add_argument("--output-report", default=None)
    parser.add_argument("--output-markdown", default=None)
    parser.add_argument("--repo-report-dir", default=str(ROOT / "reports"))
    parser.add_argument("--repo-report-prefix", default=None)
    parser.add_argument("--expected-handheld-count", type=int, default=None)
    parser.add_argument("--expected-handheld-scope", choices=HANDHELD_SCOPES, default="all")
    args = parser.parse_args()

    batch_manifest_path = Path(args.batch_manifest)
    batch_manifest = load_json(batch_manifest_path)
    identity_path = Path(args.product_identity) if args.product_identity else default_identity_path(batch_manifest)
    final_prompt_index_path = Path(args.final_prompt_index) if args.final_prompt_index else default_final_prompt_index(batch_manifest)
    job_manifest_path = Path(args.job_manifest) if args.job_manifest else default_job_manifest(batch_manifest)
    output_report = Path(args.output_report) if args.output_report else default_external_report(batch_manifest)
    output_markdown = Path(args.output_markdown) if args.output_markdown else output_report.with_suffix(".md")
    repo_report_dir = Path(args.repo_report_dir) if args.repo_report_dir else None

    report = build_report(
        batch_manifest_path=batch_manifest_path,
        identity_path=identity_path,
        final_prompt_index_path=final_prompt_index_path,
        job_manifest_path=job_manifest_path,
        compiler_path=Path(args.compiler),
        expected_handheld_count=args.expected_handheld_count,
        expected_handheld_scope=args.expected_handheld_scope,
    )
    output_report, output_markdown, repo_json_path, repo_md_path = write_report_files(
        report=report,
        output_report=output_report,
        output_markdown=output_markdown,
        repo_report_dir=repo_report_dir,
        repo_report_prefix=args.repo_report_prefix,
    )

    summary = {
        "status": report["status"],
        "render_blocked": report["render_blocked"],
        "product_id": report["product_id"],
        "checked_prompt_count": report["checked_prompt_count"],
        "blocking_issue_count": report["blocking_issue_count"],
        "warning_count": report["warning_count"],
        "output_report": str(output_report),
        "output_markdown": str(output_markdown),
        "repo_report": str(repo_json_path) if repo_json_path else None,
        "repo_markdown": str(repo_md_path) if repo_md_path else None,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if report["render_blocked"] else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ScriptError as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False, indent=2))
        raise SystemExit(2)
