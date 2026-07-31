from __future__ import annotations

import hashlib
import json
import re
import unittest
from collections.abc import Mapping, Sequence
from pathlib import Path

try:
    from .test_category_executor_content_contract import (
        ROOT,
        _substantive_teaching_problems,
        installed_category_metadata,
        load_category_recipe,
    )
except ImportError:
    from test_category_executor_content_contract import (  # type: ignore[no-redef]
        ROOT,
        _substantive_teaching_problems,
        installed_category_metadata,
        load_category_recipe,
    )


RUNTIME_STAGES = ("main", "detail", "final", "qc")
BLUEPRINT_TAG_PATTERN = re.compile(r"^blueprint:([^/]+)/([^/]+)$")
CUP_CATEGORY_ONLY_TERMS = (
    "壶",
    "杯",
    "提梁",
    "出水口",
    "直饮结构",
    "滤网",
)
HANDHELD_BLUEPRINT_TOPICS = {
    "main": "main_handheld_enable_rule",
    "detail": "detail_handheld_enable_rule",
}
CAT06_DETAIL_REQUIRED_FIELDS_SLICE_ID = "detail_required_fields_core"
CAT06_DETAIL_REQUIRED_FIELDS_SOURCE = "详情图单张变量配置提示词生成.txt"
CAT06_DUAL_HEIGHT_CLAUSE = (
    "【尺寸标注信息】与【尺寸标注图规则】都必须包含"
    "“高度约 {height_cm} 厘米”"
)
CAT06_RUNTIME_SOURCE_OVERLAY = f"\n{CAT06_DUAL_HEIGHT_CLAUSE}。"
HANDHELD_SUMMARY_DISCIPLINES = {
    "逐项检查 configs 手持声明并以启用手持场景裁定": (
        "实际启用手持数量必须等于逐项检查 configs 中每套【手持交互声明】后，"
        "实际写明“启用手持场景”的配置数量"
    ),
    "启用 ID 全部且仅包含启用项": (
        "启用手持配置必须是配置 ID 列表，"
        "严格按 configs 原始顺序列出全部且仅列出启用手持的配置"
    ),
    "实际数等于用户要求数才允许写是": (
        "只有实际启用数量与用户要求数量完全相等时，"
        "是否完全满足用户数量才允许使用固定字符串“是”"
    ),
}
HANDHELD_SUMMARY_REVERSED_DISCIPLINES = {
    "逐项检查 configs 手持声明并以启用手持场景裁定": (
        "实际启用手持数量可以直接采用预期数量，无需逐项检查 configs 中"
        "每套【手持交互声明】，也无需以“启用手持场景”裁定"
    ),
    "启用 ID 全部且仅包含启用项": (
        "启用手持配置可以省略部分启用项或混入未启用项，无需保持 configs 原始顺序"
    ),
    "实际数等于用户要求数才允许写是": (
        "即使实际启用数量与用户要求数量不相等，"
        "是否完全满足用户数量也允许使用固定字符串“是”"
    ),
}
HANDHELD_SUMMARY_CONFLICT_PATTERNS = {
    "逐项检查 configs 手持声明并以启用手持场景裁定": (
        re.compile(
            r"(?:无需|不必|可以不|允许不)[^。；;\n]{0,32}"
            r"逐项检查\s*configs",
            re.IGNORECASE,
        ),
        re.compile(
            r"实际启用手持数量[^。；;\n]{0,24}"
            r"(?:直接采用|直接等于)预期数量"
        ),
    ),
    "启用 ID 全部且仅包含启用项": (
        re.compile(
            r"(?:可以|允许)[^。；;\n]{0,24}(?:漏列|省略)"
            r"[^。；;\n]{0,16}启用项"
        ),
        re.compile(
            r"(?:可以|允许)[^。；;\n]{0,32}混入未启用项"
        ),
    ),
    "实际数等于用户要求数才允许写是": (
        re.compile(
            r"(?:实际启用(?:手持)?数量|实际数)[^。；;\n]{0,32}"
            r"(?:不相等|不等于)[^。；;\n]{0,50}"
            r"(?:允许|仍可|也可|可以)[^。；;\n]{0,20}"
            r"(?:固定字符串)?[“\"'‘]?是"
        ),
    ),
}

