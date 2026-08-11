from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT / "scripts", ROOT / "canvas-bridge"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import batch_type_gate  # noqa: E402
import detect_current_state  # noqa: E402
import workflow_production_service as production_service  # noqa: E402
from category_recipes import CategoryRecipeError, load_shared_prompt  # noqa: E402
from codex_dev_executor import (  # noqa: E402
    CodexAttachment,
    CodexDevExecutor,
    CodexTurnResult,
)
from executor_contract import (  # noqa: E402
    ExecutionRequest,
    ExecutionResult,
    ExecutorContext,
    ExecutorExecutionError,
)


STEPS = (
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
BLOCKED_MESSAGE = (
    "套装批次的后续生产工序尚未开通，本批次已停在未开通工序开始之前，"
    "未执行该工序，也未产生任何费用。"
)
SET_IDENTITY_ANCHORS = (
    "本档案是套装组合关系层的身份档案，只锁定套装的组合层信息。",
    "件数是套装身份的核心锁定项，后续生图不得增减。",
)
SET_WORKFLOW_SUPPLEMENT_ANCHORS = (
    "因此套装不是单品的同级品类，而是“在单品之上叠加一个组合关系层”。"
    "本文件及其配套源文件，都是为这一组合关系层服务的。",
    "阶段 1 产品身份建档（扩展为两级）",
)
VALID_IDENTITY = {
    "artifact_type": "product_identity_archive",
    "identity": {
        "confirmed_facts": ["已确认事实"],
        "visible_inferences": ["可见推断"],
        "unknowns": ["无法确认"],
        "prohibited_inventions": ["禁止虚构"],
        "product_lock_description": "保持可见结构不变。",
    },
    "missing_information": ["容量无法确认"],
    "blocked_reasons": [],
    "notes": "",
}


def valid_set_identity(component_count: int) -> dict[str, object]:
    return {
        "artifact_type": "set_product_identity",
        "set_identity": {
            "set_name": "测试套装",
            "set_category": "组合产品",
            "set_piece_count": component_count,
            "set_lock_description": "保持组成、主次和相对比例不变。",
        },
        "components": [
            {
                "component_name": f"单件 {index}",
                "quantity": 1,
                "hierarchy": "主件" if index == 1 else "辅件",
            }
            for index in range(1, component_count + 1)
        ],
        "missing_information": [],
        "notes": "",
    }


class SequenceTransport:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, tuple[CodexAttachment, ...]]] = []

    def run_turn(
        self,
        prompt: str,
        attachments: tuple[CodexAttachment, ...],
    ) -> CodexTurnResult:
        self.calls.append((prompt, attachments))
        if not self.responses:
            raise AssertionError("unexpected transport call")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return CodexTurnResult(
            text=str(response),
            thread_id=f"thread-{len(self.calls)}",
        )

    def continue_turn(self, *_args: object) -> CodexTurnResult:
        raise AssertionError("identity must use a fresh thread for every turn")


class ExecutorFixture(unittest.TestCase):
    def make_executor(
        self,
        root: Path,
        responses: list[object],
        *,
        batch_type: str | None = "set",
        component_names: tuple[str, ...] = ("b.png", "A.png"),
        group_names: tuple[str, ...] = ("group.png",),
        repository_root: Path = ROOT,
    ) -> tuple[CodexDevExecutor, SequenceTransport, Path]:
        workspace = root / "workspace"
        white_directory = workspace / "inputs" / "white_bg"
        component_directory = workspace / "inputs" / "component_white_bg"
        group_directory = workspace / "inputs" / "set_group"
        for directory in (white_directory, component_directory, group_directory):
            directory.mkdir(parents=True, exist_ok=True)
        (white_directory / "single.png").write_bytes(b"single")
        for filename in component_names:
            (component_directory / filename).write_bytes(filename.encode("utf-8"))
        for filename in group_names:
            (group_directory / filename).write_bytes(filename.encode("utf-8"))

        identity_directory = workspace / "artifacts" / "identity"
        manifest: dict[str, object] = {
            "product_id": "st02-product",
            "category": "杯类",
            "notes": "只使用可见信息",
            "user_declared_set_product": batch_type == "set",
            "workspace": {
                "root": str(workspace),
                "artifacts_root": str(workspace / "artifacts"),
            },
            "inputs": {
                "white_bg_images": [str(white_directory)],
                "component_white_bg_images": [str(component_directory)],
                "set_group_images": [str(group_directory)],
            },
            "artifacts": {
                "product_identity_archive": str(identity_directory),
                "set_product_identity": str(identity_directory),
            },
        }
        if batch_type is not None:
            manifest["batch_type"] = batch_type
        manifest_path = root / "manifests" / "st02-product.batch_manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        transport = SequenceTransport(responses)
        executor = CodexDevExecutor(
            ExecutorContext(
                manifest=manifest,
                manifest_path=manifest_path,
                environment={"CODEX_DEV_ALLOW_REAL_EXECUTION": "1"},
            ),
            transport=transport,
            repository_root=repository_root,
        )
        return executor, transport, identity_directory

    @staticmethod
    def identity_response() -> str:
        return json.dumps(VALID_IDENTITY, ensure_ascii=False)

    @staticmethod
    def set_response(component_count: int) -> str:
        return json.dumps(valid_set_identity(component_count), ensure_ascii=False)

    @staticmethod
    def execute_identity(executor: CodexDevExecutor) -> ExecutionResult:
        return executor.execute(ExecutionRequest(step="identity"))


