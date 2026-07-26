from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

import batch_creator as batch_creator_module  # noqa: E402
from batch_creator import BatchCreationError, BatchCreator, prepare_state_root  # noqa: E402


class ProjectDeletionWorkspaceRootTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.manifests = self.repo / "manifests"
        self.state = self.base / "state"
        self.desktop = self.base / "Desktop"
        self.workspace_parent = self.desktop / "杯类"
        self.manifests.mkdir(parents=True)
        self.workspace_parent.mkdir(parents=True)
        prepare_state_root(self.state)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _anchor(self, parent: Path) -> None:
        workspace = parent / "shuiping_20260712"
        workspace.mkdir(parents=True, exist_ok=True)
        (self.manifests / "shuiping_20260712.batch_manifest.json").write_text(
            json.dumps(
                {
                    "product_id": "shuiping_20260712",
                    "workspace": {"root": str(workspace)},
                }
            ),
            encoding="utf-8",
        )

    def test_anchor_and_known_folder_must_agree(self) -> None:
        self._anchor(self.workspace_parent)
        creator = BatchCreator(
            repo_root=self.repo,
            state_root=self.state,
            desktop_locator=lambda: self.desktop,
        )
        self.assertEqual(self.workspace_parent, creator.workspace_parent)

        other_desktop = self.base / "OtherDesktop"
        (other_desktop / "杯类").mkdir(parents=True)
        with self.assertRaises(BatchCreationError) as caught:
            BatchCreator(
                repo_root=self.repo,
                state_root=self.state,
                desktop_locator=lambda: other_desktop,
            )
        self.assertEqual("workspace_root_mismatch", caught.exception.code)

    def test_missing_anchor_uses_injected_known_folder_without_real_probe(self) -> None:
        creator = BatchCreator(
            repo_root=self.repo,
            state_root=self.state,
            desktop_locator=lambda: self.desktop,
        )
        self.assertEqual(self.workspace_parent, creator.workspace_parent)

    def test_anchor_survives_known_folder_api_failure(self) -> None:
        self._anchor(self.workspace_parent)

        def unavailable() -> Path:
            raise OSError("injected known-folder failure")

        creator = BatchCreator(
            repo_root=self.repo,
            state_root=self.state,
            desktop_locator=unavailable,
        )
        self.assertEqual(self.workspace_parent, creator.workspace_parent)

    def test_broken_reparse_anchor_never_falls_back_to_known_folder(self) -> None:
        anchor = (
            self.manifests
            / "shuiping_20260712.batch_manifest.json"
        )
        original_is_reparse = batch_creator_module._is_unsafe_reparse

        def injected_reparse(path: Path) -> bool:
            if Path(path) == anchor:
                return True
            return original_is_reparse(path)

        with mock.patch.object(
            batch_creator_module,
            "_is_unsafe_reparse",
            side_effect=injected_reparse,
        ):
            with self.assertRaises(BatchCreationError) as caught:
                BatchCreator(
                    repo_root=self.repo,
                    state_root=self.state,
                    desktop_locator=lambda: self.desktop,
                )

        self.assertEqual("reparse_point", caught.exception.code)


if __name__ == "__main__":
    unittest.main()
