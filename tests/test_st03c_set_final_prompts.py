from __future__ import annotations

import copy
import hashlib
import inspect
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
SCRIPTS = ROOT / "scripts"
TESTS = ROOT / "tests"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

import batch_intake_controller as intake_controller  # noqa: E402
import batch_type_gate  # noqa: E402
import codex_dev_downstream as downstream  # noqa: E402
import validate_final_prompt_integrity as integrity_validator  # noqa: E402
from codex_dev_downstream import (  # noqa: E402
    FinalPromptLiteralViolation,
    SET_FINAL_PROMPT_UPSTREAM_KEYS,
    build_set_final_prompt_batch_prompt,
    build_set_final_prompt_repair_prompt,
    expand_set_final_prompt_upstream_keys,
    final_prompt_bundle_targets,
    parse_set_final_prompt_batch_response,
    parse_set_variable_config_response,
    parse_user_confirmed_requirements,
)
from codex_dev_executor import CodexDevExecutor  # noqa: E402
from executor_contract import ExecutionRequest, ExecutorContext, ExecutorExecutionError  # noqa: E402
from test_st03b_set_variable_config import (  # noqa: E402
    BLOCKED_MESSAGE,
    PRODUCT_ID,
    SequenceTransport,
    SetVariableConfigFixture,
    set_detail_chunks,
    set_manifest_facts,
    set_requirements,
    valid_component_identity,
    valid_set_identity,
    valid_set_layout_inventory,
    valid_set_variable_response,
    valid_style_master,
)


SET_HANDHELD_DISABLED_MESSAGE = "套装批次暂不支持手持，主图与详情手持数量必须为 0。"
# 钉住 prompts-only 单品正文；仅在确认有意修改单品正文后才更新此指纹。
PROMPTS_ONLY_SINGLE_BODY_SHA256 = (
    "372d9a29430095e5f13ce140fdd5cb448d60e004a3935ca5c58f293f2dcfaf44"
)


def _component_identity_with_negatives(index: int) -> dict[str, object]:
    identity = copy.deepcopy(valid_component_identity(index))
    identity["identity"]["negative_prompt_constraints"] = [
        "禁止虚构未确认卖点",
        f"禁止改变第 {index} 件单件结构",
    ]
    return identity


def _formal_set_variable_config(
    root: Path,
    *,
    mode: str,
    count: int,
    handheld_target: int,
    enabled_ids: tuple[str, ...] = (),
    layout: dict[str, object] | None = None,
) -> dict[str, object]:
    root.mkdir(parents=True, exist_ok=True)
    fixture = SetVariableConfigFixture()
    layout = layout or valid_set_layout_inventory()
    response = valid_set_variable_response(
        mode,
        count=count,
        handheld_target=handheld_target,
        enabled_ids=enabled_ids,
    )
    binding_file_name = str(layout["layouts"][0]["file_name"])
    if binding_file_name != "group.png":
        for config in response["configs"]:
            overrides = config["per_image_overrides"]
            overrides["绑定角度槽位"] = overrides["绑定角度槽位"].replace(
                "group.png",
                binding_file_name,
            )
            overrides["套装编排槽位"] = overrides["套装编排槽位"].replace(
                "group.png",
                binding_file_name,
            )
    return parse_set_variable_config_response(
        json.dumps(response, ensure_ascii=False),
        mode=mode,
        product_id=PRODUCT_ID,
        requirements=set_requirements(
            main_count=count if mode == "main" else 2,
            detail_count=count if mode == "detail" else 2,
            handheld_main=handheld_target if mode == "main" else 0,
            handheld_detail=handheld_target if mode == "detail" else 0,
        ),
        set_identity=valid_set_identity(),
        component_identities=(valid_component_identity(1), valid_component_identity(2)),
        set_angle_layout_inventory=layout,
        upstream_paths=fixture.make_upstream_paths(root, mode=mode),
    )


def _final_prompt_response(
    mode: str,
    *,
    count: int,
    enabled_ids: tuple[str, ...] = (),
) -> dict[str, object]:
    ratio = "1:1" if mode == "main" else "3:4"
    prompts: list[dict[str, str]] = []
    for index in range(1, count + 1):
        config_id = f"{mode}_{index:02d}"
        handheld = (
            "本张图启用手持场景。"
            if config_id in enabled_ids
            else "本张图不启用手持场景。"
        )
        prompts.append(
            {
                "config_id": config_id,
                "final_prompt": (
                    f"图1，group.png，编排槽位一。画布比例固定为 {ratio}。{handheld}"
                ),
                "negative_prompt": "禁止虚构未确认卖点，禁止改变套装组成与单件结构",
            }
        )
    return {"prompts": prompts}


