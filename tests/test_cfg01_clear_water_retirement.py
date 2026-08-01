from __future__ import annotations

import copy
import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

from batch_creator import (  # noqa: E402
    BatchCreator,
    UploadedFile,
    prepare_state_root,
)
from batch_intake_contract import batch_intake_contract_sha256  # noqa: E402
from batch_intake_controller import (  # noqa: E402
    BatchIntakeGateError,
    parse_queued_request,
)
from codex_dev_downstream import (  # noqa: E402
    ExecutorExecutionError,
    _final_forbidden_rule,
    _reject_scene_policy_violations,
    _variable_scene_rule,
    build_final_prompt_batch_prompt,
    build_variable_config_prompt,
    parse_user_confirmed_requirements,
    stable_json_sha256,
)


FORK_CONTRACT_SHA256 = (
    "ac9e633c814b2032eb5d72c436a773c03a7dc3f4500d3383580ee7b3f3c18de0"
)
NEW_FACTS = {
    "product_type": "杯子",
    "length_cm": None,
    "width_cm": None,
    "height_cm": 25,
    "main_image_count": 1,
    "detail_image_count": 1,
    "handheld_main": 0,
    "handheld_detail": 0,
    "forbid_pouring_and_heating": True,
    "missing_d_no_retake": True,
}
ANGLE_INVENTORY = {
    "angle_slots": [
        {
            "source_asset_id": "img_001",
            "angle_slot": "A",
            "admission_result": "合格，可进入对应槽位",
        }
    ],
    "missing_angle_slots": ["D"],
}


def _manifest_with_clear_water(value: bool) -> dict[str, object]:
    return {
        "category": "杯类",
        "user_confirmed_facts": {
            **NEW_FACTS,
            "allow_clear_water": value,
        },
    }


def _confirmation_facts(prompt: str) -> dict[str, object]:
    marker = "【用户确认事实】\n"
    _, separator, remainder = prompt.partition(marker)
    if not separator:
        raise AssertionError("prompt is missing the user-confirmed-facts block")
    return json.loads(remainder.split("\n\n", 1)[0])


def _prompt_pair(requirements: object) -> tuple[str, str]:
    variable_prompt = build_variable_config_prompt(
        mode="main",
        product_id="cfg01_contract",
        repository_root=ROOT,
        identity={},
        style_master={},
        angle_inventory=ANGLE_INVENTORY,
        requirements=requirements,
    )
    common_constraints: dict[str, object] = {}
    overrides = {
        "绑定角度槽位": "A 槽位，绑定源图 img_001；本张仅调用这一张白底图。",
        "手持交互声明": "本张图不启用手持场景",
    }
    resolved = {**common_constraints, **overrides}
    variable_config = {
        "product_id": "cfg01_contract",
        "artifact_type": "main_variable_config",
        "config_count": 1,
        "common_constraints": common_constraints,
        "configs": [
            {
                "config_id": "main_01",
                "output_type": "main",
                "per_image_overrides": overrides,
                "resolved_variable_config_sha256": stable_json_sha256(resolved),
                "notes": "",
            }
        ],
    }
    final_prompt = build_final_prompt_batch_prompt(
        mode="main",
        product_id="cfg01_contract",
        repository_root=ROOT,
        identity={},
        style_master={},
        angle_inventory=ANGLE_INVENTORY,
        variable_config=variable_config,
        requirements=requirements,
    )
    return variable_prompt, final_prompt


