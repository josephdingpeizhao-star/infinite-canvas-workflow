from __future__ import annotations

import hashlib
import json
import math
import re
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

from category_recipes import installed_category_metadata, load_category_recipe  # noqa: E402
from codex_dev_downstream import (  # noqa: E402
    DETAIL_REQUIRED_OVERRIDE_FIELDS,
    FINAL_PROMPT_FIELD_SEMANTIC_CONTEXTS,
    MAIN_REQUIRED_OVERRIDE_FIELDS,
    UserConfirmedRequirements,
    build_final_prompt_batch_prompt,
    build_variable_config_prompt,
    stable_json_sha256,
)


# 这些文字与下游执行器的精确字符串合同保持一致；新品类缺任一项都应直接失败。
FIXED_CONTENT_CONTRACT_TERMS = (
    "手持交互声明",
    "动态手持样式参考图调用",
    "本张图不启用手持场景",
    "启用手持场景",
    "静态握持",
    "动态拿起",
    "无，仅动态拿起场景可调用",
    "未提供，不调用",
    "1:1",
    "3:4",
    "约 {height_cm} 厘米",
    "高度约 {height_cm} 厘米",
    "绑定角度槽位",
    "A/B/C 槽位",
    "标准模块归属",
    "尺寸标注信息",
    "尺寸标注图规则",
    "非尺寸标注图",
    "必须明确禁止容量、宽度、直径、重量、材质",
)

# 这些信号不代替字段契约，只用于判断教学正文是否具有可执行边界。
# 候选正文还必须达到杯类同字段蓝本的长度比例；仅写“字段名：请填写”会失败。
SUBSTANTIVE_CONTRACT_SIGNALS = (
    "必须",
    "不得",
    "只能",
    "禁止",
    "固定写入",
    "固定包含",
    "唯一",
    "如果",
    "若",
    "冲突",
    "依据",
    "角度",
    "颜色",
    "尺寸",
    "道具",
    "背景",
    "文字",
    "手持",
    "风格",
    "真实",
    "遮挡",
    "参考图",
    "产品身份",
    "页面任务",
    "主体",
    "信息",
)
FORCE_OR_BOUNDARY_SIGNALS = (
    "必须",
    "不得",
    "只能",
    "禁止",
    "固定写入",
    "固定包含",
)
PLACEHOLDER_TEACHING_VALUES = frozenset(
    {
        "请填写",
        "按要求执行",
        "同上",
        "略",
        "待补充",
        "TBD",
        "参见上文",
    }
)
PLACEHOLDER_TEACHING_PATTERNS = (
    re.compile(r"请填写"),
    re.compile(r"按要求执行"),
    re.compile(r"同上"),
    re.compile(r"待补充(?!信息)"),
    re.compile(r"(?<![A-Za-z])TBD(?![A-Za-z])", re.IGNORECASE),
    re.compile(r"参见上文"),
    re.compile(
        r"(?:^|[：:，,、/；;\s“”\"'‘’])略"
        r"(?=$|[。；;！!？?，,、/\s“”\"'‘’])"
    ),
)
PRODUCTION_NEGATIVE_REFERENCE_TEXT_SHA256 = {
    # categories/盘子/runtime/main.json::plate-main-hf02-required-field-teaching
    "plate-main-hf02-required-field-teaching": (
        "0597ce917bec7c19b6ffe744c0299558833a19f3a0b941863a3e9c531467af69"
    ),
    # categories/盘子/runtime/detail.json::plate-detail-hf02-required-field-teaching
    "plate-detail-hf02-required-field-teaching": (
        "f5af8d14ccae35efbd9fb87865242ffe9f904942b64b980f589a4627934eb4e3"
    ),
    # categories/碗/runtime/main.json::bowl-main-hf02-required-field-teaching
    "bowl-main-hf02-required-field-teaching": (
        "74405c9189150aabbfc5b90f5376e107f29332132eb062403a8dc0bd6f05108b"
    ),
    # categories/碗/runtime/detail.json::bowl-detail-hf02-required-field-teaching
    "bowl-detail-hf02-required-field-teaching": (
        "a27812ccff478f4ba4dad38fe93405a34fca3f369b94cda209d6ad737bc6c6a2"
    ),
}
PRODUCTION_NEGATIVE_REFERENCE_SLICE_LOCATIONS = (
    ("盘子", "main", "plate-main-hf02-required-field-teaching"),
    ("盘子", "detail", "plate-detail-hf02-required-field-teaching"),
    ("碗", "main", "bowl-main-hf02-required-field-teaching"),
    ("碗", "detail", "bowl-detail-hf02-required-field-teaching"),
)
APPROVED_NEGATIVE_REFERENCE_SENTENCE_ALLOWLIST = frozenset(
    {
        "不得在该字段中写“请填写”。",
        "不得把字段写成“同上”。",
        "不得将产品颜色填写为“TBD”。",
        "不得以“参见上文”代替真实值。",
        "不得只写类似“同上”的占位值。",
        "禁止写“请填写”，也不应允许任何占位值。",
        "不得输出“TBD”，不能允许该值进入交付。",
    }
)
NEGATIVE_REFERENCE_REJECTED_CORPUS = (
    "不得省略取值依据，产品颜色：“请填写”。",
    "产品颜色不得省略，取值：“TBD”。",
    "不得遗漏颜色证据，最终值：“同上”。",
    "不得省略取值依据但最终值写“请填写”。",
    "产品颜色不得省略但取值写“TBD”。",
    "不得遗漏颜色证据然而最终值写“同上”。",
    "禁止写“请填写”，但本字段例外可以原样输出该占位值。",
    "不得输出“TBD”，除非资料不足。",
    "不得写“同上”；资料不足时允许照此输出。",
    "禁止写“请填写”，资料不足时可照此输出。",
    "不得输出“TBD”，仅在资料不足时使用该值。",
    "不得写“同上”，若资料不足则保留该占位词。",
    "禁止写“略”，特殊情况准许照此填写。",
)