# BR-01 终审文件的 UTF-8 文本基线；CRLF/LF 统一为 LF 后计算。
# HF-03 只能增补运行教学，不得改写这些文件。
BR01_LOCKED_FILE_SHA256 = {
    "categories/盘子/prompts/angle.md": (
        "b5a78dfbd5129ea44980acfc0fc0328e9f351a8c9bf5ce2b041075400882d613"
    ),
    "categories/盘子/prompts/angle_boundary.md": (
        "692ebab8c5abb7e9a13d75938a5d53197a46ecb65244a9dd51db18bbf4c9925b"
    ),
    "categories/盘子/qc/checklist.md": (
        "eac57b3a5ab5fcf5c3cd242381021fb3f6809ae8681ecddc54bba3eb9178fbea"
    ),
    "categories/盘子/qc/realism.md": (
        "13dd3f17730e92443760b766b7460d9f28ce60183138092ac353e303b85ec951"
    ),
    "categories/盘子/qc/workflow.md": (
        "0160c1ffed53d5e961d352315c4d511b0ea86ab59f709e152cfd25b2bb1d9b4a"
    ),
    "categories/碗/prompts/angle.md": (
        "2e6b7eb2f9009dff31b94ceb3ba161f21d5df7ef5ecd89bc01ec9cf2d0569f65"
    ),
    "categories/碗/prompts/angle_boundary.md": (
        "2c27680267fdb48254e4fc135aa75daeadf4f3e3ac1dd77c1a6bf6a9dc5a0a78"
    ),
    "categories/碗/qc/checklist.md": (
        "68f74bb498683200d450c3407c31687c920e7d7989382cb0fbeff367a197425c"
    ),
    "categories/碗/qc/realism.md": (
        "922884e01111ea1778af8b2815ba02b420df10759923141cc9369d5560edbd4e"
    ),
    "categories/碗/qc/workflow.md": (
        "ab4a897bc580d12a4dafe9b8db69ae4b1887917f59b417969ea4224246f2e153"
    ),
}
BOWL_COMBINATION_RULE_SHA256 = (
    "362bf1e453445e3e6a5bc5fa92bad002524db7eadbd7c418cfcc7614698c2f70"
)
BOWL_COMBINATION_RULE_FILES = (
    "categories/碗/prompts/detail.md",
    "categories/碗/prompts/final.md",
    "categories/碗/prompts/identity.md",
    "categories/碗/prompts/main.md",
    "categories/碗/qc/checklist.md",
)
PLATE_NEGATIVE_SPOUT_EXEMPTIONS = {
    "categories/盘子/prompts/identity.md": 1,
    "categories/盘子/qc/checklist.md": 1,
    "categories/盘子/qc/runtime.json": 1,
}


def _runtime_slices(recipe, stage: str) -> list[Mapping[str, object]]:
    return list(recipe.runtime_packages[f"{stage}_runtime"]["slices"])


def _blueprint_tags(rule_slice: Mapping[str, object]) -> list[str]:
    tags = rule_slice.get("tags", ())
    if not isinstance(tags, Sequence) or isinstance(tags, (str, bytes)):
        return []
    return [
        str(tag)
        for tag in tags
        if isinstance(tag, str) and tag.startswith("blueprint:")
    ]