class Cfg01ClearWaterRetirementTest(unittest.TestCase):
    def test_contract_is_ten_fields_and_matches_fork_hash(self) -> None:
        contract = json.loads(
            (
                ROOT
                / "categories"
                / "_shared"
                / "batch-intake-contract.json"
            ).read_text(encoding="utf-8")
        )
        facts = contract["payload"]["properties"]["facts"]

        self.assertEqual(10, len(facts["required"]))
        self.assertEqual(10, len(facts["properties"]))
        self.assertEqual(set(facts["required"]), set(facts["properties"]))
        self.assertNotIn("allow_clear_water", facts["required"])
        self.assertNotIn("allow_clear_water", facts["properties"])
        self.assertEqual(
            FORK_CONTRACT_SHA256,
            batch_intake_contract_sha256(ROOT),
        )

    def test_new_manifest_compiles_no_water_without_echoing_retired_fact(self) -> None:
        requirements = parse_user_confirmed_requirements(
            {
                "category": "杯类",
                "user_confirmed_facts": dict(NEW_FACTS),
            },
            ROOT,
        )

        self.assertFalse(requirements.allow_clear_water)
        self.assertEqual(
            requirements.recipe.lexicons["scene_rules"]["no_water_forbid_actions"],
            _variable_scene_rule(requirements),
        )
        with self.assertRaisesRegex(
            ExecutorExecutionError,
            "违反用户确认场景边界",
        ):
            _reject_scene_policy_violations(
                {"notes": "本张安排清水静置"},
                requirements,
                "主图变量配置",
            )
        self.assertIn("清水场景", _final_forbidden_rule(requirements))
        for prompt in _prompt_pair(requirements):
            self.assertNotIn("allow_clear_water", _confirmation_facts(prompt))

    def test_old_manifest_true_and_false_preserve_behavior_and_echo(self) -> None:
        for allow_clear_water in (True, False):
            with self.subTest(allow_clear_water=allow_clear_water):
                requirements = parse_user_confirmed_requirements(
                    _manifest_with_clear_water(allow_clear_water),
                    ROOT,
                )
                expected_rule = (
                    "water_forbid_actions"
                    if allow_clear_water
                    else "no_water_forbid_actions"
                )
                self.assertEqual(
                    requirements.recipe.lexicons["scene_rules"][expected_rule],
                    _variable_scene_rule(requirements),
                )
                if allow_clear_water:
                    _reject_scene_policy_violations(
                        {"notes": "本张安排清水静置"},
                        requirements,
                        "主图变量配置",
                    )
                    self.assertNotIn(
                        "清水场景",
                        _final_forbidden_rule(requirements),
                    )
                else:
                    with self.assertRaisesRegex(
                        ExecutorExecutionError,
                        "违反用户确认场景边界",
                    ):
                        _reject_scene_policy_violations(
                            {"notes": "本张安排清水静置"},
                            requirements,
                            "主图变量配置",
                        )
                    self.assertIn(
                        "清水场景",
                        _final_forbidden_rule(requirements),
                    )
                for prompt in _prompt_pair(requirements):
                    self.assertIs(
                        allow_clear_water,
                        _confirmation_facts(prompt)["allow_clear_water"],
                    )

    def test_present_non_boolean_fact_still_fails_closed(self) -> None:
        for invalid in (None, 0, 1, "false"):
            with self.subTest(invalid=invalid):
                manifest = _manifest_with_clear_water(False)
                manifest["user_confirmed_facts"]["allow_clear_water"] = invalid
                with self.assertRaises(ExecutorExecutionError):
                    parse_user_confirmed_requirements(manifest, ROOT)

    def test_installed_forms_do_not_offer_retired_option(self) -> None:
        for category in ("杯类", "盘子", "碗"):
            with self.subTest(category=category):
                form = json.loads(
                    (
                        ROOT
                        / "categories"
                        / category
                        / "form.json"
                    ).read_text(encoding="utf-8")
                )
                fields = [
                    item["field"] for item in form["advanced_options"]
                ]
                self.assertEqual(
                    [
                        "forbid_pouring_and_heating",
                        "missing_d_no_retake",
                    ],
                    fields,
                )
                self.assertNotIn("allow_clear_water", fields)

    def test_controller_to_creator_writes_exact_ten_fact_manifest(self) -> None:
        payload = b"cfg01-offline-original"
        digest = hashlib.sha256(payload).hexdigest()
        info = {
            "id": "info-1",
            "type": "batch-info",
            "metadata": {
                "content": (
                    "# batch-intake\n"
                    "# request-id: cfg01-request-0001\n"
                    "# requested-at: 19000\n"
                    "build: batch"
                ),
                "batchIntake": {
                    "status": "queued",
                    "requestId": "cfg01-request-0001",
                    "requestedAt": 19_000,
                    "category": "杯类",
                    "contractHash": FORK_CONTRACT_SHA256,
                    "facts": dict(NEW_FACTS),
                },
            },
        }
        workflow = {
            "id": "workflow-1",
            "type": "workflow",
            "metadata": {},
        }
        image = {
            "id": "image-1",
            "type": "image",
            "metadata": {
                "storageKey": "image:cfg01",
                "sourceFile": {
                    "name": "白底原图.png",
                    "size": len(payload),
                    "type": "image/png",
                    "lastModified": 1_720_000_000_000,
                    "sha256": digest,
                },
            },
        }
        state = {
            "nodes": [info, workflow, image],
            "connections": [
                {
                    "id": "info-to-workflow",
                    "fromNodeId": "info-1",
                    "toNodeId": "workflow-1",
                },
                {
                    "id": "image-to-workflow",
                    "fromNodeId": "image-1",
                    "toNodeId": "workflow-1",
                },
            ],
            "viewport": {"x": 0, "y": 0, "k": 1},
        }

        request = parse_queued_request(
            state,
            info,
            now_ms=20_000,
        )
        self.assertEqual(10, len(request.facts.as_dict()))
        self.assertNotIn("allow_clear_water", request.facts.as_dict())

        retired_state = copy.deepcopy(state)
        retired_info = retired_state["nodes"][0]
        retired_info["metadata"]["batchIntake"]["facts"][
            "allow_clear_water"
        ] = True
        with self.assertRaises(BatchIntakeGateError) as caught:
            parse_queued_request(
                retired_state,
                retired_info,
                now_ms=20_000,
            )
        self.assertEqual("invalid_facts", caught.exception.code)

        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            repository = base / "repo"
            (repository / "scripts").mkdir(parents=True)
            (repository / "canvas-bridge").mkdir()
            (repository / "manifests").mkdir()
            shutil.copy2(
                ROOT / "scripts" / "build_batch_manifest.py",
                repository / "scripts",
            )
            for name in ("category_recipes.py", "image_count_contract.py"):
                shutil.copy2(
                    ROOT / "canvas-bridge" / name,
                    repository / "canvas-bridge",
                )
            shutil.copytree(
                ROOT / "categories",
                repository / "categories",
            )
            for name in (
                "batch_manifest.template.json",
                "asset_manifest.template.json",
            ):
                shutil.copy2(
                    ROOT / "manifests" / name,
                    repository / "manifests",
                )

            test_root = base / "test-root"
            test_root.mkdir()
            (test_root / ".canvas_intake_test_root").write_text(
                "canvas-intake-test-root-v1\n",
                encoding="utf-8",
            )
            state_root = base / "state"
            prepare_state_root(state_root)
            upload_path = base / "upload.png"
            upload_path.write_bytes(payload)
            creator = BatchCreator(
                repository,
                state_root,
                test_root=test_root,
                now=lambda: datetime(2026, 7, 31, 12, 34, 56),
            )
            result = creator.create(
                request,
                (
                    UploadedFile(
                        source_node_id="image-1",
                        path=upload_path,
                        name="白底原图.png",
                        size=len(payload),
                        mime_type="image/png",
                        sha256=digest,
                    ),
                ),
            )
            manifest = json.loads(
                result.manifest_path.read_text(encoding="utf-8")
            )

        self.assertEqual(10, len(manifest["user_confirmed_facts"]))
        self.assertNotIn(
            "allow_clear_water",
            manifest["user_confirmed_facts"],
        )


if __name__ == "__main__":
    unittest.main()