# 中央主图/详情图提示词位置：codex_dev_downstream.py:1536-1537、1582-1583；
# 中文业务键的精确校验位置：codex_dev_downstream.py:2025-2038。
HANDHELD_SUMMARY_COMMON_KEYS = (
    "实际启用手持数量",
    "未启用手持数量",
    "启用手持配置",
    "是否完全满足用户数量",
)
DETAIL_HANDHELD_CHUNK_SUMMARY_KEYS = (
    "本段手持配额",
    "本段实际启用数量",
    "本段启用手持配置",
)

# 源码内联位置：canvas-bridge/codex_dev_downstream.py:2421。
FINAL_BATCH_TOP_LEVEL_FIELDS = frozenset({"prompts"})

# 最终单项字段在源码第 2456 行仍是内联集合；字段名不再抄写，
# 直接取第 100-104 行的 FINAL_PROMPT_FIELD_SEMANTIC_CONTEXTS。
FINAL_PROMPT_ITEM_FIELDS = frozenset(FINAL_PROMPT_FIELD_SEMANTIC_CONTEXTS)

# 源码内联精确值位置：
# - 画布比例：1927、2120-2122、2452-2484；
# - 手持声明与动态参考：1932-1943、2125-2138；
# - 详情模块：1945-1953、2139-2147。
INLINE_EXACT_FIELD_TERMS = {
    "main": {
        "输出画布比例": ("1:1",),
        "手持交互声明": (
            "本张图不启用手持场景",
            "启用手持场景",
            "静态握持",
            "动态拿起",
        ),
        "动态手持样式参考图调用": (
            "无",
            "无，仅动态拿起场景可调用",
            "未提供，不调用",
        ),
    },
    "detail": {
        "标准模块归属": tuple(f"模块{number:02d}" for number in range(1, 9)),
        "输出画布比例": ("3:4",),
        "手持交互声明": (
            "本张图不启用手持场景",
            "启用手持场景",
            "静态握持",
            "动态拿起",
        ),
        "动态手持样式参考图调用": (
            "无",
            "无，仅动态拿起场景可调用",
            "未提供，不调用",
        ),
    },
}
CAT06_DUAL_HEIGHT_CLAUSE_PATTERN = re.compile(
    r"【尺寸标注信息】\s*与\s*【尺寸标注图规则】\s*"
    r"(?:都|均)必须包含\s*[“\"]高度约 \{height_cm\} 厘米[”\"]"
)
CAT07_NEGATIVE_RELATION_SUFFIX = re.compile(
    r"^[”\"']?[^。！？!?；;]{0,32}(?:均|都)?(?:仅|只)?"
    r"(?:是|为|属于|作|作为|用作|当作|供)\s*"
    r"(?:错误(?:值|写法|示例)?|禁用(?:值|项|写法|示例)|"
    r"禁止(?:值|项|写法|示例)|反例|示例|参考值|无效值|"
    r"错误示例|错误要求|错误规则)"
)


def _optional_dimension_keys(recipe) -> frozenset[str]:
    dimensions = recipe.form["dimensions"]
    required = {str(key) for key in dimensions["required"]}
    return frozenset(
        str(field["key"])
        for field in dimensions["fields"]
        if str(field["key"]) in {"length_cm", "width_cm"}
        and str(field["key"]) not in required
    )