class St02GateTests(unittest.TestCase):
    def test_gate_matrix_has_exact_three_branch_behavior(self) -> None:
        self.assertEqual(
            frozenset(
                {
                    "identity",
                    "style_master",
                    "angle_inventory",
                    "main_vc",
                    "detail_vc",
                    "final_prompts",
                    "integrity",
                    "renders",
                }
            ),
            batch_type_gate.SET_READY_STEPS,
        )
        self.assertIsNone(
            batch_type_gate.set_batch_blocked_message(
                {"batch_type": "set"},
                "identity",
            )
        )
        blocked_steps = tuple(
            step
            for step in STEPS
            if step
            not in {
                "identity",
                "style_master",
                "angle_inventory",
                "main_vc",
                "detail_vc",
                "final_prompts",
                "integrity",
                "renders",
            }
        )
        for step in blocked_steps:
            with self.subTest(batch_type="set", step=step):
                self.assertEqual(
                    BLOCKED_MESSAGE,
                    batch_type_gate.set_batch_blocked_message(
                        {"batch_type": "set"},
                        step,
                    ),
                )
        for batch_type in ("bundle", None, True, []):
            for step in ("identity", "style_master"):
                with self.subTest(invalid_batch_type=batch_type, step=step):
                    self.assertEqual(
                        BLOCKED_MESSAGE,
                        batch_type_gate.set_batch_blocked_message(
                            {"batch_type": batch_type},
                            step,
                        ),
                    )
        for manifest in ({"batch_type": "single"}, {}):
            for step in STEPS:
                with self.subTest(manifest=manifest, step=step):
                    self.assertIsNone(
                        batch_type_gate.set_batch_blocked_message(manifest, step)
                    )


