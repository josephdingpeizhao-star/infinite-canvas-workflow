from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
SCRIPTS = ROOT / "scripts"
TESTS = ROOT / "tests"
for extra in (BRIDGE, SCRIPTS, TESTS):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import batch_intake_controller as intake_controller  # noqa: E402
import validate_final_prompt_integrity as integrity_validator  # noqa: E402
from codex_dev_downstream import parse_user_confirmed_requirements  # noqa: E402
from final_prompt_integrity_fixtures import (  # noqa: E402
    build_final_prompt_bundle,
    read_json,
    write_json,
)
from test_st03c_set_final_prompts import St03cSetFinalPromptFixture  # noqa: E402


SET_DIMENSIONS_DISABLED_MESSAGE = "套装批次不填写长、宽、高，请清空三项尺寸后再登记。"
SET_HEIGHT_SKIP_REASON = "套装编译链不产出已确认高度字面，故本检查按设计跳过。"


def confirmed_facts(
    *,
    length_cm: int | None,
    width_cm: int | None,
    height_cm: int | None,
) -> dict[str, object]:
    return {
        "product_type": "杯子",
        "length_cm": length_cm,
        "width_cm": width_cm,
        "height_cm": height_cm,
        "main_image_count": 2,
        "detail_image_count": 7,
        "handheld_main": 0,
        "handheld_detail": 0,
        "forbid_pouring_and_heating": True,
        "missing_d_no_retake": True,
    }


class St09SetHeightIntegrityTests(unittest.TestCase):
    @staticmethod
    def _set_bundle(root: Path) -> dict[str, object]:
        fixture = St03cSetFinalPromptFixture(methodName="runTest")
        prepared = fixture.prepare_full_chain(root, handheld_target=0)
        manifest_path = prepared["manifest_path"]
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["user_confirmed_facts"].update(
            {"length_cm": 5, "width_cm": 6, "height_cm": 6}
        )
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return prepared

    def test_set_with_supplied_dimensions_skips_height_literal_but_keeps_ratio_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            prepared = self._set_bundle(Path(temporary))
            manifest_path = prepared["manifest_path"]

            report = integrity_validator.build_prompts_only_report(
                batch_manifest_path=manifest_path
            )

            self.assertEqual("pass", report["status"], report["blocking_issues"])
            self.assertFalse(
                any(
                    str(issue["issue_id"]).startswith("confirmed_height_")
                    for issue in report["blocking_issues"]
                )
            )
            skipped = {
                str(item["check"]): str(item["reason"])
                for item in report["skipped_checks"]
            }
            self.assertEqual(SET_HEIGHT_SKIP_REASON, skipped["confirmed_height_literal"])
            ratio_result = next(
                item
                for item in report["results"]
                if item["check_item"] == "ratio_and_confirmed_height_literals"
            )
            self.assertEqual("pass", ratio_result["status"])
            self.assertEqual(
                "invalid_ratios=0, invalid_heights=skipped.",
                ratio_result["notes"],
            )

            prompt_path = prepared["final_dir"] / "main_01_final_prompt.json"
            prompt = json.loads(prompt_path.read_text(encoding="utf-8"))
            prompt["final_prompt"] = prompt["final_prompt"].replace(
                "画布比例固定为 1:1。",
                "",
            )
            prompt_path.write_text(
                json.dumps(prompt, ensure_ascii=False),
                encoding="utf-8",
            )
            failed = integrity_validator.build_prompts_only_report(
                batch_manifest_path=manifest_path
            )
            issue_ids = {str(issue["issue_id"]) for issue in failed["blocking_issues"]}
            self.assertEqual("fail", failed["status"])
            self.assertIn("canvas_ratio_literal_mismatch_main_01", issue_ids)
            self.assertFalse(any(issue_id.startswith("confirmed_height_") for issue_id in issue_ids))

    def test_single_missing_confirmed_height_still_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            bundle = build_final_prompt_bundle(Path(temporary))
            detail_path = bundle.prompt_path("detail_01")
            detail = read_json(detail_path)
            detail["final_prompt"] = detail["final_prompt"].replace("25 厘米", "30 厘米")
            write_json(detail_path, detail)

            report = integrity_validator.build_prompts_only_report(
                batch_manifest_path=bundle.manifest_path
            )

            issue_ids = {str(issue["issue_id"]) for issue in report["blocking_issues"]}
            self.assertEqual("fail", report["status"])
            self.assertIn("confirmed_height_literal_mismatch_detail_01", issue_ids)
            self.assertNotIn(
                "confirmed_height_literal",
                {str(item["check"]) for item in report["skipped_checks"]},
            )


class St09SetDimensionIntakeTests(unittest.TestCase):
    def test_backend_intake_rejects_each_supplied_set_dimension(self) -> None:
        for field, dimensions in (
            ("length_cm", (5, None, None)),
            ("width_cm", (None, 6, None)),
            ("height_cm", (None, None, 6)),
            ("all_dimensions", (5, 6, 6)),
        ):
            with self.subTest(field=field):
                with self.assertRaises(intake_controller.BatchIntakeGateError) as caught:
                    intake_controller._parse_facts(
                        confirmed_facts(
                            length_cm=dimensions[0],
                            width_cm=dimensions[1],
                            height_cm=dimensions[2],
                        ),
                        category="杯类",
                        batch_type="set",
                        repository_root=ROOT,
                        info_node_id="st09-info",
                        request_id="st09-request",
                    )
                self.assertEqual("invalid_facts", caught.exception.code)
                self.assertEqual(
                    SET_DIMENSIONS_DISABLED_MESSAGE,
                    caught.exception.user_message,
                )

        parsed = intake_controller._parse_facts(
            confirmed_facts(length_cm=None, width_cm=None, height_cm=None),
            category="杯类",
            batch_type="set",
            repository_root=ROOT,
            info_node_id="st09-info",
            request_id="st09-request",
        )
        self.assertEqual((None, None, None), (parsed.length_cm, parsed.width_cm, parsed.height_cm))

    def test_single_required_height_behavior_is_unchanged(self) -> None:
        with self.assertRaises(intake_controller.BatchIntakeGateError) as caught:
            intake_controller._parse_facts(
                confirmed_facts(length_cm=None, width_cm=None, height_cm=None),
                category="杯类",
                batch_type="single",
                repository_root=ROOT,
                info_node_id="st09-info",
                request_id="st09-request",
            )
        self.assertEqual("invalid_facts", caught.exception.code)
        self.assertIn("必填尺寸", caught.exception.user_message)

    def test_runtime_parser_keeps_existing_set_dimensions_for_resume(self) -> None:
        requirements = parse_user_confirmed_requirements(
            {
                "category": "杯类",
                "batch_type": "set",
                "user_confirmed_facts": confirmed_facts(
                    length_cm=5,
                    width_cm=6,
                    height_cm=6,
                ),
            },
            ROOT,
        )

        self.assertEqual(
            (5, 6, 6),
            (requirements.length_cm, requirements.width_cm, requirements.height_cm),
        )


if __name__ == "__main__":
    unittest.main()