def _has_positive_required_relation(sentence: str, target: str) -> bool:
    for match in re.finditer(re.escape(target), sentence):
        prefix = sentence[: match.start()]
        local_prefix = re.split(r"[，,:：]", prefix)[-1]
        suffix = sentence[match.end() :]
        if re.search(
            r"(?:"
            r"(?:不得|禁止|严禁|不应|不能|不可|无需|不必|不要|避免|不再)"
            r"[^，,:：。！？!?；;]{0,24}"
            r"|(?:必须|应当|应|要|一律)?\s*"
            r"(?:取消|删除|删去|去掉|移除|停止|撤销|废止|替换)"
            r"(?:执行|使用|采用|写入|写|包含|保留|调用|选择|输出)?"
            r"[^，,:：。！？!?；;]{0,16}"
            r"|(?:必须|应当|一律)\s*不(?:再)?\s*"
            r"(?:执行|使用|采用|写入|写|包含|保留|调用|选择|输出)"
            r"[^，,:：。！？!?；;]{0,16}"
            r")\s*[“\"]?$",
            local_prefix,
        ) or CAT07_NEGATIVE_RELATION_SUFFIX.search(suffix):
            continue
        if re.search(
            r"(?:必须|应当|一律|只能)[^，,:：。！？!?；;]{0,16}$",
            local_prefix,
        ):
            return True
    return False


def _optional_dimension_disambiguation_teaching(recipe, text: str) -> tuple[str, ...]:
    optional_keys = _optional_dimension_keys(recipe)
    if not optional_keys:
        return ()
    width_matches: list[str] = []
    length_matches: list[str] = []
    for raw_line in text.splitlines():
        for raw_sentence in re.split(r"(?<=[。！？!?；;])", raw_line):
            sentence = " ".join(raw_sentence.split())
            if not sentence:
                continue
            width_contract = (
                "宽度" in sentence
                and "同栏" in sentence
                and "禁止另行编造宽度" in sentence
                and any(marker in sentence for marker in ("不得删除", "逐字保留"))
                and re.search(
                    r"(?:如|若|如果|当)用户已(?:确认|填写)[^，。！？!?；;]*宽度",
                    sentence,
                )
                is not None
                and _has_positive_required_relation(sentence, "同栏明确区分")
            )
            if width_contract and sentence not in width_matches:
                width_matches.append(sentence)
            length_contract = (
                "长度" in sentence
                and "未确认参数" in sentence
                and "削弱" in sentence
                and any(marker in sentence for marker in ("不得删除", "逐字保留"))
                and re.search(
                    r"(?:如|若|如果|当)用户已(?:确认|填写)[^，。！？!?；;]*长度",
                    sentence,
                )
                is not None
                and _has_positive_required_relation(sentence, "逐字保留")
            )
            if length_contract and sentence not in length_matches:
                length_matches.append(sentence)
    if not width_matches or ("length_cm" in optional_keys and not length_matches):
        return ()
    matches = [
        min(width_matches, key=lambda item: len(_normalized_contract_text(item)))
    ]
    if "length_cm" in optional_keys:
        matches.append(
            min(length_matches, key=lambda item: len(_normalized_contract_text(item)))
        )
    return tuple(matches)


def _runtime_text(recipe, stage: str) -> str:
    package = recipe.runtime_packages[f"{stage}_runtime"]
    return "\n".join(str(item.get("text") or "") for item in package["slices"])


def _production_negative_reference_texts() -> dict[str, str]:
    texts: dict[str, str] = {}
    for category_key, stage, slice_id in PRODUCTION_NEGATIVE_REFERENCE_SLICE_LOCATIONS:
        recipe = load_category_recipe(ROOT, category_key)
        matches = [
            str(rule_slice.get("text") or "")
            for rule_slice in recipe.runtime_packages[f"{stage}_runtime"]["slices"]
            if rule_slice.get("slice_id") == slice_id
        ]
        if len(matches) == 1:
            texts[slice_id] = matches[0]
    return texts


def _stage_text(recipe, stage: str) -> str:
    return "\n".join((recipe.prompts[f"{stage}_prompt"], _runtime_text(recipe, stage)))