def _alignment_problems(
    blueprint_slices: Sequence[Mapping[str, object]],
    candidate_slices: Sequence[Mapping[str, object]],
    stage: str,
) -> list[str]:
    problems: list[str] = []
    blueprint_by_id: dict[str, Mapping[str, object]] = {}
    for rule_slice in blueprint_slices:
        slice_id = str(rule_slice.get("slice_id") or "")
        if not slice_id:
            problems.append(f"{stage} 杯类蓝本存在空 slice_id")
        elif slice_id in blueprint_by_id:
            problems.append(f"{stage} 杯类蓝本主题重复：{slice_id}")
        else:
            blueprint_by_id[slice_id] = rule_slice

    candidates_by_topic: dict[str, list[Mapping[str, object]]] = {}
    for rule_slice in candidate_slices:
        slice_id = str(rule_slice.get("slice_id") or "<empty>")
        tags = _blueprint_tags(rule_slice)
        if not tags:
            continue
        if len(tags) != 1:
            problems.append(f"{stage}/{slice_id} 存在多个 blueprint 标签：{tags}")
            continue

        match = BLUEPRINT_TAG_PATTERN.fullmatch(tags[0])
        if match is None:
            problems.append(f"{stage}/{slice_id} blueprint 标签格式未知：{tags[0]}")
            continue
        tagged_stage, topic = match.groups()
        if tagged_stage != stage or topic not in blueprint_by_id:
            problems.append(f"{stage}/{slice_id} 指向未知蓝本主题：{tags[0]}")
            continue
        candidates_by_topic.setdefault(topic, []).append(rule_slice)

    for topic, blueprint_slice in blueprint_by_id.items():
        matches = candidates_by_topic.get(topic, [])
        if not matches:
            problems.append(f"{stage} 缺少蓝本主题：{topic}")
            continue
        if len(matches) != 1:
            problems.append(f"{stage} 蓝本主题映射重复：{topic}={len(matches)}")
            continue
        candidate = matches[0]
        for problem in _substantive_teaching_problems(
            str(blueprint_slice.get("text") or ""),
            str(candidate.get("text") or ""),
        ):
            problems.append(
                f"{stage}/{topic}/{candidate.get('slice_id')} 实质教学门禁失败：{problem}"
            )
    return problems


def _normalize_text_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _normalized_utf8_sha256(text: str) -> str:
    return hashlib.sha256(
        _normalize_text_newlines(text).encode("utf-8")
    ).hexdigest()