class St02SharedPromptTests(ExecutorFixture):
    def test_shared_prompt_loader_returns_approved_sources_with_four_anchors(
        self,
    ) -> None:
        cases = (
            (
                "set_identity_prompt",
                ROOT / "categories" / "_shared" / "prompts" / "set_identity.md",
                ROOT / "套装产品身份档案提示词.txt",
                SET_IDENTITY_ANCHORS,
            ),
            (
                "set_workflow_supplement",
                ROOT
                / "categories"
                / "_shared"
                / "prompts"
                / "set_workflow_supplement.md",
                ROOT / "套装产品工作流补充规则.txt",
                SET_WORKFLOW_SUPPLEMENT_ANCHORS,
            ),
        )
        for key, shared_path, approved_source, anchors in cases:
            with self.subTest(key=key):
                prompt = load_shared_prompt(ROOT, key)
                self.assertTrue(prompt)
                self.assertEqual(shared_path.read_bytes(), approved_source.read_bytes())
                self.assertEqual(shared_path.read_text(encoding="utf-8"), prompt)
                for anchor in anchors:
                    self.assertIn(anchor, prompt)

    def test_shared_prompt_loader_fails_closed_for_four_invalid_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            prompt_directory = repository / "categories" / "_shared" / "prompts"

            with self.assertRaises(CategoryRecipeError) as unknown_caught:
                load_shared_prompt(repository, "unknown_shared_prompt")
            self.assertIs(type(unknown_caught.exception), CategoryRecipeError)
            self.assertEqual("共享提示词键无效", str(unknown_caught.exception))

            with self.assertRaises(CategoryRecipeError) as missing_caught:
                load_shared_prompt(repository, "set_identity_prompt")
            self.assertIs(type(missing_caught.exception), CategoryRecipeError)
            self.assertEqual("共享提示词文件不存在", str(missing_caught.exception))

            prompt_directory.mkdir(parents=True)
            identity_path = prompt_directory / "set_identity.md"
            identity_path.write_bytes(b"")
            with self.assertRaises(CategoryRecipeError) as empty_caught:
                load_shared_prompt(repository, "set_identity_prompt")
            self.assertIs(type(empty_caught.exception), CategoryRecipeError)
            self.assertEqual("共享提示词文件为空", str(empty_caught.exception))

            workflow_path = prompt_directory / "set_workflow_supplement.md"
            workflow_path.write_text(" \n\t\n", encoding="utf-8")
            with self.assertRaises(CategoryRecipeError) as whitespace_caught:
                load_shared_prompt(repository, "set_workflow_supplement")
            self.assertIs(type(whitespace_caught.exception), CategoryRecipeError)
            self.assertEqual("共享提示词文件仅含空白", str(whitespace_caught.exception))

    def test_executor_uses_shared_sources_without_legacy_reference_fallback(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            repository = base / "repository"
            shutil.copytree(ROOT / "categories", repository / "categories")
            for skill_name in ("product-identity-archive", "set-product-identity"):
                shutil.copytree(
                    ROOT / ".agents" / "skills" / skill_name,
                    repository / ".agents" / "skills" / skill_name,
                )

            shared_identity_marker = "SHARED_SET_IDENTITY_SOURCE_ANCHOR"
            shared_workflow_marker = "SHARED_SET_WORKFLOW_SOURCE_ANCHOR"
            shared_prompt_root = repository / "categories" / "_shared" / "prompts"
            (shared_prompt_root / "set_identity.md").write_text(
                shared_identity_marker,
                encoding="utf-8",
            )
            (shared_prompt_root / "set_workflow_supplement.md").write_text(
                shared_workflow_marker,
                encoding="utf-8",
            )

            legacy_reference_markers = (
                "LEGACY_SKILL_SET_IDENTITY_REFERENCE",
                "LEGACY_SKILL_SET_WORKFLOW_REFERENCE",
                "LEGACY_ROOT_SET_IDENTITY_REFERENCE",
                "LEGACY_ROOT_SET_WORKFLOW_REFERENCE",
            )
            legacy_reference_root = (
                repository / ".agents" / "skills" / "set-product-identity" / "references"
            )
            (legacy_reference_root / "套装产品身份档案提示词.txt").write_text(
                legacy_reference_markers[0],
                encoding="utf-8",
            )
            (legacy_reference_root / "套装产品工作流补充规则.txt").write_text(
                legacy_reference_markers[1],
                encoding="utf-8",
            )
            (repository / "套装产品身份档案提示词.txt").write_text(
                legacy_reference_markers[2],
                encoding="utf-8",
            )
            (repository / "套装产品工作流补充规则.txt").write_text(
                legacy_reference_markers[3],
                encoding="utf-8",
            )

            executor, transport, _identity_directory = self.make_executor(
                base / "runtime",
                [
                    self.identity_response(),
                    self.identity_response(),
                    self.set_response(2),
                ],
                repository_root=repository,
            )
            self.execute_identity(executor)

            self.assertEqual(3, len(transport.calls))
            set_prompt = transport.calls[-1][0]
            self.assertIn(shared_identity_marker, set_prompt)
            self.assertIn(shared_workflow_marker, set_prompt)
            for marker in legacy_reference_markers:
                self.assertNotIn(marker, set_prompt)

    def test_executor_translates_shared_prompt_error_before_first_transport(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            executor, transport, identity_directory = self.make_executor(
                Path(temporary),
                [self.identity_response()],
            )
            with mock.patch(
                "codex_dev_executor.load_shared_prompt",
                side_effect=CategoryRecipeError("共享提示词文件不存在"),
            ):
                with self.assertRaises(ExecutorExecutionError) as caught:
                    self.execute_identity(executor)
            self.assertIs(type(caught.exception), ExecutorExecutionError)
            self.assertEqual(
                "codex-dev 无法加载套装产品身份建档规则",
                str(caught.exception),
            )
            self.assertEqual(0, len(transport.calls))
            self.assertEqual([], list(identity_directory.rglob("*.json")))


class St02ExecutorTests(ExecutorFixture):
    def test_two_level_identity_is_ordered_atomic_and_emits_each_turn(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executor, transport, identity_directory = self.make_executor(
                root,
                [
                    self.identity_response(),
                    self.identity_response(),
                    self.set_response(2),
                ],
            )
            progress: list[int] = []
            executor.set_turn_progress_callback(lambda: progress.append(1))

            result = self.execute_identity(executor)

            expected_names = (
                "component_01_product_identity_archive.json",
                "component_02_product_identity_archive.json",
                "set_product_identity.json",
            )
            self.assertEqual(3, len(transport.calls))
            self.assertEqual([1, 1, 1], progress)
            self.assertEqual(
                ["A.png", "b.png", "group.png"],
                [[attachment.name for attachment in call[1]][0] for call in transport.calls],
            )
            self.assertEqual(expected_names, tuple(path.name for path in result.outputs))
            self.assertEqual("套装两级身份档案已生成", result.detail)
            self.assertEqual("thread-3", result.metadata["thread_id"])
            self.assertEqual(
                ["thread-1", "thread-2"],
                result.metadata["component_thread_ids"],
            )
            self.assertEqual(
                expected_names,
                tuple(sorted(path.name for path in identity_directory.glob("*.json"))),
            )

            for index, source_filename in enumerate(("A.png", "b.png"), start=1):
                archive = json.loads(result.outputs[index - 1].read_text(encoding="utf-8"))
                self.assertEqual("product_identity_archive", archive["artifact_type"])
                self.assertEqual(index, archive["component_index"])
                self.assertEqual(source_filename, archive["component_source_image"])
                self.assertEqual([source_filename], archive["source_inputs"])

            set_archive = json.loads(result.outputs[-1].read_text(encoding="utf-8"))
            self.assertEqual("set_product_identity", set_archive["artifact_type"])
            self.assertIs(True, set_archive["user_declared_set_product"])
            self.assertEqual(["group.png"], set_archive["source_inputs"])
            self.assertEqual(2, len(set_archive["components"]))
            for index, source_filename in enumerate(("A.png", "b.png"), start=1):
                component = set_archive["components"][index - 1]
                self.assertEqual(f"单件 {index}", component["component_name"])
                self.assertEqual(1, component["quantity"])
                self.assertIn(component["hierarchy"], {"主件", "辅件"})
                self.assertEqual(index, component["component_index"])
                self.assertEqual(source_filename, component["component_source_image"])
                self.assertEqual(
                    f"component_{index:02d}_product_identity_archive.json",
                    component["identity_archive_file"],
                )
                self.assertTrue(
                    {"identity", "identity_archive", "product_identity_archive"}.isdisjoint(
                        component
                    )
                )
            self.assertIn("套装批次的组成单件建档", transport.calls[0][0])
            self.assertIn("本单件为套装第 1/2 件，文件名 A.png", transport.calls[0][0])
            self.assertIn("《套装产品身份档案》", transport.calls[-1][0])
            self.assertEqual(1, transport.calls[-1][0].count("--- SKILL START ---"))
            self.assertEqual(2, transport.calls[-1][0].count("--- REFERENCE START ---"))
            product_skill = (
                ROOT / ".agents" / "skills" / "product-identity-archive" / "SKILL.md"
            ).read_text(encoding="utf-8")
            category_identity_reference = (
                ROOT / "categories" / "杯类" / "prompts" / "identity.md"
            ).read_text(encoding="utf-8")
            set_skill_root = ROOT / ".agents" / "skills" / "set-product-identity"
            set_skill = (set_skill_root / "SKILL.md").read_text(encoding="utf-8")
            set_identity_reference = (
                ROOT / "categories" / "_shared" / "prompts" / "set_identity.md"
            ).read_text(encoding="utf-8")
            set_workflow_reference = (
                ROOT
                / "categories"
                / "_shared"
                / "prompts"
                / "set_workflow_supplement.md"
            ).read_text(encoding="utf-8")
            self.assertNotEqual(set_identity_reference, set_workflow_reference)
            for component_prompt, _attachments in transport.calls[:2]:
                self.assertIn(product_skill, component_prompt)
                self.assertIn(category_identity_reference, component_prompt)
            set_prompt = transport.calls[-1][0]
            self.assertIn(set_skill, set_prompt)
            self.assertIn(set_identity_reference, set_prompt)
            self.assertIn(set_workflow_reference, set_prompt)
            for anchor in (*SET_IDENTITY_ANCHORS, *SET_WORKFLOW_SUPPLEMENT_ANCHORS):
                self.assertIn(anchor, set_prompt)

    def test_set_rule_loading_failure_stops_before_transport_and_landing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            executor, transport, identity_directory = self.make_executor(
                Path(temporary),
                [self.identity_response()],
            )
            progress: list[int] = []
            executor.set_turn_progress_callback(lambda: progress.append(1))
            with mock.patch.object(
                executor,
                "_load_set_identity_rules",
                side_effect=ExecutorExecutionError("确定性规则加载失败"),
            ):
                with self.assertRaises(ExecutorExecutionError) as caught:
                    self.execute_identity(executor)
            self.assertIn("规则加载失败", str(caught.exception))
            self.assertEqual(0, len(transport.calls))
            self.assertEqual([], progress)
            self.assertEqual([], list(identity_directory.rglob("*.json")))

    def test_component_count_mismatch_rejects_without_landing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            executor, transport, identity_directory = self.make_executor(
                Path(temporary),
                [
                    self.identity_response(),
                    self.identity_response(),
                    self.set_response(1),
                ],
            )
            progress: list[int] = []
            executor.set_turn_progress_callback(lambda: progress.append(1))
            with self.assertRaises(ExecutorExecutionError) as caught:
                self.execute_identity(executor)
            self.assertIn("组成条目数量无效", str(caught.exception))
            self.assertEqual(3, len(transport.calls))
            self.assertEqual([1, 1], progress)
            self.assertEqual([], list(identity_directory.glob("*.json")))

    def test_set_identity_forbidden_fields_are_rejected_at_both_layers(self) -> None:
        mutations = (
            ("qc_results", False),
            ("variable_configs", True),
            ("final_prompt", False),
        )
        for field, nested in mutations:
            with self.subTest(field=field, nested=nested), tempfile.TemporaryDirectory() as temporary:
                response = valid_set_identity(2)
                target = response["set_identity"] if nested else response
                assert isinstance(target, dict)
                target[field] = {"forbidden": True}
                executor, _transport, identity_directory = self.make_executor(
                    Path(temporary),
                    [
                        self.identity_response(),
                        self.identity_response(),
                        json.dumps(response, ensure_ascii=False),
                    ],
                )
                progress: list[int] = []
                executor.set_turn_progress_callback(lambda: progress.append(1))
                with self.assertRaises(ExecutorExecutionError) as caught:
                    self.execute_identity(executor)
                self.assertIn("越界工作流产物", str(caught.exception))
                self.assertEqual([1, 1], progress)
                self.assertEqual([], list(identity_directory.glob("*.json")))

    def test_embedded_component_archives_are_rejected_at_root_and_set_identity(self) -> None:
        mutations = (
            ("identity_archive", False),
            ("product_identity_archive", True),
            ("component_archives", False),
            ("component_identity_archives", True),
            ("identity", False),
        )
        for field, nested in mutations:
            with self.subTest(field=field, nested=nested), tempfile.TemporaryDirectory() as temporary:
                response = valid_set_identity(2)
                target = response["set_identity"] if nested else response
                assert isinstance(target, dict)
                target[field] = copy.deepcopy(VALID_IDENTITY)
                executor, _transport, identity_directory = self.make_executor(
                    Path(temporary),
                    [
                        self.identity_response(),
                        self.identity_response(),
                        json.dumps(response, ensure_ascii=False),
                    ],
                )
                progress: list[int] = []
                executor.set_turn_progress_callback(lambda: progress.append(1))
                with self.assertRaises(ExecutorExecutionError) as caught:
                    self.execute_identity(executor)
                self.assertIn("越界工作流产物", str(caught.exception))
                self.assertEqual([1, 1], progress)
                self.assertEqual([], list(identity_directory.glob("*.json")))

    def test_embedded_archives_inside_component_items_are_rejected(self) -> None:
        for field in (
            "identity_archive",
            "product_identity_archive",
            "component_archives",
            "component_identity_archives",
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary:
                response = valid_set_identity(2)
                components = response["components"]
                assert isinstance(components, list)
                assert isinstance(components[0], dict)
                components[0][field] = copy.deepcopy(VALID_IDENTITY)
                executor, _transport, identity_directory = self.make_executor(
                    Path(temporary),
                    [
                        self.identity_response(),
                        self.identity_response(),
                        json.dumps(response, ensure_ascii=False),
                    ],
                )
                progress: list[int] = []
                executor.set_turn_progress_callback(lambda: progress.append(1))
                with self.assertRaises(ExecutorExecutionError) as caught:
                    self.execute_identity(executor)
                self.assertIn("套装组成条目无效", str(caught.exception))
                self.assertEqual([1, 1], progress)
                self.assertEqual([], list(identity_directory.rglob("*.json")))

    def test_invalid_component_responses_reject_without_landing(self) -> None:
        invalid_responses = (
            "not-json",
            json.dumps({**VALID_IDENTITY, "artifact_type": "wrong"}, ensure_ascii=False),
        )
        for response in invalid_responses:
            with self.subTest(response=response), tempfile.TemporaryDirectory() as temporary:
                executor, transport, identity_directory = self.make_executor(
                    Path(temporary),
                    [response],
                )
                progress: list[int] = []
                executor.set_turn_progress_callback(lambda: progress.append(1))
                with self.assertRaises(ExecutorExecutionError):
                    self.execute_identity(executor)
                self.assertEqual(1, len(transport.calls))
                self.assertEqual([], progress)
                self.assertEqual([], list(identity_directory.glob("*.json")))

    def test_second_component_transport_failure_leaves_no_partial_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            executor, transport, identity_directory = self.make_executor(
                Path(temporary),
                [self.identity_response(), RuntimeError("offline failure")],
            )
            progress: list[int] = []
            executor.set_turn_progress_callback(lambda: progress.append(1))
            with self.assertRaises(ExecutorExecutionError):
                self.execute_identity(executor)
            self.assertEqual(2, len(transport.calls))
            self.assertEqual([1], progress)
            self.assertEqual([], list(identity_directory.glob("*.json")))

    def test_unrelated_json_residue_stops_before_transport(self) -> None:
        for relative_path in (
            Path("unrelated_history.json"),
            Path("historical") / "nested_residue.json",
        ):
            with self.subTest(relative_path=relative_path), tempfile.TemporaryDirectory() as temporary:
                executor, transport, identity_directory = self.make_executor(
                    Path(temporary),
                    [self.identity_response()],
                )
                residue = identity_directory / relative_path
                residue.parent.mkdir(parents=True)
                residue.write_text("{}", encoding="utf-8")
                with self.assertRaises(ExecutorExecutionError) as caught:
                    self.execute_identity(executor)
                self.assertIn("不会覆盖", str(caught.exception))
                self.assertEqual(0, len(transport.calls))
                self.assertEqual(
                    [residue],
                    [
                        item
                        for item in identity_directory.rglob("*")
                        if item.is_file() and item.suffix.lower() == ".json"
                    ],
                )

    def test_component_count_outside_two_through_eight_fails_closed(self) -> None:
        for component_count in (1, 9):
            with self.subTest(component_count=component_count), tempfile.TemporaryDirectory() as temporary:
                names = tuple(f"component-{index}.png" for index in range(component_count))
                executor, transport, identity_directory = self.make_executor(
                    Path(temporary),
                    [],
                    component_names=names,
                )
                with self.assertRaises(ExecutorExecutionError) as caught:
                    self.execute_identity(executor)
                self.assertIn("2–8", str(caught.exception))
                self.assertEqual(0, len(transport.calls))
                self.assertEqual([], list(identity_directory.glob("*.json")))

    def test_group_count_outside_one_through_three_fails_closed(self) -> None:
        for group_count in (0, 4):
            with self.subTest(group_count=group_count), tempfile.TemporaryDirectory() as temporary:
                names = tuple(f"group-{index}.png" for index in range(group_count))
                executor, transport, identity_directory = self.make_executor(
                    Path(temporary),
                    [],
                    group_names=names,
                )
                with self.assertRaises(ExecutorExecutionError) as caught:
                    self.execute_identity(executor)
                self.assertIn("1–3", str(caught.exception))
                self.assertEqual(0, len(transport.calls))
                self.assertEqual([], list(identity_directory.glob("*.json")))

    def test_single_identity_keeps_original_one_turn_one_file_behavior(self) -> None:
        for batch_type in ("single", None):
            with self.subTest(batch_type=batch_type), tempfile.TemporaryDirectory() as temporary:
                executor, transport, _identity_directory = self.make_executor(
                    Path(temporary),
                    [self.identity_response()],
                    batch_type=batch_type,
                )
                result = self.execute_identity(executor)
                self.assertEqual(1, len(transport.calls))
                self.assertIn("单品《产品身份档案》", transport.calls[0][0])
                self.assertEqual(("product_identity_archive.json",), tuple(path.name for path in result.outputs))
                self.assertEqual(
                    "product_identity_archive",
                    json.loads(result.outputs[0].read_text(encoding="utf-8"))["artifact_type"],
                )


class _ServiceCanvasClient:
    def __init__(self, batch_id: str, step: str) -> None:
        self.statuses: list[str] = []
        self.machine = {
            "id": "machine",
            "type": "workflow",
            "position": {"x": 0, "y": 0},
            "width": 420,
            "height": 300,
            "metadata": {
                "content": (
                    "# workflow-production\n"
                    f"# request-id: req-{batch_id}\n"
                    f"run: {step}"
                ),
                "workflowProduction": {
                    "status": "queued",
                    "requestId": f"req-{batch_id}",
                    "batchId": batch_id,
                    "requestedAt": 1_000,
                    "producedCount": 0,
                },
            },
        }
        self.state = {
            "nodes": [
                self.machine,
                {
                    "id": "card",
                    "type": "batch-info",
                    "metadata": {
                        "batchIntake": {
                            "status": "completed",
                            "receipt": {"batchId": batch_id, "imageCount": 1},
                        }
                    },
                },
                {
                    "id": "source",
                    "type": "image",
                    "metadata": {
                        "content": "blob:source",
                        "storageKey": "image:source",
                    },
                },
            ],
            "connections": [
                {"id": "card-machine", "fromNodeId": "card", "toNodeId": "machine"},
                {"id": "source-machine", "fromNodeId": "source", "toNodeId": "machine"},
            ],
        }

    def call_tool(self, name: str) -> dict[str, object]:
        if name != "canvas_get_state":
            raise AssertionError(name)
        return self.state

    def apply_ops(self, ops: list[dict[str, object]]) -> int:
        for op in ops:
            if op.get("type") != "update_node" or op.get("id") != "machine":
                raise AssertionError(op)
            metadata = op.get("metadata", {})
            if not isinstance(metadata, dict):
                raise AssertionError(op)
            self.machine["metadata"] = {
                **self.machine["metadata"],
                **metadata,
            }
            production = metadata.get("workflowProduction")
            if isinstance(production, dict) and isinstance(production.get("status"), str):
                self.statuses.append(production["status"])
        return len(ops)


class _ServiceExecutor:
    name = "st02-offline"

    def __init__(self, executed: list[str], on_execute=lambda: None) -> None:
        self.executed = executed
        self.on_execute = on_execute
        self.turn_progress_callback = None

    def set_turn_progress_callback(self, callback: object) -> None:
        self.turn_progress_callback = callback

    def execute(self, request: object) -> ExecutionResult:
        self.executed.append(request.step)
        if callable(self.turn_progress_callback):
            self.turn_progress_callback()
        self.on_execute()
        return ExecutionResult(detail="offline-ok", provider=self.name)


class _ForbiddenServiceExecutor:
    name = "st02-forbidden"

    def execute(self, _request: object) -> ExecutionResult:
        raise AssertionError("set batch gate must stop before executor execution")


class _FakeHeartbeatWorker:
    def __init__(self) -> None:
        self.submitted: list[list[dict[str, object]]] = []
        self.alive = True

    def submit(self, ops: list[dict[str, object]]) -> None:
        self.submitted.append(ops)

    def close(self, *, drain: bool) -> None:
        self.drain = drain
        self.alive = False


class St02ServiceTests(unittest.TestCase):
    _STEP_ROUTES = {
        "identity": ("needs_product_identity_archive", "product-identity-archive"),
        "style_master": ("needs_style_master", "style-master-extractor"),
        "angle_inventory": ("needs_angle_inventory", "angle-inventory"),
        "main_vc": ("needs_main_variable_configs", "main-variable-config"),
        "detail_vc": ("needs_detail_variable_configs", "detail-variable-config"),
        "final_prompts": ("needs_final_prompts", "final-prompt-compiler"),
        "qc": ("needs_qc_reports", "qc-inspector"),
    }

    @staticmethod
    def prepare_repository(root: Path) -> Path:
        repository = root / "repo"
        (repository / "manifests").mkdir(parents=True)
        shutil.copytree(ROOT / "categories", repository / "categories")
        return repository

    @staticmethod
    def write_manifest(
        repository: Path,
        root: Path,
        batch_id: str,
        batch_type: str | None,
    ) -> Path:
        workspace = root / f"workspace-{batch_id}"
        style_directory = workspace / "inputs" / "style_refs"
        style_directory.mkdir(parents=True)
        (style_directory / "style.jpg").write_bytes(b"style")
        (workspace / "outputs" / "renders").mkdir(parents=True)
        (workspace / ".canvas_batch").write_text(
            json.dumps({"type": "canvas-batch-v1", "product_id": batch_id}),
            encoding="utf-8",
        )
        manifest: dict[str, object] = {
            "product_id": batch_id,
            "category": "杯类",
            "requested_outputs": ["main", "detail", "final_prompts", "qc_reports"],
            "workspace": {"root": str(workspace)},
            "inputs": {"style_reference_images": [str(style_directory)]},
            "user_confirmed_facts": {
                "main_image_count": 1,
                "detail_image_count": 1,
            },
            "drafts": {},
            "artifacts": {},
            "outputs": {
                "renders": [str(workspace / "outputs" / "renders")],
                "repaired": [],
            },
        }
        if batch_type is not None:
            manifest["batch_type"] = batch_type
        manifest_path = repository / "manifests" / f"{batch_id}.batch_manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        return repository / "manifests" / f"{batch_id}.events.jsonl"

    @classmethod
    def route_and_integrity(
        cls,
        step: str,
    ) -> tuple[dict[str, object], dict[str, object]]:
        if step in {"integrity", "renders"}:
            return (
                {
                    "current_stage": "needs_generated_images_before_qc",
                    "next_required_skill": None,
                    "blocked_reasons": ["QC is post-generation only"],
                    "available_artifacts": ["final_prompts"],
                    "outputs": {
                        "renders": {"file_count": 0},
                        "repaired": {"file_count": 0},
                    },
                    "inputs": {"style_reference_images": {"file_count": 1}},
                },
                {
                    "found": step == "renders",
                    "status": "pass" if step == "renders" else "missing",
                    "render_blocked": False,
                },
            )
        stage, skill = cls._STEP_ROUTES[step]
        return (
            {
                "current_stage": stage,
                "next_required_skill": skill,
                "blocked_reasons": [],
                "available_artifacts": ["final_prompts"] if step == "qc" else [],
                "outputs": {
                    "renders": {"file_count": 2 if step == "qc" else 0},
                    "repaired": {"file_count": 0},
                },
                "inputs": {"style_reference_images": {"file_count": 1}},
            },
            {"found": False, "status": "missing", "render_blocked": False},
        )

    @staticmethod
    def event_names(journal: Path) -> list[str]:
        if not journal.exists():
            return []
        return [
            str(json.loads(line).get("event") or "")
            for line in journal.read_text(encoding="utf-8").splitlines()
        ]

    def make_service(
        self,
        repository: Path,
        root: Path,
        client: _ServiceCanvasClient,
        executor_builder: object,
        route_reader: object,
        integrity_reader: object,
    ) -> production_service.WorkflowProductionService:
        return production_service.WorkflowProductionService(
            repository,
            client=client,
            executor_builder=executor_builder,
            route_reader=route_reader,
            integrity_reader=integrity_reader,
            artifact_reader=lambda _manifest: (),
            render_artifact_reader=lambda _manifest: (),
            repaired_artifact_reader=lambda _manifest: (),
            clock_ms=lambda: 1_100,
            environment={},
            batch_lock_root=root / "locks",
            persistence_timeout_ms=0,
        )

    def test_set_identity_runs_then_rotates_to_the_render_fee_gate_with_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self.prepare_repository(root)
            batch_id = "set-identity"
            journal = self.write_manifest(repository, root, batch_id, "set")
            client = _ServiceCanvasClient(batch_id, "identity")
            identity_route, identity_integrity = self.route_and_integrity("identity")
            style_route, style_integrity = self.route_and_integrity("renders")
            advanced = {"value": False}
            built: list[str] = []
            executed: list[str] = []
            executors: list[_ServiceExecutor] = []

            def build_executor(step: str, *_args: object) -> _ServiceExecutor:
                built.append(step)
                executor = _ServiceExecutor(
                    executed,
                    on_execute=lambda: advanced.__setitem__("value", True),
                )
                executors.append(executor)
                return executor

            service = self.make_service(
                repository,
                root,
                client,
                build_executor,
                lambda _path: style_route if advanced["value"] else identity_route,
                lambda _route: style_integrity if advanced["value"] else identity_integrity,
            )
            worker = _FakeHeartbeatWorker()
            with mock.patch.object(
                service,
                "_start_qc_heartbeat_worker",
                return_value=worker,
            ) as start_worker:
                service.poll_once()

            events = self.event_names(journal)
            event_records = [
                json.loads(line)
                for line in journal.read_text(encoding="utf-8").splitlines()
            ]
            production = client.machine["metadata"]["workflowProduction"]
            self.assertEqual(["identity"], built)
            self.assertEqual(["identity"], executed)
            self.assertEqual(1, events.count("step_started"))
            self.assertEqual(1, events.count("step_succeeded"))
            self.assertEqual(0, events.count("step_auto_retry"))
            self.assertEqual(1, events.count("production_paused"))
            self.assertEqual(
                ["awaiting_render_gate"],
                [
                    event.get("reason")
                    for event in event_records
                    if event.get("event") == "production_paused"
                ],
            )
            self.assertEqual("paused", production["status"])
            self.assertEqual(
                "上游准备完成，已停在出图前。等待批准下一闸门。",
                production["message"],
            )
            self.assertIn("running", client.statuses)
            self.assertEqual("paused", client.statuses[-1])
            start_worker.assert_called_once_with(f"req-{batch_id}")
            self.assertTrue(callable(executors[0].turn_progress_callback))
            self.assertEqual(1, len(worker.submitted))
            self.assertIs(True, worker.drain)

    def test_set_other_eight_steps_stop_before_executor_and_events(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self.prepare_repository(root)
            blocked_steps = tuple(
                step
                for step in STEPS
                if step
                not in {
                    "identity",
                    "style_master",
                    "angle_inventory",
                    "main_vc",
                    "detail_vc",
                    "final_prompts",
                    "integrity",
                    "renders",
                }
            )
            for index, step in enumerate(blocked_steps, start=1):
                with self.subTest(step=step):
                    batch_id = f"blocked-{index}"
                    journal = self.write_manifest(repository, root, batch_id, "set")
                    client = _ServiceCanvasClient(batch_id, step)
                    route, integrity = self.route_and_integrity(step)
                    built: list[str] = []

                    def forbidden_builder(
                        built_step: str,
                        *_args: object,
                    ) -> _ForbiddenServiceExecutor:
                        built.append(built_step)
                        return _ForbiddenServiceExecutor()

                    service = self.make_service(
                        repository,
                        root,
                        client,
                        forbidden_builder,
                        lambda _path, route=route: route,
                        lambda _route, integrity=integrity: integrity,
                    )
                    service.poll_once()

                    events = self.event_names(journal)
                    production = client.machine["metadata"]["workflowProduction"]
                    self.assertEqual("failed", production["status"])
                    self.assertEqual(BLOCKED_MESSAGE, production["errorMessage"])
                    self.assertEqual([], built)
                    self.assertEqual(0, events.count("step_started"))
                    self.assertEqual(0, events.count("step_auto_retry"))
                    self.assertEqual(0, events.count("production_paused"))

    def test_single_and_missing_identity_do_not_start_or_bind_turn_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self.prepare_repository(root)
            identity_route, integrity = self.route_and_integrity("identity")
            for index, batch_type in enumerate(("single", None), start=1):
                with self.subTest(batch_type=batch_type):
                    batch_id = f"single-heartbeat-{index}"
                    journal = self.write_manifest(
                        repository,
                        root,
                        batch_id,
                        batch_type,
                    )
                    client = _ServiceCanvasClient(batch_id, "identity")
                    executed: list[str] = []
                    executors: list[_ServiceExecutor] = []
                    service: production_service.WorkflowProductionService

                    def build_executor(
                        _step: str,
                        *_args: object,
                    ) -> _ServiceExecutor:
                        executor = _ServiceExecutor(
                            executed,
                            on_execute=lambda: setattr(service, "stopping", True),
                        )
                        executors.append(executor)
                        return executor

                    service = self.make_service(
                        repository,
                        root,
                        client,
                        build_executor,
                        lambda _path: identity_route,
                        lambda _route: integrity,
                    )
                    with mock.patch.object(
                        service,
                        "_start_qc_heartbeat_worker",
                    ) as start_worker:
                        service.poll_once()

                    self.assertEqual(["identity"], executed)
                    self.assertIsNone(executors[0].turn_progress_callback)
                    start_worker.assert_not_called()
                    events = self.event_names(journal)
                    self.assertEqual(1, events.count("step_started"))
                    self.assertEqual(1, events.count("step_succeeded"))


class St02RoutingAndBuilderTests(ExecutorFixture):
    @staticmethod
    def make_repository_fixture(root: Path) -> None:
        (root / "scripts").mkdir(parents=True)
        (root / "canvas-bridge").mkdir()
        (root / "manifests").mkdir()
        shutil.copy2(ROOT / "scripts" / "build_batch_manifest.py", root / "scripts")
        shutil.copy2(
            ROOT / "canvas-bridge" / "category_recipes.py",
            root / "canvas-bridge",
        )
        shutil.copy2(
            ROOT / "canvas-bridge" / "image_count_contract.py",
            root / "canvas-bridge",
        )
        shutil.copytree(ROOT / "categories", root / "categories")
        shutil.copy2(
            ROOT / "manifests" / "batch_manifest.template.json",
            root / "manifests",
        )
        shutil.copy2(
            ROOT / "manifests" / "asset_manifest.template.json",
            root / "manifests",
        )

    def test_real_batch_build_and_two_level_archives_route_to_style_master(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            repository = base / "repo"
            workspace = base / "workspace"
            self.make_repository_fixture(repository)
            product_id = "st02_real_builder_set"
            command = self.builder_command(
                product_id,
                "set",
                workspace,
                script_path=repository / "scripts" / "build_batch_manifest.py",
                dry_run=False,
            )
            self.assertNotIn("--dry-run", command)
            environment = os.environ.copy()
            environment["PYTHONUTF8"] = "1"
            environment["PYTHONIOENCODING"] = "utf-8"
            completed = subprocess.run(
                command,
                cwd=repository,
                capture_output=True,
                check=False,
                env=environment,
            )
            self.assertEqual(
                0,
                completed.returncode,
                completed.stderr.decode("utf-8", errors="replace"),
            )
            payload = json.loads(completed.stdout.decode("utf-8"))
            self.assertEqual("created", payload["status"])
            manifest_path = Path(payload["manifest"])
            workspace_manifest_path = Path(payload["workspace_manifest"])
            asset_manifest_path = Path(payload["asset_manifest"])
            self.assertTrue(manifest_path.is_file())
            self.assertTrue(workspace_manifest_path.is_file())
            self.assertTrue(asset_manifest_path.is_file())
            for directory in payload["directories"]:
                self.assertTrue(Path(directory).is_dir(), directory)

            component_directory = workspace / "inputs" / "component_white_bg"
            group_directory = workspace / "inputs" / "set_group"
            (component_directory / "单件二.png").write_bytes(b"two")
            (component_directory / "单件一.png").write_bytes(b"one")
            (group_directory / "套装合影.png").write_bytes(b"group")

            manifest = json.loads(workspace_manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(product_id, manifest["product_id"])
            self.assertEqual("set", manifest["batch_type"])
            self.assertEqual(
                manifest["artifacts"]["product_identity_archive"],
                manifest["artifacts"]["set_product_identity"],
            )
            self.assertEqual(
                manifest["artifacts"]["angle_inventory"],
                manifest["artifacts"]["set_angle_layout_inventory"],
            )
            transport = SequenceTransport(
                [
                    self.identity_response(),
                    self.identity_response(),
                    self.set_response(2),
                ]
            )
            executor = CodexDevExecutor(
                ExecutorContext(
                    manifest=manifest,
                    manifest_path=workspace_manifest_path,
                    environment={"CODEX_DEV_ALLOW_REAL_EXECUTION": "1"},
                ),
                transport=transport,
                repository_root=ROOT,
            )
            self.execute_identity(executor)

            route = detect_current_state.inspect_batch(
                repository,
                product_id,
            )
            self.assertEqual("needs_style_master", route["current_stage"])
            self.assertIn("product_identity_archive", route["available_artifacts"])
            self.assertIn("set_product_identity", route["available_artifacts"])
            product_counts = route["artifacts"]["product_identity_archive"][
                "typed_artifact_counts"
            ]
            set_counts = route["artifacts"]["set_product_identity"][
                "typed_artifact_counts"
            ]
            self.assertEqual(2, product_counts["product_identity_archive"])
            self.assertEqual(1, product_counts["set_product_identity"])
            self.assertEqual(product_counts, set_counts)

    @staticmethod
    def builder_command(
        product_id: str,
        batch_type: str,
        workspace: Path | None,
        *,
        script_path: Path | None = None,
        dry_run: bool = True,
    ) -> list[str]:
        command = [
            sys.executable,
            "-B",
            str(script_path or ROOT / "scripts" / "build_batch_manifest.py"),
            "--product-id",
            product_id,
            "--product-type",
            "杯子",
            "--batch-type",
            batch_type,
            "--category",
            "杯类",
            "--height-cm",
            "25",
            "--main-count",
            "1",
            "--detail-count",
            "1",
            "--handheld-main",
            "0",
            "--handheld-detail",
            "0",
            "--forbid-pouring-and-heating",
            "true",
            "--missing-d-no-retake",
            "true",
        ]
        if workspace is not None:
            command.extend(("--workspace-root", str(workspace)))
        if dry_run:
            command.append("--dry-run")
        return command

    def test_builder_both_paths_and_batch_types_publish_both_identity_pointers(self) -> None:
        environment = os.environ.copy()
        environment["PYTHONUTF8"] = "1"
        environment["PYTHONIOENCODING"] = "utf-8"
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            for mode in ("external", "repository"):
                for batch_type in ("single", "set"):
                    with self.subTest(mode=mode, batch_type=batch_type):
                        product_id = f"st02_builder_{mode}_{batch_type}"
                        workspace = (
                            base / f"workspace-{batch_type}"
                            if mode == "external"
                            else None
                        )
                        completed = subprocess.run(
                            self.builder_command(product_id, batch_type, workspace),
                            cwd=ROOT,
                            capture_output=True,
                            check=False,
                            env=environment,
                        )
                        self.assertEqual(
                            0,
                            completed.returncode,
                            completed.stderr.decode("utf-8", errors="replace"),
                        )
                        payload = json.loads(completed.stdout.decode("utf-8"))
                        manifest = payload["manifest_data"]
                        product_pointer = manifest["artifacts"][
                            "product_identity_archive"
                        ]
                        set_product_pointer = manifest["artifacts"][
                            "set_product_identity"
                        ]
                        angle_pointer = manifest["artifacts"]["angle_inventory"]
                        set_angle_pointer = manifest["artifacts"][
                            "set_angle_layout_inventory"
                        ]
                        for pointer in (
                            product_pointer,
                            set_product_pointer,
                            angle_pointer,
                            set_angle_pointer,
                        ):
                            self.assertIsInstance(pointer, str)
                            self.assertTrue(pointer)
                        self.assertEqual(product_pointer, set_product_pointer)
                        self.assertEqual(angle_pointer, set_angle_pointer)
                        self.assertEqual(batch_type, manifest["batch_type"])
                        if workspace is not None:
                            self.assertFalse(workspace.exists())
