from __future__ import annotations

import json
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
BRIDGE_ROOT = REPO_ROOT / "canvas-bridge"
if str(BRIDGE_ROOT) not in sys.path:
    sys.path.insert(0, str(BRIDGE_ROOT))

import batch_creator
from batch_creator import BatchCreationError, BatchCreator, FROZEN_PRODUCT_ID
from launcher import canvas_launcher, runtime_paths
from launcher.config import load_config
from launcher.orchestrator import build_service_specs

import make_demo_workspace


DEFAULT_CONFIG = REPO_ROOT / "launcher" / "launcher_config.json"


class RuntimePathTests(unittest.TestCase):
    def test_repo_and_project_roots_are_derived_from_launcher_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="伞形根（测试）-") as raw:
            project = Path(raw)
            repo = project / "杯类代码仓库（水壶类）"
            launcher_dir = repo / "launcher"
            launcher_dir.mkdir(parents=True)

            self.assertEqual(runtime_paths.repo_root(launcher_dir), repo)
            self.assertEqual(runtime_paths.project_root(launcher_dir), project)

    def test_unknown_placeholder_keeps_existing_error_message(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "^启动配置包含未知占位符：missing$"):
            runtime_paths.expand_template("{missing}", {})

    def test_find_bun_returns_absolute_hit_or_literal_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            executable = Path(raw) / "bun.exe"
            executable.touch()
            with mock.patch.object(runtime_paths.shutil, "which", return_value=str(executable)):
                self.assertEqual(runtime_paths.find_bun(), str(executable.resolve()))
        with mock.patch.object(runtime_paths.shutil, "which", return_value=None):
            self.assertEqual(runtime_paths.find_bun(), "bun")

    def test_resolve_dist_root_expands_project_root(self) -> None:
        with tempfile.TemporaryDirectory(prefix="伞形根（测试）-") as raw:
            project = Path(raw)
            launcher_dir = project / "主仓（水壶类）" / "launcher"
            launcher_dir.mkdir(parents=True)
            config = {"web": {"dist": {"root": "{project_root}/infinite-canvas/web/dist"}}}

            resolved = runtime_paths.resolve_dist_root(config, launcher_dir)

            self.assertEqual(resolved, project / "infinite-canvas" / "web" / "dist")


class OrchestratorPortabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="伞形根（测试）-")
        self.project = Path(self.temp.name)
        self.repo = self.project / "杯类代码仓库（水壶类）"
        self.launcher_dir = self.repo / "launcher"
        self.launcher_dir.mkdir(parents=True)
        self.pythonw = self.project / "Python" / "pythonw.exe"
        self.config = load_config(DEFAULT_CONFIG, override_path=self.project / "missing-override.json")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _specs(self, config: dict[str, object] | None = None) -> dict[str, object]:
        with mock.patch.object(runtime_paths, "find_bun", return_value="C:/portable/bun.exe"):
            specs = build_service_specs(
                config or self.config,
                launcher_dir=self.launcher_dir,
                pythonw_path=self.pythonw,
            )
        return {spec.name: spec for spec in specs}

    def test_agent_command_uses_sibling_fork(self) -> None:
        agent = self._specs()["agent"]
        self.assertEqual(
            agent.command,
            (
                "C:/portable/bun.exe",
                "run",
                "--cwd",
                f"{self.project}/infinite-canvas/canvas-agent",
                "dev",
            ),
        )

    def test_workbench_script_and_cwd_use_repository_root(self) -> None:
        workbench = self._specs()["workbench"]
        self.assertEqual(workbench.command[1], str(self.repo / "canvas-bridge" / "spike_canvas_push.py"))
        self.assertEqual(workbench.cwd, self.repo)

    def test_dist_command_contains_expanded_dist_root(self) -> None:
        web = self._specs()["web"]
        expected = str(self.project / "infinite-canvas" / "web" / "dist")
        self.assertEqual(web.command[3], expected)
        self.assertNotIn("{", " ".join(web.command))
        self.assertNotIn("}", " ".join(web.command))

    def test_existing_pythonw_and_launcher_placeholders_are_unchanged(self) -> None:
        web = self._specs()["web"]
        self.assertEqual(web.command[0], str(self.pythonw))
        self.assertEqual(web.command[1], str(self.launcher_dir.resolve() / "static_server.py"))
        self.assertEqual(web.cwd, self.launcher_dir.resolve())

    def test_override_is_merged_and_its_placeholders_are_expanded(self) -> None:
        override = self.project / "config.override.json"
        override.write_text(
            json.dumps(
                {
                    "services": {"agent": {"cwd": "{repo_root}/overridden-agent"}},
                    "web": {"dist": {"root": "{repo_root}/overridden-dist"}},
                }
            ),
            encoding="utf-8",
        )
        config = load_config(DEFAULT_CONFIG, override_path=override)

        specs = self._specs(config)

        self.assertEqual(specs["agent"].cwd, self.repo / "overridden-agent")
        self.assertEqual(specs["web"].command[3], str(self.repo / "overridden-dist"))
        self.assertEqual(config["runtime"]["startup_timeout_seconds"], 60)