def _source_range_problems(
    rule_slice: Mapping[str, object],
    *,
    verify_content: bool = True,
) -> list[str]:
    slice_id = str(rule_slice.get("slice_id") or "<empty>")
    source_file = rule_slice.get("source_file")
    line_start = rule_slice.get("line_start")
    line_end = rule_slice.get("line_end")
    if not isinstance(source_file, str) or not source_file.strip():
        return [f"{slice_id} 缺少 source_file"]
    if (
        not isinstance(line_start, int)
        or isinstance(line_start, bool)
        or not isinstance(line_end, int)
        or isinstance(line_end, bool)
        or line_start < 1
        or line_end < line_start
    ):
        return [f"{slice_id} 行范围无效：{line_start}-{line_end}"]

    source_path = (ROOT / source_file).resolve()
    try:
        source_path.relative_to(ROOT.resolve())
    except ValueError:
        return [f"{slice_id} source_file 越出仓库：{source_file}"]
    if not source_path.is_file():
        return [f"{slice_id} source_file 不存在：{source_file}"]

    source_text = source_path.read_bytes().decode("utf-8")
    source_lines = _normalize_text_newlines(source_text).splitlines()
    line_count = len(source_lines)
    if line_end > line_count:
        return [f"{slice_id} 行范围越界：{line_end}>{line_count}"]
    if not verify_content:
        return []

    ranged_text = "\n".join(source_lines[line_start - 1 : line_end])
    expected_text = _normalize_text_newlines(str(rule_slice.get("text") or ""))

    # CAT-06 只在杯类运行教学上叠加一条由详情 prompt 同步背书的精确句子；
    # 锁定蓝本保持只读。除这一个逐字匹配的插入外，仍按原规则逐字校验源范围。
    if (
        slice_id == CAT06_DETAIL_REQUIRED_FIELDS_SLICE_ID
        and source_file == CAT06_DETAIL_REQUIRED_FIELDS_SOURCE
    ):
        if expected_text.count(CAT06_RUNTIME_SOURCE_OVERLAY) != 1:
            return [f"{slice_id} CAT-06 双栏高度教学叠加必须恰好出现一次"]
        prompt_text = (ROOT / "categories" / "杯类" / "prompts" / "detail.md").read_text(
            encoding="utf-8"
        )
        if prompt_text.count(CAT06_DUAL_HEIGHT_CLAUSE) != 1:
            return [f"{slice_id} CAT-06 双栏高度教学缺少 prompt 背书"]
        expected_text = expected_text.replace(CAT06_RUNTIME_SOURCE_OVERLAY, "", 1)

    runtime_matches: list[Mapping[str, object]] = []
    if source_path.suffix.lower() == ".json":
        try:
            source_document = json.loads(source_text)
        except json.JSONDecodeError:
            source_document = None
        if isinstance(source_document, Mapping):
            source_slices = source_document.get("slices")
            if isinstance(source_slices, Sequence) and not isinstance(
                source_slices,
                (str, bytes),
            ):
                runtime_matches = [
                    item
                    for item in source_slices
                    if isinstance(item, Mapping)
                    and str(item.get("slice_id") or "") == slice_id
                ]

    if runtime_matches:
        exact_id_pattern = re.compile(
            r'"slice_id"\s*:\s*'
            + re.escape(json.dumps(slice_id, ensure_ascii=False))
        )
        if exact_id_pattern.search(ranged_text) is None:
            return [f"{slice_id} 自指范围未包含准确 slice_id"]
        text_match = re.search(
            r'"text"\s*:\s*("(?:\\.|[^"\\])*")',
            ranged_text,
            re.DOTALL,
        )
        if text_match is None:
            return [f"{slice_id} 自指范围无法定位 text 并还原正文"]
        try:
            restored_text = json.loads(text_match.group(1))
        except json.JSONDecodeError:
            return [f"{slice_id} 自指范围中的 text 不是有效 JSON 字符串"]
        if _normalize_text_newlines(restored_text) != expected_text:
            return [f"{slice_id} 自指范围还原正文与 slice.text 不一致"]
        return []

    if _normalize_text_newlines(ranged_text) != expected_text:
        return [f"{slice_id} 普通文本源范围与 slice.text 不一致"]
    return []


def _mapped_slices(recipe, stage: str) -> list[Mapping[str, object]]:
    return [
        rule_slice
        for rule_slice in _runtime_slices(recipe, stage)
        if _blueprint_tags(rule_slice)
    ]


def _cup_category_only_term_hits(text: str) -> list[str]:
    return [term for term in CUP_CATEGORY_ONLY_TERMS if term in text]


def _bowl_combination_rule_block(path: Path) -> str:
    text = path.read_bytes().decode("utf-8")
    lines = _normalize_text_newlines(text).splitlines()
    try:
        start = lines.index("【组合单元硬约束】")
    except ValueError as exc:
        raise AssertionError(f"{path} 缺少组合单元硬约束") from exc
    return "\n".join(lines[start : start + 4])