def _facts(*, handheld_main: int, handheld_detail: int) -> dict[str, object]:
    return {
        "product_type": "杯子",
        "length_cm": None,
        "width_cm": None,
        "height_cm": None,
        "main_image_count": 6,
        "detail_image_count": 8,
        "handheld_main": handheld_main,
        "handheld_detail": handheld_detail,
        "forbid_pouring_and_heating": True,
        "missing_d_no_retake": True,
    }


class St03cSetHandheldIntakeTests(unittest.TestCase):
    @staticmethod
    def _parse_facts(
        batch_type: str,
        *,
        handheld_main: int,
        handheld_detail: int,
    ) -> intake_controller.ConfirmedFacts:
        facts = _facts(
            handheld_main=handheld_main,
            handheld_detail=handheld_detail,
        )
        if batch_type == "single":
            facts["height_cm"] = 25
        return intake_controller._parse_facts(
            facts,
            category="杯类",
            batch_type=batch_type,
            repository_root=ROOT,
            info_node_id="st03c-info",
            request_id="st03c-request",
        )

    @staticmethod
    def _run_manifest_builder(
        batch_type: str,
        *,
        handheld_main: int,
        handheld_detail: int,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["PYTHONUTF8"] = "1"
        environment["PYTHONIOENCODING"] = "utf-8"
        return subprocess.run(
            [
                sys.executable,
                "-B",
                str(ROOT / "scripts" / "build_batch_manifest.py"),
                "--product-id",
                f"st03c_{batch_type}_{handheld_main}_{handheld_detail}",
                "--product-type",
                "杯子",
                "--batch-type",
                batch_type,
                "--category",
                "杯类",
                "--height-cm",
                "25",
                "--main-count",
                "6",
                "--detail-count",
                "8",
                "--handheld-main",
                str(handheld_main),
                "--handheld-detail",
                str(handheld_detail),
                "--forbid-pouring-and-heating",
                "true",
                "--missing-d-no-retake",
                "true",
                "--dry-run",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            env=environment,
        )

    def test_canvas_payload_rejects_nonzero_set_handheld_counts(self) -> None:
        for handheld_main, handheld_detail in ((1, 0), (0, 1)):
            with self.subTest(
                handheld_main=handheld_main,
                handheld_detail=handheld_detail,
            ):
                with self.assertRaises(intake_controller.BatchIntakeGateError) as caught:
                    self._parse_facts(
                        "set",
                        handheld_main=handheld_main,
                        handheld_detail=handheld_detail,
                    )
                self.assertEqual("invalid_facts", caught.exception.code)
                self.assertEqual(SET_HANDHELD_DISABLED_MESSAGE, caught.exception.user_message)

    def test_cli_rejects_nonzero_set_handheld_counts(self) -> None:
        for handheld_main, handheld_detail in ((1, 0), (0, 1)):
            with self.subTest(
                handheld_main=handheld_main,
                handheld_detail=handheld_detail,
            ):
                completed = self._run_manifest_builder(
                    "set",
                    handheld_main=handheld_main,
                    handheld_detail=handheld_detail,
                )
                self.assertEqual(2, completed.returncode)
                self.assertEqual(f"{SET_HANDHELD_DISABLED_MESSAGE}\n", completed.stdout)

    def test_zero_set_and_legal_single_handheld_counts_pass_both_entries(self) -> None:
        set_facts = self._parse_facts("set", handheld_main=0, handheld_detail=0)
        self.assertEqual((0, 0), (set_facts.handheld_main, set_facts.handheld_detail))
        set_completed = self._run_manifest_builder(
            "set",
            handheld_main=0,
            handheld_detail=0,
        )
        self.assertEqual(0, set_completed.returncode, set_completed.stdout + set_completed.stderr)
        set_manifest = json.loads(set_completed.stdout)["manifest_data"]
        self.assertEqual(
            (0, 0),
            (
                set_manifest["user_confirmed_facts"]["handheld_main"],
                set_manifest["user_confirmed_facts"]["handheld_detail"],
            ),
        )

        single_facts = self._parse_facts("single", handheld_main=2, handheld_detail=1)
        self.assertEqual(
            (2, 1),
            (single_facts.handheld_main, single_facts.handheld_detail),
        )
        single_completed = self._run_manifest_builder(
            "single",
            handheld_main=2,
            handheld_detail=1,
        )
        self.assertEqual(
            0,
            single_completed.returncode,
            single_completed.stdout + single_completed.stderr,
        )

    def test_runtime_parser_keeps_existing_nonzero_set_handheld_interface(self) -> None:
        requirements = parse_user_confirmed_requirements(
            {
                "category": "杯类",
                "batch_type": "set",
                "user_confirmed_facts": _facts(
                    handheld_main=2,
                    handheld_detail=1,
                ),
            },
            ROOT,
        )

        self.assertEqual((2, 1), (requirements.handheld_main, requirements.handheld_detail))


class St03cGateAndArchitectureTests(unittest.TestCase):
    def test_set_has_eight_open_steps_one_closed_and_single_has_all_nine(self) -> None:
        steps = (
            "identity",
            "style_master",
            "angle_inventory",
            "main_vc",
            "detail_vc",
            "final_prompts",
            "integrity",
            "renders",
            "qc",
        )
        self.assertEqual(
            {
                "identity",
                "style_master",
                "angle_inventory",
                "main_vc",
                "detail_vc",
                "final_prompts",
                "integrity",
                "renders",
            },
            set(batch_type_gate.SET_READY_STEPS),
        )
        for step in steps:
            with self.subTest(batch_type="set", step=step):
                self.assertEqual(
                    BLOCKED_MESSAGE if step == "qc" else None,
                    batch_type_gate.set_batch_blocked_message(
                        {"batch_type": "set"},
                        step,
                    ),
                )
            with self.subTest(batch_type="single", step=step):
                self.assertIsNone(
                    batch_type_gate.set_batch_blocked_message(
                        {"batch_type": "single"},
                        step,
                    )
                )

    def test_shared_upstream_family_is_consumed_by_bundle_and_integrity(self) -> None:
        self.assertEqual(
            (
                "set_product_identity",
                "component_identity_archive_NN",
                "style_master",
                "set_angle_layout_inventory",
                "variable_config",
            ),
            SET_FINAL_PROMPT_UPSTREAM_KEYS,
        )
        self.assertEqual(
            (
                "set_product_identity",
                "component_identity_archive_01",
                "component_identity_archive_02",
                "style_master",
                "set_angle_layout_inventory",
                "variable_config",
            ),
            expand_set_final_prompt_upstream_keys(2),
        )
        bundle_source = inspect.getsource(downstream.build_set_final_prompt_bundle)
        integrity_source = inspect.getsource(integrity_validator.build_prompts_only_report)
        set_entry_source = inspect.getsource(
            integrity_validator._build_set_prompts_only_report
        )
        self.assertIn("expand_set_final_prompt_upstream_keys", bundle_source)
        self.assertIn("expand_set_final_prompt_upstream_keys", integrity_source)
        self.assertIn("SET_FINAL_PROMPT_COMPONENT_UPSTREAM_KEY", bundle_source)
        self.assertIn("SET_FINAL_PROMPT_UPSTREAM_KEYS", integrity_source)
        self.assertIn("_build_prompts_only_report_common", integrity_source)
        self.assertIn("_build_prompts_only_report_common", set_entry_source)
        self.assertIn("set_upstream_keys", set_entry_source)
        self.assertIn("expand_upstream_keys", set_entry_source)
        self.assertIn('batch_type="set"', set_entry_source)
        self.assertIn('batch_type="single"', integrity_source)

        dispatch_start = integrity_source.index("    batch_manifest_probe =")
        single_body_start = integrity_source.index(
            "    return _build_prompts_only_report_common(",
            dispatch_start,
        )
        public_single_source = (
            integrity_source[:dispatch_start] + integrity_source[single_body_start:]
        )
        self.assertNotIn("_build_set_prompts_only_report", public_single_source)
        self.assertNotIn("SET_FINAL_PROMPT", public_single_source)
        self.assertNotIn("set_upstream_keys", public_single_source)
        self.assertEqual(
            PROMPTS_ONLY_SINGLE_BODY_SHA256,
            hashlib.sha256(public_single_source.rstrip().encode("utf-8")).hexdigest(),
        )

    def test_single_final_prompt_functions_remain_set_free(self) -> None:
        for function in (
            downstream.build_final_prompt_batch_prompt,
            downstream.parse_final_prompt_batch_response,
            downstream.build_final_prompt_bundle,
            downstream.build_final_prompt_repair_prompt,
        ):
            with self.subTest(function=function.__name__):
                source = inspect.getsource(function)
                self.assertNotIn("_set_final_prompt", source)
                self.assertNotIn("SET_FINAL_PROMPT", source)

    def test_single_executor_dispatch_does_not_enter_set_final_branch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifacts_root = root / "artifacts"
            manifest = {
                "product_id": "st03c-single-dispatch",
                "batch_type": "single",
                "category": "杯类",
                "user_confirmed_facts": {
                    **set_manifest_facts(
                        main_count=2,
                        detail_count=2,
                        handheld_main=0,
                        handheld_detail=0,
                    ),
                    "height_cm": 12,
                },
                "workspace": {"artifacts_root": str(artifacts_root)},
                "artifacts": {
                    "final_prompts": str(artifacts_root / "final_prompts"),
                    "product_identity_archive": str(artifacts_root / "missing_identity"),
                    "style_master": str(artifacts_root / "missing_style"),
                    "angle_inventory": str(artifacts_root / "missing_angle"),
                    "main_variable_configs": str(artifacts_root / "missing_main"),
                    "detail_variable_configs": str(artifacts_root / "missing_detail"),
                },
            }
            executor = CodexDevExecutor(
                ExecutorContext(
                    manifest=manifest,
                    manifest_path=root / "single.batch_manifest.json",
                    environment={"CODEX_DEV_ALLOW_REAL_EXECUTION": "1"},
                ),
                transport=SequenceTransport([]),
                repository_root=ROOT,
            )
            with mock.patch.object(
                executor,
                "_execute_set_final_prompts",
                side_effect=AssertionError("single branch entered set final prompts"),
            ) as set_branch:
                with self.assertRaises(ExecutorExecutionError):
                    executor.execute(ExecutionRequest(step="final_prompts"))
            set_branch.assert_not_called()


class St03cSetFinalPromptFixture(SetVariableConfigFixture):
    def prepare_full_chain(
        self,
        root: Path,
        *,
        handheld_target: int,
        actual_handheld: int | None = None,
    ) -> dict[str, object]:
        main_count = 2
        detail_count = 7
        actual_handheld = handheld_target if actual_handheld is None else actual_handheld
        main_enabled = tuple(
            f"main_{index:02d}" for index in range(1, actual_handheld + 1)
        )
        detail_enabled = tuple(
            f"detail_{index:02d}" for index in range(1, actual_handheld + 1)
        )
        main_variable_response = valid_set_variable_response(
            "main",
            count=main_count,
            handheld_target=handheld_target,
            enabled_ids=main_enabled,
        )
        detail_variable_response = valid_set_variable_response(
            "detail",
            count=detail_count,
            handheld_target=handheld_target,
            enabled_ids=detail_enabled,
        )
        responses = [
            main_variable_response,
            *set_detail_chunks(detail_variable_response),
            _final_prompt_response("main", count=main_count, enabled_ids=main_enabled),
            _final_prompt_response("detail", count=detail_count, enabled_ids=detail_enabled),
        ]
        executor, transport, manifest, paths = self.make_executor(root, responses)
        manifest["user_confirmed_facts"] = set_manifest_facts(
            main_count=main_count,
            detail_count=detail_count,
            handheld_main=handheld_target,
            handheld_detail=handheld_target,
        )
        artifacts_root = Path(manifest["workspace"]["artifacts_root"])
        final_dir = artifacts_root / "final_prompts"
        manifest["artifacts"]["final_prompts"] = str(final_dir)
        # 套装完整性分流不应读取这个单品角度目录；故意不创建其文件。
        manifest["artifacts"]["angle_inventory"] = str(
            artifacts_root / "unused_single_angle_inventory"
        )
        for index in (1, 2):
            component_path = (
                paths["identity"] / f"component_{index:02d}_product_identity_archive.json"
            )
            component_path.write_text(
                json.dumps(_component_identity_with_negatives(index), ensure_ascii=False),
                encoding="utf-8",
            )

        executor.execute(ExecutionRequest(step="main_vc"))
        executor.execute(ExecutionRequest(step="detail_vc"))
        final_transport_calls_before = len(transport.calls)
        final_continuations_before = len(transport.continuation_calls)
        result = executor.execute(ExecutionRequest(step="final_prompts"))
        executor.context.manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return {
            "executor": executor,
            "transport": transport,
            "manifest": manifest,
            "manifest_path": executor.context.manifest_path,
            "paths": paths,
            "final_dir": final_dir,
            "result": result,
            "final_transport_calls_before": final_transport_calls_before,
            "final_continuations_before": final_continuations_before,
        }

    def parse_final(
        self,
        root: Path,
        response: dict[str, object],
        *,
        mode: str = "main",
        handheld_target: int = 0,
        enabled_ids: tuple[str, ...] = (),
        layout: dict[str, object] | None = None,
    ) -> dict[str, dict[str, str]]:
        variable_config = _formal_set_variable_config(
            root / f"formal_{mode}",
            mode=mode,
            count=2,
            handheld_target=handheld_target,
            enabled_ids=enabled_ids,
            layout=layout,
        )
        return parse_set_final_prompt_batch_response(
            json.dumps(response, ensure_ascii=False),
            mode=mode,
            product_id=PRODUCT_ID,
            requirements=set_requirements(
                main_count=2,
                detail_count=2,
                handheld_main=handheld_target if mode == "main" else 0,
                handheld_detail=handheld_target if mode == "detail" else 0,
            ),
            set_identity=valid_set_identity(),
            component_identities=(valid_component_identity(1), valid_component_identity(2)),
            set_angle_layout_inventory=layout or valid_set_layout_inventory(),
            variable_config=variable_config,
            style_master_text=json.dumps(valid_style_master(), ensure_ascii=False),
        )


class St03cSetFinalPromptCompilerTests(St03cSetFinalPromptFixture):
    def test_set_final_full_chain_uses_two_turns_and_writes_closed_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            prepared = self.prepare_full_chain(Path(temporary), handheld_target=0)
            final_dir = prepared["final_dir"]
            requirements = set_requirements(
                main_count=2,
                detail_count=7,
                handheld_main=0,
                handheld_detail=0,
            )
            self.assertEqual(
                set(final_prompt_bundle_targets(final_dir, requirements=requirements)),
                {path for path in final_dir.iterdir()},
            )
            self.assertEqual(
                2,
                len(prepared["transport"].calls)
                - prepared["final_transport_calls_before"],
            )
            self.assertEqual(
                prepared["final_continuations_before"],
                len(prepared["transport"].continuation_calls),
            )
            self.assertEqual((final_dir / "final_prompt_index.json",), prepared["result"].outputs)

            expected_upstream_keys = set(expand_set_final_prompt_upstream_keys(2))
            index = json.loads((final_dir / "final_prompt_index.json").read_text(encoding="utf-8"))
            self.assertEqual(["group.png"] * 9, [item["bound_reference"] for item in index["items"]])
            for item in index["items"]:
                final_doc = json.loads(Path(item["final_prompt_path"]).read_text(encoding="utf-8"))
                self.assertEqual(expected_upstream_keys, set(final_doc["upstream_artifacts"]))
            with self.assertRaisesRegex(ExecutorExecutionError, "已存在"):
                prepared["executor"].execute(ExecutionRequest(step="final_prompts"))

    def test_set_binding_literals_accept_green_and_reject_four_red_cases(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legal = _final_prompt_response("main", count=2)
            parsed = self.parse_final(root, legal)
            self.assertEqual(("main_01", "main_02"), tuple(parsed))

            cases: list[tuple[str, dict[str, object], dict[str, object], str]] = []
            missing_number = copy.deepcopy(legal)
            missing_number["prompts"][0]["final_prompt"] = missing_number["prompts"][0][
                "final_prompt"
            ].replace("图1，", "")
            cases.append(("missing image number", missing_number, valid_set_layout_inventory(), "未保留套装角度图号"))

            missing_file = copy.deepcopy(legal)
            missing_file["prompts"][0]["final_prompt"] = missing_file["prompts"][0][
                "final_prompt"
            ].replace("group.png，", "")
            cases.append(("missing file name", missing_file, valid_set_layout_inventory(), "未保留套装角度文件名"))

            layout_with_rejected = valid_set_layout_inventory()
            layout_with_rejected["layouts"].append(
                {
                    "layout_id": "layout_003",
                    "image_index": 3,
                    "file_name": "rejected.png",
                    "is_set_group": True,
                    "overall_camera": "C",
                    "layout_slot": "编排槽位二",
                    "admission_result": "不适合入库，需重拍",
                }
            )
            rejected_reference = copy.deepcopy(legal)
            rejected_reference["prompts"][0]["final_prompt"] += "不得遗漏图3。"
            cases.append(("rejected layout reference", rejected_reference, layout_with_rejected, "引用了不合格套装编排条目"))

            unsuitable_value = copy.deepcopy(legal)
            unsuitable_value["prompts"][0]["final_prompt"] += "不适合归入现有编排。"
            cases.append(("unsuitable enum", unsuitable_value, valid_set_layout_inventory(), "使用了不适合的套装编排值"))

            for name, response, layout, message in cases:
                with self.subTest(name=name), self.assertRaisesRegex(
                    ExecutorExecutionError,
                    message,
                ):
                    self.parse_final(root, response, layout=layout)

            shared_marker_layout = valid_set_layout_inventory()
            for entry in shared_marker_layout["layouts"]:
                entry["file_name"] = "按上传顺序识别"
            shared_marker_layout["layouts"][1]["admission_result"] = "不适合入库，需重拍"
            shared_marker_legal = _final_prompt_response("main", count=2)
            for prompt in shared_marker_legal["prompts"]:
                prompt["final_prompt"] = prompt["final_prompt"].replace(
                    "group.png",
                    "按上传顺序识别",
                )
            self.parse_final(
                root,
                shared_marker_legal,
                layout=shared_marker_layout,
            )
            shared_marker_rejected = copy.deepcopy(shared_marker_legal)
            shared_marker_rejected["prompts"][0]["final_prompt"] += "引用图2。"
            with self.assertRaisesRegex(
                ExecutorExecutionError,
                "引用了不合格套装编排条目",
            ):
                self.parse_final(
                    root,
                    shared_marker_rejected,
                    layout=shared_marker_layout,
                )

    def test_set_binding_rejects_image_index_prefix_collision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            response = _final_prompt_response("main", count=2)
            response["prompts"][0]["final_prompt"] = response["prompts"][0][
                "final_prompt"
            ].replace("图1，", "图12，")
            with self.assertRaisesRegex(
                ExecutorExecutionError,
                "未保留套装角度图号",
            ):
                self.parse_final(Path(temporary), response)

    def test_set_bundle_rejects_missing_shared_upstream_key(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            upstream_paths = {
                "set_product_identity": root / "set_product_identity.json",
                "component_identity_archive_01": root / "component_01.json",
                "component_identity_archive_02": root / "component_02.json",
                "set_angle_layout_inventory": root / "set_angle_layout_inventory.json",
            }
            with self.assertRaisesRegex(
                ExecutorExecutionError,
                "无法固定套装最终提示词上游引用",
            ):
                downstream.build_set_final_prompt_bundle(
                    product_id=PRODUCT_ID,
                    output_dir=root / "final_prompts",
                    prompt_batches={"main": {}, "detail": {}},
                    variable_configs={
                        "main": ({}, root / "main_variable_config.json"),
                        "detail": ({}, root / "detail_variable_config.json"),
                    },
                    upstream_paths=upstream_paths,
                    set_angle_layout_inventory=valid_set_layout_inventory(),
                    requirements=set_requirements(
                        main_count=2,
                        detail_count=2,
                        handheld_main=0,
                        handheld_detail=0,
                    ),
                )

    def test_set_prompt_has_no_height_requirement_and_ratio_repair_is_ratio_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legal = _final_prompt_response("main", count=2)
            self.assertNotIn("高度约", legal["prompts"][0]["final_prompt"])
            self.parse_final(root, legal)

            missing_ratio = copy.deepcopy(legal)
            missing_ratio["prompts"][0]["final_prompt"] = missing_ratio["prompts"][0][
                "final_prompt"
            ].replace("画布比例固定为 1:1。", "")
            with self.assertRaises(FinalPromptLiteralViolation) as caught:
                self.parse_final(root, missing_ratio)
            self.assertEqual("未保留画布比例", caught.exception.safe_reason)
            repair = build_set_final_prompt_repair_prompt(mode="main")
            self.assertIn("画布比例固定为 1:1", repair)
            self.assertNotIn("高度", repair)
            self.assertNotIn("厘米", repair)

    def test_prompt_constructor_uses_archive_dimension_source_and_config_handheld_count(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            variable_config = _formal_set_variable_config(
                Path(temporary),
                mode="main",
                count=2,
                handheld_target=1,
                enabled_ids=("main_01",),
            )
            prompt = build_set_final_prompt_batch_prompt(
                mode="main",
                product_id=PRODUCT_ID,
                repository_root=ROOT,
                set_identity=valid_set_identity(),
                component_identities=(valid_component_identity(1), valid_component_identity(2)),
                style_master=valid_style_master(),
                set_angle_layout_inventory=valid_set_layout_inventory(),
                variable_config=variable_config,
                requirements=set_requirements(
                    main_count=2,
                    detail_count=2,
                    handheld_main=2,
                    handheld_detail=0,
                ),
                final_prompt_skill_text="FINAL_SKILL_MARKER",
                set_workflow_supplement="SET_WORKFLOW_MARKER",
                set_layout_rules="SET_LAYOUT_MARKER",
            )
            for marker in (
                "FINAL_SKILL_MARKER",
                "SET_WORKFLOW_MARKER",
                "SET_LAYOUT_MARKER",
                "以《套装产品身份档案》及各单件《产品身份档案》的真实尺寸字段为准",
                '"expected_handheld": 1',
            ):
                self.assertIn(marker, prompt)
            self.assertNotIn("高度约 None", prompt)

    def test_zero_and_historical_nonzero_config_handheld_full_chains_pass(self) -> None:
        for target, actual in ((0, 0), (1, 1), (2, 1)):
            with self.subTest(
                handheld_target=target,
                actual_handheld=actual,
            ), tempfile.TemporaryDirectory() as temporary:
                prepared = self.prepare_full_chain(
                    Path(temporary),
                    handheld_target=target,
                    actual_handheld=actual,
                )
                report = integrity_validator.build_prompts_only_report(
                    batch_manifest_path=prepared["manifest_path"]
                )
                self.assertEqual("pass", report["status"], report["blocking_issues"])
                self.assertEqual(actual, report["handheld_count_summary"]["variable_config_main"])
                self.assertEqual(actual, report["handheld_count_summary"]["final_prompt_main"])


class St03cSetIntegrityTests(St03cSetFinalPromptFixture):
    @staticmethod
    def issue_ids(report: dict[str, object]) -> set[str]:
        return {str(issue["issue_id"]) for issue in report["issues"]}

    def test_integrity_passes_set_bundle_and_uses_component_negative_union(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            prepared = self.prepare_full_chain(Path(temporary), handheld_target=0)
            first_component = prepared["paths"]["identity"] / "component_01_product_identity_archive.json"
            first_doc = json.loads(first_component.read_text(encoding="utf-8"))
            first_doc["identity"].pop("negative_prompt_constraints")
            first_component.write_text(
                json.dumps(first_doc, ensure_ascii=False),
                encoding="utf-8",
            )
            report = integrity_validator.build_prompts_only_report(
                batch_manifest_path=prepared["manifest_path"]
            )
            self.assertEqual("pass", report["status"], report["blocking_issues"])
            self.assertNotIn("identity_negative_prompt_constraint_missing", self.issue_ids(report))

    def test_integrity_fails_closed_for_component_layout_and_upstream_damage(self) -> None:
        cases = (
            ("component", "document_unreadable_component_identity_02"),
            ("layout", "document_unreadable_set_angle_layout_inventory"),
            (
                "upstream",
                "upstream_path_mismatch_set_angle_layout_inventory_main_01",
            ),
            ("extra_component_upstream", "upstream_key_set_mismatch_main_01"),
        )
        for mutation, expected_issue in cases:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                prepared = self.prepare_full_chain(Path(temporary), handheld_target=0)
                if mutation == "component":
                    (
                        prepared["paths"]["identity"]
                        / "component_02_product_identity_archive.json"
                    ).unlink()
                elif mutation == "layout":
                    (
                        prepared["paths"]["layout"]
                        / "set_angle_layout_inventory.json"
                    ).unlink()
                else:
                    prompt_path = prepared["final_dir"] / "main_01_final_prompt.json"
                    final_doc = json.loads(prompt_path.read_text(encoding="utf-8"))
                    if mutation == "upstream":
                        final_doc["upstream_artifacts"]["set_angle_layout_inventory"] = str(
                            prepared["final_dir"] / "wrong-layout.json"
                        )
                    else:
                        final_doc["upstream_artifacts"][
                            "component_identity_archive_03"
                        ] = str(
                            prepared["paths"]["identity"]
                            / "component_03_product_identity_archive.json"
                        )
                    prompt_path.write_text(
                        json.dumps(final_doc, ensure_ascii=False),
                        encoding="utf-8",
                    )
                report = integrity_validator.build_prompts_only_report(
                    batch_manifest_path=prepared["manifest_path"]
                )
                self.assertEqual("fail", report["status"])
                self.assertIn(expected_issue, self.issue_ids(report))

    def test_integrity_rejects_config_prompt_handheld_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            prepared = self.prepare_full_chain(Path(temporary), handheld_target=0)
            prompt_path = prepared["final_dir"] / "main_01_final_prompt.json"
            final_doc = json.loads(prompt_path.read_text(encoding="utf-8"))
            final_doc["final_prompt"] = final_doc["final_prompt"].replace(
                "本张图不启用手持场景",
                "本张图启用手持场景",
            )
            prompt_path.write_text(
                json.dumps(final_doc, ensure_ascii=False),
                encoding="utf-8",
            )
            report = integrity_validator.build_prompts_only_report(
                batch_manifest_path=prepared["manifest_path"]
            )
            self.assertEqual("fail", report["status"])
            self.assertIn("handheld_count_main_mismatch", self.issue_ids(report))

    def test_integrity_rejects_matching_nonzero_handheld_above_manifest_zero(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            prepared = self.prepare_full_chain(Path(temporary), handheld_target=1)
            manifest_path = prepared["manifest_path"]
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["user_confirmed_facts"]["handheld_main"] = 0
            manifest["user_confirmed_facts"]["handheld_detail"] = 0
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            report = integrity_validator.build_prompts_only_report(
                batch_manifest_path=manifest_path
            )
            self.assertEqual("fail", report["status"])
            blocking_by_id = {
                str(issue["issue_id"]): issue for issue in report["blocking_issues"]
            }
            for mode in ("main", "detail"):
                issue = blocking_by_id[f"handheld_count_{mode}_mismatch"]
                self.assertEqual(
                    "Set handheld counts must match between variable configs and final "
                    "prompts without exceeding the batch manifest target.",
                    issue["description"],
                )
                self.assertEqual(
                    {"expected": 0, "variable_config": 1, "final_prompt": 1},
                    issue["evidence"],
                )

    def test_integrity_expectations_keep_set_height_none(self) -> None:
        confirmed_height, expected_handheld, source = integrity_validator.integrity_expectations(
            {
                "category": "杯类",
                "batch_type": "set",
                "user_confirmed_facts": set_manifest_facts(
                    main_count=2,
                    detail_count=2,
                    handheld_main=0,
                    handheld_detail=0,
                ),
            }
        )
        self.assertIsNone(confirmed_height)
        self.assertEqual({"main": 0, "detail": 0}, expected_handheld)
        self.assertEqual("structured", source)

    def test_schema_accepts_single_and_set_but_rejects_mixed_upstreams(self) -> None:
        from jsonschema import Draft202012Validator

        with tempfile.TemporaryDirectory() as temporary:
            prepared = self.prepare_full_chain(Path(temporary), handheld_target=0)
            set_doc = json.loads(
                (prepared["final_dir"] / "main_01_final_prompt.json").read_text(
                    encoding="utf-8"
                )
            )
            single_doc = copy.deepcopy(set_doc)
            single_doc["upstream_artifacts"] = {
                "product_identity_archive": "identity.json",
                "style_master": "style.json",
                "angle_inventory": "angles.json",
                "variable_config": "main.json",
            }
            mixed_doc = copy.deepcopy(single_doc)
            mixed_doc["upstream_artifacts"]["set_product_identity"] = "set.json"
            out_of_range_component_doc = copy.deepcopy(set_doc)
            out_of_range_component_doc["upstream_artifacts"][
                "component_identity_archive_99"
            ] = "component_99.json"

            schema = json.loads(
                (ROOT / "schemas" / "final_prompt.schema.json").read_text(encoding="utf-8")
            )
            validator = Draft202012Validator(schema)
            self.assertEqual([], list(validator.iter_errors(single_doc)))
            self.assertEqual([], list(validator.iter_errors(set_doc)))
            self.assertTrue(list(validator.iter_errors(mixed_doc)))
            self.assertTrue(list(validator.iter_errors(out_of_range_component_doc)))


if __name__ == "__main__":
    unittest.main()