class LauncherBunGuardTests(unittest.TestCase):
    def test_startup_fails_closed_with_clear_message_when_bun_is_missing(self) -> None:
        config = load_config(DEFAULT_CONFIG, override_path=REPO_ROOT / "missing-override.json")
        logger = mock.Mock(spec=logging.Logger)
        message_box = mock.Mock()
        specs = mock.Mock()
        with (
            mock.patch.object(canvas_launcher, "load_config", return_value=config),
            mock.patch.object(canvas_launcher, "configure_launcher_logger", return_value=logger),
            mock.patch.object(canvas_launcher, "_resolve_pythonw", return_value=Path("C:/Python/pythonw.exe")),
            mock.patch.object(canvas_launcher.shutil, "which", return_value=None),
            mock.patch.object(canvas_launcher, "show_message_box", message_box),
            mock.patch.object(canvas_launcher, "build_service_specs", specs),
        ):
            result = canvas_launcher.main()

        self.assertEqual(result, 1)
        self.assertIn("未找到 bun", message_box.call_args.args[1])
        specs.assert_not_called()

    def test_spec_construction_still_works_when_bun_is_missing(self) -> None:
        config = load_config(DEFAULT_CONFIG, override_path=REPO_ROOT / "missing-override.json")
        with mock.patch.object(runtime_paths.shutil, "which", return_value=None):
            specs = build_service_specs(
                config,
                launcher_dir=REPO_ROOT / "launcher",
                pythonw_path=Path("C:/Python/pythonw.exe"),
            )

        self.assertEqual(specs[0].command[0], "bun")


