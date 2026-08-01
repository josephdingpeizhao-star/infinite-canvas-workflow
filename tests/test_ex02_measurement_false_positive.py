from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

from codex_dev_downstream import _reject_unsupported_claims  # noqa: E402
from executor_contract import ExecutorExecutionError  # noqa: E402


_OLD_MEASUREMENT_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])(\d+(?:\.\d+)?)\s*"
    r"(毫升|ml|升|l|毫米|mm|厘米|cm|克|g|千克|kg)",
    flags=re.IGNORECASE,
)
_REAL_INCIDENT_COMPOSITION = (
    "竖版 3:4 克制信息分区构图，产品在中部略偏右，左侧或上方放置少量尺寸文字和贴边标注线，"
    "背景与道具保持真实摄影感；整张只做单场景尺寸与可信细节说明，不做拼图、多画面或复杂信息图。"
)
_LEXICONS = {
    "product_material_context_markers": ["产品", "杯身"],
    "unsupported_fact_terms": ["认证"],
    "competing_dimension_terms": [
        "长度",
        "长",
        "宽度",
        "宽",
        "高度",
        "高",
        "直径",
        "口径",
        "深度",
        "深",
        "厚度",
        "厚",
    ],
    "dimension_label_terms": {
        "length_cm": ["长度", "长"],
        "width_cm": ["宽度", "宽"],
        "height_cm": ["高度", "高"],
    },
}


def _payload(text: str, *, field: str = "构图方式") -> dict[str, Any]:
    return {
        "configs": [
            {
                "config_id": "detail_03",
                "per_image_overrides": {field: text},
            }
        ]
    }


def _run_detector(text: str, *, field: str = "构图方式") -> None:
    _reject_unsupported_claims(
        _payload(text, field=field),
        7,
        "详情图变量配置",
        product_type="杯子",
        lexicons=_LEXICONS,
        confirmed_dimensions={"height_cm": 7},
    )


class Ex02MeasurementFalsePositiveTest(unittest.TestCase):
    def test_real_incident_old_pattern_matches_but_detector_accepts(self) -> None:
        old_matches = [
            match.group(0)
            for match in _OLD_MEASUREMENT_PATTERN.finditer(
                _REAL_INCIDENT_COMPOSITION
            )
        ]

        self.assertEqual(["4 克"], old_matches)
        _run_detector(_REAL_INCIDENT_COMPOSITION)

        ratio_only_probe = "竖版 3:4 克数仅指版式比例"
        self.assertEqual(
            ["4 克"],
            [
                match.group(0)
                for match in _OLD_MEASUREMENT_PATTERN.finditer(ratio_only_probe)
            ],
        )
        _run_detector(ratio_only_probe)

    def test_ratio_matrix_is_not_treated_as_measurement(self) -> None:
        cases = (
            "正方形 1:1 构图",
            "竖版 3:4 构图",
            "竖版 3:4 克制信息分区构图",
            "竖版 3：4 升级信息层级",
            "宽银幕 16:9 构图",
        )
        for text in cases:
            with self.subTest(text=text):
                _run_detector(text)

    def test_explicit_non_measurement_word_matrix_is_accepted(self) -> None:
        for word in (
            "克制",
            "克服",
            "克隆",
            "升级",
            "升华",
            "升温",
            "升起",
            "升值",
        ):
            with self.subTest(word=word):
                text = f"版本 4 {word}说明"
                self.assertIsNotNone(_OLD_MEASUREMENT_PATTERN.search(text))
                _run_detector(text)

    def test_real_unconfirmed_measurements_remain_blocked(self) -> None:
        for claim in (
            "容量 500 毫升",
            "净重 4 克",
            "重量约 4 克的杯子",
            "4 克的重量",
            "容量 1 升",
            "4 g",
            "1.5 kg",
            "500ml",
            "容量：500 毫升",
            "净重：4 克",
            "重量: 1.5 kg",
            "允许标注：4 克",
            "容量:500 毫升",
            "净重: 4 克",
            "重量：1.5 kg",
            "允许标注： 4 克",
        ):
            with self.subTest(claim=claim):
                with self.assertRaisesRegex(
                    ExecutorExecutionError,
                    "未确认参数",
                ):
                    _run_detector(claim)

    def test_confirmed_dimension_semantics_are_unchanged(self) -> None:
        _run_detector("产品高度约 7 厘米", field="尺寸比例锁定")

        with self.assertRaisesRegex(
            ExecutorExecutionError,
            "未确认参数",
        ):
            _run_detector("产品宽度约 7 厘米", field="尺寸比例锁定")


if __name__ == "__main__":
    unittest.main()