def _normalized_contract_text(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _negative_reference_text_is_allowlisted(text: str) -> bool:
    """只豁免完整批准语料或四份生产教学正文的固定哈希。"""

    normalized_text = _normalized_contract_text(text)
    if normalized_text in APPROVED_NEGATIVE_REFERENCE_SENTENCE_ALLOWLIST:
        return True
    digest = hashlib.sha256(normalized_text.encode("utf-8")).hexdigest()
    return digest in PRODUCTION_NEGATIVE_REFERENCE_TEXT_SHA256.values()


def _has_placeholder_teaching(text: str) -> bool:
    """识别短占位和伪装成长规则的占位值，只保留明确的安全豁免。"""

    negative_reference_allowlisted = _negative_reference_text_is_allowlisted(text)
    for raw_line in text.splitlines() or [text]:
        line = raw_line.strip()
        if not line:
            continue
        line = re.sub(r"^(?:[-*+]|\d+[.、])\s*", "", line)
        stripped_line = line.strip(" \t。；;！!？?\"'“”‘’")
        if stripped_line in PLACEHOLDER_TEACHING_VALUES:
            return True

        # “待补充信息”是业务流程中的真实状态名，不等于“待补充”占位值。
        searchable_line = line.replace("待补充信息", "")
        for pattern in PLACEHOLDER_TEACHING_PATTERNS:
            for match in pattern.finditer(searchable_line):
                if not negative_reference_allowlisted:
                    return True
    return False


def _field_teaching_evidence(recipe, stage: str, field: str) -> str:
    fields = (
        MAIN_REQUIRED_OVERRIDE_FIELDS
        if stage == "main"
        else DETAIL_REQUIRED_OVERRIDE_FIELDS
    )
    required_headings = {f"【{item}】" for item in fields}
    heading = f"【{field}】"
    candidates: list[str] = []
    slices = recipe.runtime_packages[f"{stage}_runtime"]["slices"]
    for rule_slice in slices:
        text = str(rule_slice.get("text") or "")
        lines = text.splitlines()
        for index, line in enumerate(lines):
            if line.strip() != heading:
                continue
            body: list[str] = []
            for following in lines[index + 1 :]:
                if following.strip() in required_headings:
                    break
                body.append(following)
            candidates.append("\n".join(body).strip())

    if not candidates:
        for rule_slice in slices:
            for line in str(rule_slice.get("text") or "").splitlines():
                if field in line:
                    candidates.append(line.strip())

    evidence = max(
        candidates,
        key=lambda item: len(_normalized_contract_text(item)),
        default="",
    )

    # CAT-06/CAT-07 的跨栏要求可以由同一详情运行包中的契约切片集中教学。
    # 将这些跨字段正文去重后同时计入两栏证据，避免切片位置不同导致相对长度误判。
    if (
        stage == "detail"
        and field in {"尺寸标注信息", "尺寸标注图规则"}
        and evidence
    ):
        match = CAT06_DUAL_HEIGHT_CLAUSE_PATTERN.search(_stage_text(recipe, stage))
        if match is not None and match.group(0) not in evidence:
            evidence = f"{evidence}\n{match.group(0)}"
        for clause in _optional_dimension_disambiguation_teaching(
            recipe,
            _stage_text(recipe, stage),
        ):
            if clause not in evidence:
                evidence = f"{evidence}\n{clause}"

    return evidence


def _substantive_teaching_problems(blueprint: str, candidate: str) -> list[str]:
    normalized_blueprint = _normalized_contract_text(blueprint)
    normalized_candidate = _normalized_contract_text(candidate)
    problems: list[str] = []
    minimum_length = max(24, math.ceil(len(normalized_blueprint) * 0.6))
    if len(normalized_candidate) < minimum_length:
        problems.append(
            f"正文过短：{len(normalized_candidate)} < 蓝本下限 {minimum_length}"
        )

    blueprint_signals = {
        signal for signal in SUBSTANTIVE_CONTRACT_SIGNALS if signal in blueprint
    }
    candidate_signals = {
        signal for signal in SUBSTANTIVE_CONTRACT_SIGNALS if signal in candidate
    }
    minimum_signals = math.ceil(len(blueprint_signals) * 0.55)
    if len(candidate_signals) < minimum_signals:
        problems.append(
            f"约束信号不足：{len(candidate_signals)} < 蓝本下限 {minimum_signals}"
        )

    if any(signal in blueprint for signal in FORCE_OR_BOUNDARY_SIGNALS) and not any(
        signal in candidate for signal in FORCE_OR_BOUNDARY_SIGNALS
    ):
        problems.append("缺少必须/不得/只能/禁止等执行边界")
    for fixed_marker in ("固定写入", "固定包含"):
        if fixed_marker in blueprint and fixed_marker not in candidate:
            problems.append(f"缺少蓝本标记：{fixed_marker}")
    if _has_placeholder_teaching(candidate):
        problems.append("疑似占位式教学")
    return problems


def _requirements_for_category(recipe) -> UserConfirmedRequirements:
    required_dimensions = set(recipe.form["dimensions"]["required"])
    return UserConfirmedRequirements(
        product_type=recipe.product_noun,
        length_cm=18 if "length_cm" in required_dimensions else None,
        width_cm=16 if "width_cm" in required_dimensions else None,
        height_cm=8,
        main_image_count=1,
        detail_image_count=1,
        handheld_main=0,
        handheld_detail=0,
        allow_clear_water=False,
        forbid_pouring_and_heating=True,
        missing_d_no_retake=True,
        category=recipe.key,
        recipe=recipe,
    )


def _final_stage_prompt(recipe) -> str:
    product_id = f"{recipe.key}_字段合同测试"
    requirements = _requirements_for_category(recipe)
    common = {"已确认高度": "约 8 厘米"}
    overrides = {
        "绑定角度槽位": "A 槽位，唯一绑定源图 img_001",
        "手持交互声明": "本张图不启用手持场景",
    }
    resolved = dict(common)
    resolved.update(overrides)
    variable_config = {
        "product_id": product_id,
        "artifact_type": "main_variable_config",
        "config_count": 1,
        "common_constraints": common,
        "configs": [
            {
                "config_id": "main_01",
                "output_type": "main",
                "per_image_overrides": overrides,
                "resolved_variable_config_sha256": stable_json_sha256(resolved),
                "notes": "字段合同测试",
            }
        ],
    }
    return build_final_prompt_batch_prompt(
        mode="main",
        product_id=product_id,
        repository_root=ROOT,
        identity={"artifact_type": "product_identity_archive"},
        style_master={"artifact_type": "style_master"},
        angle_inventory={
            "angle_slots": [
                {
                    "source_asset_id": "img_001",
                    "angle_slot": "A",
                    "admission_result": "合格，可进入对应槽位",
                }
            ],
            "missing_angle_slots": ["D"],
        },
        variable_config=variable_config,
        requirements=requirements,
    )


def _variable_config_stage_prompt(recipe, mode: str) -> str:
    requirements = _requirements_for_category(recipe)
    return build_variable_config_prompt(
        mode=mode,
        product_id=f"{recipe.key}_手持数量说明测试",
        repository_root=ROOT,
        identity={"artifact_type": "product_identity_archive"},
        style_master={"artifact_type": "style_master"},
        angle_inventory={
            "angle_slots": [
                {
                    "source_asset_id": "img_001",
                    "angle_slot": "A",
                    "admission_result": "合格，可进入对应槽位",
                }
            ],
            "missing_angle_slots": ["D"],
        },
        requirements=requirements,
        main_variable_config={} if mode == "detail" else None,
    )


class CategoryExecutorContentContractTest(unittest.TestCase):
    def test_cat07_evidence_rejects_cancelled_optional_dimension_rules(self) -> None:
        recipe = load_category_recipe(ROOT, "杯类")
        valid_width = (
            "如用户已确认宽度，“宽度”禁止项不得删除该已确认宽度，"
            "必须在同栏明确区分已确认宽度与“禁止另行编造宽度”；"
        )
        valid_length = (
            "如用户已确认长度，该长度同理必须逐字保留，"
            "不得被“未确认参数”禁止句削弱。"
        )
        cases = {
            "negated-width": (
                "如用户已确认宽度，“宽度”禁止项不得删除该已确认宽度，"
                "不得同栏明确区分已确认宽度与“禁止另行编造宽度”；"
                + valid_length
            ),
            "cancelled-width": (
                "如用户已确认宽度，“宽度”禁止项不得删除该已确认宽度，"
                "必须取消执行同栏明确区分已确认宽度与“禁止另行编造宽度”；"
                + valid_length
            ),
            "cancelled-length": (
                valid_width
                + "如用户已确认长度，该长度必须取消使用逐字保留，"
                "不得被“未确认参数”禁止句削弱。"
            ),
            "example-width": (
                "如用户已确认宽度，“宽度”禁止项不得删除该已确认宽度，"
                "必须在同栏明确区分已确认宽度与“禁止另行编造宽度”，"
                "本条仅作示例；"
                + valid_length
            ),
        }
        for label, teaching in cases.items():
            with self.subTest(case=label):
                self.assertEqual(
                    (),
                    _optional_dimension_disambiguation_teaching(recipe, teaching),
                )

        self.assertEqual(
            2,
            len(
                _optional_dimension_disambiguation_teaching(
                    recipe,
                    valid_width + valid_length,
                )
            ),
        )

    def test_every_installed_category_teaches_imported_required_fields_in_consuming_stage(
        self,
    ) -> None:
        category_keys = [item["key"] for item in installed_category_metadata(ROOT)]

        for category_key in category_keys:
            with self.subTest(category=category_key):
                recipe = load_category_recipe(ROOT, category_key)
                main = _stage_text(recipe, "main")
                detail = _stage_text(recipe, "detail")
                final = _final_stage_prompt(recipe)

                self.assertEqual(
                    [],
                    [field for field in MAIN_REQUIRED_OVERRIDE_FIELDS if field not in main],
                    f"品类“{category_key}”主图阶段缺少执行器必填字段教学",
                )
                self.assertEqual(
                    [],
                    [
                        field
                        for field in DETAIL_REQUIRED_OVERRIDE_FIELDS
                        if field not in detail
                    ],
                    f"品类“{category_key}”详情阶段缺少执行器必填字段教学",
                )
                self.assertEqual(
                    [],
                    [field for field in FINAL_PROMPT_ITEM_FIELDS if field not in final],
                    f"品类“{category_key}”最终阶段缺少中央三字段教学",
                )
                self.assertTrue(
                    all(field in final for field in FINAL_BATCH_TOP_LEVEL_FIELDS),
                    f"品类“{category_key}”最终阶段缺少批次包装字段",
                )

    def test_every_required_field_has_substantive_runtime_value_specification(
        self,
    ) -> None:
        category_keys = [item["key"] for item in installed_category_metadata(ROOT)]
        blueprint = load_category_recipe(ROOT, "杯类")

        for category_key in category_keys:
            recipe = load_category_recipe(ROOT, category_key)
            for stage, fields in (
                ("main", MAIN_REQUIRED_OVERRIDE_FIELDS),
                ("detail", DETAIL_REQUIRED_OVERRIDE_FIELDS),
            ):
                stage_text = _stage_text(recipe, stage)
                for field in fields:
                    with self.subTest(
                        category=category_key,
                        stage=stage,
                        field=field,
                    ):
                        blueprint_evidence = _field_teaching_evidence(
                            blueprint,
                            stage,
                            field,
                        )
                        candidate_evidence = _field_teaching_evidence(
                            recipe,
                            stage,
                            field,
                        )
                        self.assertTrue(
                            blueprint_evidence,
                            f"杯类蓝本无法定位字段教学：{stage}/{field}",
                        )
                        self.assertTrue(
                            candidate_evidence,
                            f"品类“{category_key}”运行规则缺少字段正文：{stage}/{field}",
                        )
                        self.assertEqual(
                            [],
                            _substantive_teaching_problems(
                                blueprint_evidence,
                                candidate_evidence,
                            ),
                            (
                                f"品类“{category_key}”存在占位或残缺字段教学："
                                f"{stage}/{field}"
                            ),
                        )
                        for exact_term in INLINE_EXACT_FIELD_TERMS.get(
                            stage,
                            {},
                        ).get(field, ()):
                            exact_scope = (
                                candidate_evidence
                                if field
                                in {
                                    "手持交互声明",
                                    "动态手持样式参考图调用",
                                }
                                else stage_text
                            )
                            self.assertIn(
                                exact_term,
                                exact_scope,
                                (
                                    f"品类“{category_key}”字段精确值缺失："
                                    f"{stage}/{field}/{exact_term}"
                                ),
                            )

    def test_placeholder_field_copy_fails_the_substance_gate(self) -> None:
        blueprint = load_category_recipe(ROOT, "杯类")
        blueprint_evidence = _field_teaching_evidence(
            blueprint,
            "main",
            "产品颜色依据",
        )

        problems = _substantive_teaching_problems(
            blueprint_evidence,
            "产品颜色依据：请填写，并按要求执行。",
        )

        self.assertTrue(problems)
        self.assertTrue(
            any("正文过短" in problem or "占位式教学" in problem for problem in problems)
        )

    def test_short_placeholder_sentences_and_field_values_are_detected(self) -> None:
        candidates = (
            "同上。",
            "略",
            "待补充",
            "产品颜色依据：请填写，并按要求执行。",
            "【绑定角度槽位】：同上",
            "产品颜色依据：请填写并按要求执行。",
            "产品颜色依据：请填写或待补充。",
            "产品颜色依据：TBD。",
            "产品颜色依据：参见上文。",
        )

        for candidate in candidates:
            with self.subTest(candidate=candidate):
                self.assertTrue(_has_placeholder_teaching(candidate))

    def test_placeholder_words_inside_real_rules_are_not_misclassified(self) -> None:
        candidates = (
            "教学步骤不得省略，必须逐项写明取值依据与冲突裁定。",
            "不得省略任何字段，必须逐项写明取值依据与冲突裁定。",
            "请忽略旧版字段，必须使用当批确认值。",
            "不得填写“待补充信息”，资料不足时必须先阻断并请求确认。",
            "资料不足时写明待补充信息，并在交付前阻断。",
        )

        for candidate in candidates:
            with self.subTest(candidate=candidate):
                self.assertFalse(_has_placeholder_teaching(candidate))

    def test_explicit_negative_placeholder_references_are_exempt(self) -> None:
        for candidate in APPROVED_NEGATIVE_REFERENCE_SENTENCE_ALLOWLIST:
            with self.subTest(candidate=candidate):
                self.assertFalse(_has_placeholder_teaching(candidate))

    def test_actual_production_negative_placeholder_texts_are_hash_allowlisted(
        self,
    ) -> None:
        texts = _production_negative_reference_texts()
        self.assertEqual(
            set(PRODUCTION_NEGATIVE_REFERENCE_TEXT_SHA256),
            set(texts),
        )
        for slice_id, candidate in texts.items():
            digest = hashlib.sha256(
                _normalized_contract_text(candidate).encode("utf-8")
            ).hexdigest()
            with self.subTest(slice_id=slice_id):
                self.assertEqual(
                    PRODUCTION_NEGATIVE_REFERENCE_TEXT_SHA256[slice_id],
                    digest,
                )
                self.assertFalse(_has_placeholder_teaching(candidate))

    def test_negative_reference_bypasses_remain_placeholder_teaching(
        self,
    ) -> None:
        for candidate in NEGATIVE_REFERENCE_REJECTED_CORPUS:
            with self.subTest(candidate=candidate):
                self.assertTrue(_has_placeholder_teaching(candidate))

    def test_appending_permission_clause_revokes_every_negative_exemption(
        self,
    ) -> None:
        exempt_texts = {
            **_production_negative_reference_texts(),
            **{
                f"approved-{index}": candidate
                for index, candidate in enumerate(
                    APPROVED_NEGATIVE_REFERENCE_SENTENCE_ALLOWLIST,
                    start=1,
                )
            },
        }
        for text_id, candidate in exempt_texts.items():
            mutations = (
                candidate + "资料不足时允许照此输出。",
                candidate.removesuffix("。") + "，资料不足时允许照此输出。",
                candidate.removesuffix("。") + "；资料不足时允许照此输出。",
                candidate + "\n资料不足时允许照此输出。",
            )
            for mutated in mutations:
                with self.subTest(text_id=text_id, suffix=mutated[-20:]):
                    self.assertTrue(_has_placeholder_teaching(mutated))

    def test_long_placeholder_copy_cannot_pass_by_piling_up_constraint_words(
        self,
    ) -> None:
        blueprint = (
            "【产品颜色依据】\n"
            "必须依据原图逐项确认主体颜色、边缘颜色、图案颜色与光线影响，"
            "不得凭空改色；若参考图之间冲突，只能写明证据和冲突裁定，"
            "固定写入可执行颜色结论，禁止使用占位值。"
        )
        candidates = (
            (
                "产品颜色依据：请填写，后续必须依据原图核对颜色、尺寸、角度、"
                "背景、道具、文字、手持、风格、真实感与参考图；不得冲突，"
                "只能写明证据，禁止省略，固定写入最终结论。"
            ),
            (
                "产品颜色依据：请填写并按要求执行；必须核对颜色、尺寸、角度、"
                "主体、背景、道具、文字和参考图，不得冲突，只能依据证据，"
                "禁止省略并固定写入最终结论。"
            ),
            (
                "产品颜色依据：请填写或待补充；必须核对颜色、尺寸、角度、"
                "主体、背景、道具、文字和参考图，不得冲突，只能依据证据，"
                "禁止省略并固定写入最终结论。"
            ),
            (
                "产品颜色依据：TBD；必须核对颜色、尺寸、角度、主体、背景、"
                "道具、文字和参考图，不得冲突，只能依据证据，禁止省略，"
                "固定写入最终结论。"
            ),
            (
                "产品颜色依据：参见上文；必须核对颜色、尺寸、角度、主体、"
                "背景、道具、文字和参考图，不得冲突，只能依据证据，禁止省略，"
                "固定写入最终结论。"
            ),
        )

        for candidate in candidates:
            with self.subTest(candidate=candidate[:24]):
                problems = _substantive_teaching_problems(blueprint, candidate)
                minimum_length = max(
                    24,
                    math.ceil(len(_normalized_contract_text(blueprint)) * 0.6),
                )
                self.assertGreaterEqual(
                    len(_normalized_contract_text(candidate)),
                    minimum_length,
                )
                self.assertLess(
                    len(_normalized_contract_text(candidate)),
                    minimum_length * 2,
                )
                self.assertFalse(
                    any(
                        "正文过短" in problem or "约束信号不足" in problem
                        for problem in problems
                    ),
                    problems,
                )
                self.assertTrue(
                    any("占位式教学" in problem for problem in problems),
                    problems,
                )

    def test_placeholder_copy_still_fails_beyond_twice_the_blueprint_floor(
        self,
    ) -> None:
        blueprint = (
            "【产品颜色依据】\n"
            "必须依据原图确认主体颜色与图案颜色，不得凭空改色；"
            "若参考图冲突，只能写明证据和裁定，固定写入可执行结论。"
        )
        real_rule_padding = (
            "必须核对颜色、尺寸、角度、主体、背景、道具、文字、手持、风格、"
            "真实感与参考图，不得制造冲突，只能依据证据裁定，禁止省略并固定写入。"
        )
        candidate = (
            "产品颜色依据：请填写，并按要求执行。"
            + real_rule_padding * 5
        )

        problems = _substantive_teaching_problems(blueprint, candidate)
        minimum_length = max(
            24,
            math.ceil(len(_normalized_contract_text(blueprint)) * 0.6),
        )

        self.assertGreater(
            len(_normalized_contract_text(candidate)),
            minimum_length * 2,
        )
        self.assertFalse(
            any(
                "正文过短" in problem or "约束信号不足" in problem
                for problem in problems
            ),
            problems,
        )
        self.assertTrue(
            any("占位式教学" in problem for problem in problems),
            problems,
        )

    def test_variable_config_prompts_teach_exact_handheld_summary_business_keys(
        self,
    ) -> None:
        category_keys = [item["key"] for item in installed_category_metadata(ROOT)]

        for category_key in category_keys:
            recipe = load_category_recipe(ROOT, category_key)
            for mode, scope in (("main", "主图"), ("detail", "详情图")):
                with self.subTest(category=category_key, mode=mode):
                    prompt = _variable_config_stage_prompt(recipe, mode)
                    if mode == "main":
                        summary_key = "handheld_count_summary"
                        expected_keys = (
                            f"用户要求{scope}手持数量",
                            *HANDHELD_SUMMARY_COMMON_KEYS,
                        )
                    else:
                        summary_key = "handheld_chunk_summary"
                        expected_keys = DETAIL_HANDHELD_CHUNK_SUMMARY_KEYS
                    self.assertIn(summary_key, prompt)
                    for key in expected_keys:
                        self.assertIn(
                            key,
                            prompt,
                            f"品类“{category_key}”{scope}提示词缺少精确业务键：{key}",
                        )

    def test_every_installed_category_teaches_all_fixed_content_contract_terms(self) -> None:
        category_keys = [item["key"] for item in installed_category_metadata(ROOT)]

        for category_key in category_keys:
            with self.subTest(category=category_key):
                recipe = load_category_recipe(ROOT, category_key)
                archive = json.dumps(
                    {
                        "lexicons": recipe.lexicons,
                        "prompts": recipe.prompts,
                        "runtime": recipe.runtime_packages,
                    },
                    ensure_ascii=False,
                )
                missing = [
                    term for term in FIXED_CONTENT_CONTRACT_TERMS if term not in archive
                ]
                self.assertEqual(
                    [],
                    missing,
                    f"品类“{category_key}”缺少执行器内容合同教学：{missing}",
                )
                for module_number in range(1, 9):
                    module = f"模块{module_number:02d}"
                    self.assertIn(
                        module,
                        archive,
                        f"品类“{category_key}”缺少{module}教学",
                    )

    def test_every_installed_category_teaches_contracts_in_the_consuming_stage(self) -> None:
        category_keys = [item["key"] for item in installed_category_metadata(ROOT)]

        for category_key in category_keys:
            with self.subTest(category=category_key):
                recipe = load_category_recipe(ROOT, category_key)
                main = _stage_text(recipe, "main")
                detail = _stage_text(recipe, "detail")
                final = _stage_text(recipe, "final")

                for term in (
                    "1:1",
                    "手持交互声明",
                    "动态手持样式参考图调用",
                    "本张图不启用手持场景",
                    "无，仅动态拿起场景可调用",
                    "未提供，不调用",
                ):
                    self.assertIn(term, main, f"品类“{category_key}”主图阶段缺少：{term}")

                for term in (
                    "3:4",
                    "标准模块归属",
                    "高度约 {height_cm} 厘米",
                    "尺寸标注信息",
                    "尺寸标注图规则",
                    "非尺寸标注图",
                    "手持交互声明",
                    "动态手持样式参考图调用",
                ):
                    self.assertIn(term, detail, f"品类“{category_key}”详情阶段缺少：{term}")

                for term in (
                    "{expected_ratio}",
                    "约 {height_cm} 厘米",
                    "A/B/C 槽位",
                    "手持启用或禁用状态",
                ):
                    self.assertIn(term, final, f"品类“{category_key}”最终阶段缺少：{term}")


if __name__ == "__main__":
    unittest.main()