class BatchProjectParentTests(unittest.TestCase):
    def _layout(self, raw: str) -> tuple[Path, Path, Path]:
        project = Path(raw)
        repo = project / "主仓（水壶类）"
        (repo / "manifests").mkdir(parents=True)
        state = project / "state"
        return project, repo, state

    def _write_frozen_manifest(self, repo: Path, parent: Path) -> None:
        value = {
            "product_id": FROZEN_PRODUCT_ID,
            "workspace": {"root": str(parent / FROZEN_PRODUCT_ID)},
        }
        (repo / "manifests" / f"{FROZEN_PRODUCT_ID}.batch_manifest.json").write_text(
            json.dumps(value, ensure_ascii=False),
            encoding="utf-8",
        )

    def _creator(
        self,
        repo: Path,
        state: Path,
        *,
        desktop: Path | None = None,
    ) -> BatchCreator:
        locator = (lambda: desktop) if desktop is not None else None
        return BatchCreator(
            repo_root=repo,
            state_root=state,
            desktop_locator=locator,
            batch_lock_factory=None,
        )

    def test_project_parent_with_matching_anchor_is_selected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="伞形根（测试）-") as raw:
            project, repo, state = self._layout(raw)
            project_parent = project / "杯类"
            project_parent.mkdir()
            self._write_frozen_manifest(repo, project_parent)

            creator = self._creator(repo, state)

            self.assertEqual(creator.workspace_parent, project_parent.resolve())

    def test_project_parent_rejects_mismatched_anchor(self) -> None:
        with tempfile.TemporaryDirectory(prefix="伞形根（测试）-") as raw:
            project, repo, state = self._layout(raw)
            (project / "杯类").mkdir()
            legacy_parent = project / "旧批次位置"
            legacy_parent.mkdir()
            self._write_frozen_manifest(repo, legacy_parent)

            with self.assertRaises(BatchCreationError) as caught:
                self._creator(repo, state)

            self.assertEqual(caught.exception.code, "workspace_root_mismatch")
            self.assertEqual(
                caught.exception.user_message,
                "项目内批次目录与既有登记位置不一致，已安全停止。",
            )

    def test_project_parent_accepts_unavailable_old_anchor(self) -> None:
        with tempfile.TemporaryDirectory(prefix="伞形根（测试）-") as raw:
            project, repo, state = self._layout(raw)
            project_parent = project / "杯类"
            project_parent.mkdir()
            self._write_frozen_manifest(repo, project / "已搬走的旧位置")

            creator = self._creator(repo, state)

            self.assertEqual(creator.workspace_parent, project_parent.resolve())

    def test_project_parent_rejects_mismatched_desktop_parent(self) -> None:
        with tempfile.TemporaryDirectory(prefix="伞形根（测试）-") as raw:
            project, repo, state = self._layout(raw)
            (project / "杯类").mkdir()
            desktop = project / "桌面"
            (desktop / "杯类").mkdir(parents=True)

            with self.assertRaises(BatchCreationError) as caught:
                self._creator(repo, state, desktop=desktop)

            self.assertEqual(caught.exception.code, "workspace_root_mismatch")
            self.assertEqual(
                caught.exception.user_message,
                "既有批次目录与 Windows 桌面位置不一致，已安全停止。",
            )

    def test_missing_project_parent_preserves_all_legacy_routes(self) -> None:
        with self.subTest(route="anchor"), tempfile.TemporaryDirectory(prefix="伞形根（测试）-") as raw:
            project, repo, state = self._layout(raw)
            anchor = project / "旧批次位置"
            anchor.mkdir()
            self._write_frozen_manifest(repo, anchor)
            self.assertEqual(self._creator(repo, state).workspace_parent, anchor.resolve())

        with self.subTest(route="desktop"), tempfile.TemporaryDirectory(prefix="伞形根（测试）-") as raw:
            project, repo, state = self._layout(raw)
            desktop = project / "桌面"
            desktop_parent = desktop / "杯类"
            desktop_parent.mkdir(parents=True)
            self.assertEqual(
                self._creator(repo, state, desktop=desktop).workspace_parent,
                desktop_parent.resolve(),
            )

        with self.subTest(route="missing"), tempfile.TemporaryDirectory(prefix="伞形根（测试）-") as raw:
            _, repo, state = self._layout(raw)
            with self.assertRaises(BatchCreationError) as caught:
                self._creator(repo, state)
            self.assertEqual(caught.exception.code, "invalid_repository")
            self.assertEqual(caught.exception.user_message, "无法核对批次工作区父目录，已停止登记。")

    def test_project_parent_reparse_is_refused(self) -> None:
        with tempfile.TemporaryDirectory(prefix="伞形根（测试）-") as raw:
            project, repo, state = self._layout(raw)
            project_parent = project / "杯类"
            project_parent.mkdir()
            original = batch_creator._is_unsafe_reparse

            def mark_project_parent_unsafe(path: Path) -> bool:
                return Path(path) == project_parent or original(path)

            with mock.patch.object(batch_creator, "_is_unsafe_reparse", side_effect=mark_project_parent_unsafe):
                with self.assertRaises(BatchCreationError) as caught:
                    self._creator(repo, state)

            self.assertEqual(caught.exception.code, "reparse_point")


class DemoWorkspaceDefaultTests(unittest.TestCase):
    def test_default_root_is_sibling_of_repository(self) -> None:
        self.assertEqual(
            make_demo_workspace.DEFAULT_ROOT,
            REPO_ROOT.parent / "canvas-demo-workspace",
        )


if __name__ == "__main__":
    unittest.main()