def _handheld_summary_discipline_problems(text: str, stage: str) -> list[str]:
    scope = "主图" if stage == "main" else "详情图"
    expected_phrases = {
        "结构化对象名": "handheld_count_summary",
        "用户要求数量键": f"用户要求{scope}手持数量",
        "实际启用数量键": "实际启用手持数量",
        "未启用数量键": "未启用手持数量",
        "启用配置键": "启用手持配置",
        "完全满足键": "是否完全满足用户数量",
        "用户预期对应纪律": "预期数量",
        "配置原始顺序": "configs 原始顺序",
        "启用列表长度纪律": "列表长度必须等于实际启用手持数量",
        "未启用数量算法": "配置总数减去实际启用手持数量",
        "完全满足固定值": "固定字符串“是”",
        "无法满足时阻断": "必须在交付前阻断",
        "禁止静默改默认值": "不得静默把用户要求改成默认值",
        **HANDHELD_SUMMARY_DISCIPLINES,
    }
    problems = [
        f"{label}缺失：{phrase}"
        for label, phrase in expected_phrases.items()
        if phrase not in text
    ]
    for label, patterns in HANDHELD_SUMMARY_CONFLICT_PATTERNS.items():
        if any(pattern.search(text) for pattern in patterns):
            problems.append(f"{label}存在反向冲突语句")
    return problems


