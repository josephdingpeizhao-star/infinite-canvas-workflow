from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RECIPE_STAGES = ("main", "detail", "final")

CONFIRMED_HEIGHT_LITERAL = re.compile(
    r"高度\s*约\s*"
    r"(?:\{\s*height_cm\s*\}|_{2,}|\d+(?:\.\d+)?)"
    r"\s*(?:厘米|cm\b)",
    re.IGNORECASE,
)
BARE_HEIGHT_LITERAL = re.compile(
    r"(?<!度)高\s*约\s*"
    r"(?:\{\s*height_cm\s*\}|_{2,}|\d+(?:\.\d+)?)"
    r"\s*(?:厘米|cm\b)",
    re.IGNORECASE,
)


def category_recipe_directories(categories_root: Path) -> list[Path]:
    return [
        candidate
        for candidate in sorted(categories_root.iterdir(), key=lambda path: path.name)
        if candidate.is_dir()
        and not candidate.name.startswith("_")
        and (
            (candidate / "prompts").is_dir()
            or (candidate / "runtime").is_dir()
        )
    ]


def assert_confirmed_height_literal_reachable(
    testcase: unittest.TestCase,
    category_root: Path,
) -> None:
    checked_stages: list[str] = []
    for stage in RECIPE_STAGES:
        recipe_files = [
            path
            for path in (
                category_root / "prompts" / f"{stage}.md",
                category_root / "runtime" / f"{stage}.json",
            )
            if path.is_file()
        ]
        if not recipe_files:
            continue
        checked_stages.append(stage)
        teaching = "\n".join(
            path.read_text(encoding="utf-8")
            for path in recipe_files
        )
        if CONFIRMED_HEIGHT_LITERAL.search(teaching):
            continue
        if BARE_HEIGHT_LITERAL.search(teaching):
            testcase.fail(
                f"品类“{category_root.name}”的 {stage} 阶段仅包含裸“高约”教学，"
                "缺少闸门可识别的“高度约”确认高度句式。"
            )
        testcase.fail(
            f"品类“{category_root.name}”的 {stage} 阶段缺少闸门可识别的"
            "“高度约”确认高度教学句式。"
        )

    testcase.assertTrue(
        checked_stages,
        f"品类“{category_root.name}”没有可检查的 main/detail/final 配方阶段。",
    )


class CategoryConfirmedHeightLiteralContractTest(unittest.TestCase):
    def test_every_category_recipe_stage_can_reach_confirmed_height_literal(
        self,
    ) -> None:
        category_roots = category_recipe_directories(ROOT / "categories")
        self.assertTrue(category_roots, "categories/* 下没有发现品类配方目录。")

        for category_root in category_roots:
            with self.subTest(category=category_root.name):
                assert_confirmed_height_literal_reachable(self, category_root)

    def test_fault_injection_rejects_category_that_only_teaches_bare_height(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fake_category = Path(temp_dir) / "categories" / "假品类"
            prompts = fake_category / "prompts"
            prompts.mkdir(parents=True)
            (prompts / "main.md").write_text(
                "用户确认高度必须写“高约 {height_cm} 厘米”。",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                AssertionError,
                "仅包含裸“高约”教学",
            ) as caught:
                assert_confirmed_height_literal_reachable(self, fake_category)

            self.assertIn("缺少闸门可识别的“高度约”", str(caught.exception))

    def test_fault_injection_checks_each_stage_instead_of_category_aggregate(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fake_category = Path(temp_dir) / "categories" / "混合假品类"
            prompts = fake_category / "prompts"
            prompts.mkdir(parents=True)
            (prompts / "main.md").write_text(
                "用户确认高度必须写“高约 {height_cm} 厘米”。",
                encoding="utf-8",
            )
            (prompts / "detail.md").write_text(
                "用户确认高度必须写“高度约 {height_cm} 厘米”。",
                encoding="utf-8",
            )
            (prompts / "final.md").write_text(
                "用户确认高度必须写“高度约 {height_cm} 厘米”。",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                AssertionError,
                "main 阶段仅包含裸“高约”教学",
            ):
                assert_confirmed_height_literal_reachable(self, fake_category)


if __name__ == "__main__":
    unittest.main()