class CategoryRuntimeSliceAlignmentTest(unittest.TestCase):
    def test_every_installed_category_maps_every_cup_slice_once_and_substantively(
        self,
    ) -> None:
        blueprint = load_category_recipe(ROOT, "杯类")
        category_keys = [item["key"] for item in installed_category_metadata(ROOT)]

        for stage in RUNTIME_STAGES:
            blueprint_slices = _runtime_slices(blueprint, stage)
            for rule_slice in blueprint_slices:
                with self.subTest(category="杯类", stage=stage, source=rule_slice.get("slice_id")):
                    self.assertEqual([], _source_range_problems(rule_slice))

            for category_key in category_keys:
                if category_key == "杯类":
                    continue
                recipe = load_category_recipe(ROOT, category_key)
                candidate_slices = _runtime_slices(recipe, stage)
                with self.subTest(category=category_key, stage=stage, alignment=True):
                    self.assertEqual(
                        [],
                        _alignment_problems(blueprint_slices, candidate_slices, stage),
                    )
                for rule_slice in candidate_slices:
                    with self.subTest(
                        category=category_key,
                        stage=stage,
                        source=rule_slice.get("slice_id"),
                        bounds=True,
                    ):
                        self.assertEqual(
                            [],
                            _source_range_problems(
                                rule_slice,
                                verify_content=False,
                            ),
                        )
                for rule_slice in _mapped_slices(recipe, stage):
                    with self.subTest(
                        category=category_key,
                        stage=stage,
                        source=rule_slice.get("slice_id"),
                        content=True,
                    ):
                        self.assertEqual([], _source_range_problems(rule_slice))

    def test_missing_blueprint_topic_fails_alignment(self) -> None:
        blueprint = [{"slice_id": "topic", "text": "必须逐项执行并说明依据。"}]

        problems = _alignment_problems(blueprint, [], "main")

        self.assertTrue(any("缺少蓝本主题" in problem for problem in problems))

    def test_duplicate_blueprint_topic_fails_alignment(self) -> None:
        blueprint = [{"slice_id": "topic", "text": "必须逐项执行并说明依据。"}]
        candidate = {
            "slice_id": "candidate",
            "tags": ["blueprint:main/topic"],
            "text": "必须逐项执行并说明依据，禁止省略证据。",
        }

        problems = _alignment_problems(blueprint, [candidate, dict(candidate)], "main")

        self.assertTrue(any("映射重复" in problem for problem in problems))

    def test_unknown_and_multi_blueprint_tags_fail_alignment(self) -> None:
        blueprint = [{"slice_id": "topic", "text": "必须逐项执行并说明依据。"}]
        candidates = [
            {
                "slice_id": "unknown",
                "tags": ["blueprint:main/not-a-topic"],
                "text": "必须逐项执行并说明依据。",
            },
            {
                "slice_id": "multi",
                "tags": ["blueprint:main/topic", "blueprint:main/not-a-topic"],
                "text": "必须逐项执行并说明依据。",
            },
        ]

        problems = _alignment_problems(blueprint, candidates, "main")

        self.assertTrue(any("未知蓝本主题" in problem for problem in problems))
        self.assertTrue(any("多个 blueprint 标签" in problem for problem in problems))

    def test_source_range_shifted_by_one_line_fails_content_location(self) -> None:
        recipe = load_category_recipe(ROOT, "盘子")
        candidates: list[Mapping[str, object]] = []
        for rule_slice in _mapped_slices(recipe, "main"):
            source_file = rule_slice.get("source_file")
            line_start = rule_slice.get("line_start")
            slice_id = str(rule_slice.get("slice_id") or "")
            if not isinstance(source_file, str) or not isinstance(line_start, int):
                continue
            source_path = ROOT / source_file
            if source_path.suffix.lower() != ".json":
                continue
            source_lines = _normalize_text_newlines(
                source_path.read_bytes().decode("utf-8")
            ).splitlines()
            if line_start <= len(source_lines) and slice_id in source_lines[line_start - 1]:
                candidates.append(rule_slice)

        self.assertTrue(candidates, "缺少可用于 line_start 错位注入的 runtime 自指映射片")
        source_slice = candidates[0]
        self.assertEqual([], _source_range_problems(source_slice))

        shifted_slice = dict(source_slice)
        shifted_slice["line_start"] = int(source_slice["line_start"]) + 1
        problems = _source_range_problems(shifted_slice)

        self.assertTrue(problems)
        self.assertTrue(
            any("自指范围未包含准确 slice_id" in problem for problem in problems),
            problems,
        )

    def test_placeholder_mapping_fails_substantive_gate(self) -> None:
        blueprint = [
            {
                "slice_id": "topic",
                "text": "必须逐项写明产品身份、角度、颜色、尺寸和证据依据，不得省略。",
            }
        ]
        candidates = [
            {
                "slice_id": "placeholder",
                "tags": ["blueprint:main/topic"],
                "text": "同上",
            }
        ]

        problems = _alignment_problems(blueprint, candidates, "main")

        self.assertTrue(any("实质教学门禁失败" in problem for problem in problems))
        self.assertTrue(any("占位式教学" in problem for problem in problems))

    def test_mapped_slices_contain_no_cup_category_only_terms(self) -> None:
        category_keys = [item["key"] for item in installed_category_metadata(ROOT)]

        for category_key in category_keys:
            if category_key == "杯类":
                continue
            recipe = load_category_recipe(ROOT, category_key)
            for stage in RUNTIME_STAGES:
                for rule_slice in _mapped_slices(recipe, stage):
                    text = str(rule_slice.get("text") or "")
                    with self.subTest(
                        category=category_key,
                        stage=stage,
                        slice_id=rule_slice.get("slice_id"),
                    ):
                        self.assertEqual(
                            [],
                            _cup_category_only_term_hits(text),
                        )

    def test_cup_category_only_term_scan_rejects_unlisted_hu_term(self) -> None:
        self.assertEqual(
            ["壶"],
            _cup_category_only_term_hits("映射新片不得凭空增加壶盖。"),
        )

    def test_plate_negative_spout_exemptions_are_exact_and_do_not_spread(
        self,
    ) -> None:
        plate_root = ROOT / "categories" / "盘子"
        actual: dict[str, int] = {}
        for path in plate_root.rglob("*"):
            if not path.is_file() or path.suffix not in {".md", ".json"}:
                continue
            text = path.read_text(encoding="utf-8")
            count = text.count("壶嘴")
            if count:
                relative = path.relative_to(ROOT).as_posix()
                actual[relative] = count
                self.assertIn(
                    "不得新增把手、壶嘴",
                    text,
                    f"{relative} 的“壶嘴”必须保持为 BR-01 否定性禁用语句",
                )

        self.assertEqual(PLATE_NEGATIVE_SPOUT_EXEMPTIONS, actual)

    def test_non_cup_handheld_slices_teach_complete_summary_discipline(self) -> None:
        category_keys = [item["key"] for item in installed_category_metadata(ROOT)]

        for category_key in category_keys:
            if category_key == "杯类":
                continue
            recipe = load_category_recipe(ROOT, category_key)
            for stage, topic in HANDHELD_BLUEPRINT_TOPICS.items():
                expected_tag = f"blueprint:{stage}/{topic}"
                matches = [
                    rule_slice
                    for rule_slice in _mapped_slices(recipe, stage)
                    if expected_tag in _blueprint_tags(rule_slice)
                ]
                with self.subTest(category=category_key, stage=stage, mapping=True):
                    self.assertEqual(1, len(matches))
                if len(matches) != 1:
                    continue
                text = str(matches[0].get("text") or "")
                with self.subTest(category=category_key, stage=stage, content=True):
                    self.assertEqual(
                        [],
                        _handheld_summary_discipline_problems(text, stage),
                    )

    def test_removing_or_reversing_handheld_summary_disciplines_fails(self) -> None:
        recipe = load_category_recipe(ROOT, "盘子")
        topic = HANDHELD_BLUEPRINT_TOPICS["main"]
        expected_tag = f"blueprint:main/{topic}"
        matches = [
            rule_slice
            for rule_slice in _mapped_slices(recipe, "main")
            if expected_tag in _blueprint_tags(rule_slice)
        ]
        self.assertEqual(1, len(matches))
        source_text = str(matches[0].get("text") or "")

        for label, phrase in HANDHELD_SUMMARY_DISCIPLINES.items():
            self.assertIn(phrase, source_text, f"故障注入基线缺少纪律：{label}")
            mutated_cases = {
                "删除": source_text.replace(phrase, "", 1),
                "反写": source_text.replace(
                    phrase,
                    HANDHELD_SUMMARY_REVERSED_DISCIPLINES[label],
                    1,
                ),
                "正反并存": (
                    source_text
                    + "\n【冲突规则故障注入】\n"
                    + HANDHELD_SUMMARY_REVERSED_DISCIPLINES[label]
                ),
            }
            for mutation, mutated_text in mutated_cases.items():
                with self.subTest(discipline=label, mutation=mutation):
                    problems = _handheld_summary_discipline_problems(
                        mutated_text,
                        "main",
                    )
                    self.assertTrue(
                        any(problem.startswith(label) for problem in problems),
                        problems,
                    )

    def test_br01_locked_files_keep_exact_normalized_utf8_text(self) -> None:
        for relative, expected_hash in BR01_LOCKED_FILE_SHA256.items():
            with self.subTest(file=relative):
                text = (ROOT / relative).read_bytes().decode("utf-8")
                actual_hash = _normalized_utf8_sha256(text)
                self.assertEqual(
                    expected_hash,
                    actual_hash,
                    (
                        f"{relative} 的 BR-01 终审文本发生变化"
                        f"（已统一 CRLF/LF；expected={expected_hash}, "
                        f"actual={actual_hash}）"
                    ),
                )

    def test_bowl_combination_three_rules_remain_identical_in_five_places(
        self,
    ) -> None:
        blocks = [
            _bowl_combination_rule_block(ROOT / relative)
            for relative in BOWL_COMBINATION_RULE_FILES
        ]

        self.assertEqual(5, len(blocks))
        for relative, block in zip(BOWL_COMBINATION_RULE_FILES, blocks):
            with self.subTest(file=relative):
                actual_hash = _normalized_utf8_sha256(block)
                self.assertEqual(
                    BOWL_COMBINATION_RULE_SHA256,
                    actual_hash,
                    (
                        f"{relative} 的组合单元三铁律发生变化"
                        f"（已统一 CRLF/LF；expected={BOWL_COMBINATION_RULE_SHA256}, "
                        f"actual={actual_hash}）"
                    ),
                )


if __name__ == "__main__":
    unittest.main()
