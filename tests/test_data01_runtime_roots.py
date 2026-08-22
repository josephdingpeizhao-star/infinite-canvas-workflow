from __future__ import annotations

import ctypes
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

import runtime_roots  # noqa: E402


class RuntimeRootsTest(unittest.TestCase):
    def setUp(self) -> None:
        runtime_roots.reset_data_root_cache_for_tests()

    def tearDown(self) -> None:
        runtime_roots.reset_data_root_cache_for_tests()

    def test_environment_root_always_precedes_windows_documents(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            configured = Path(temporary) / "configured-data"
            with (
                mock.patch.dict(
                    os.environ,
                    {runtime_roots.DATA_ROOT_ENV: str(configured)},
                    clear=True,
                ),
                mock.patch.object(runtime_roots.os, "name", "nt"),
                mock.patch.object(
                    runtime_roots,
                    "_windows_documents_directory",
                    side_effect=AssertionError("Documents must not be consulted"),
                ) as documents,
            ):
                self.assertEqual(configured.resolve(), runtime_roots.resolve_data_root())
                self.assertEqual(
                    configured.resolve() / "workflow-runtime",
                    runtime_roots.repository_root(),
                )
            documents.assert_not_called()

    def test_environment_root_rejects_blank_and_relative_values_without_fallback(self) -> None:
        for raw in ("   ", "relative/data"):
            with self.subTest(raw=raw):
                runtime_roots.reset_data_root_cache_for_tests()
                with (
                    mock.patch.dict(
                        os.environ,
                        {runtime_roots.DATA_ROOT_ENV: raw},
                        clear=True,
                    ),
                    mock.patch.object(
                        runtime_roots,
                        "_windows_documents_directory",
                        side_effect=AssertionError("invalid env must fail closed"),
                    ) as documents,
                ):
                    with self.assertRaises(ValueError) as caught:
                        runtime_roots.resolve_data_root()
                self.assertIn(runtime_roots.DATA_ROOT_ENV, str(caught.exception))
                documents.assert_not_called()

    def test_windows_known_folder_api_supplies_redirected_documents(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            documents = Path(temporary) / "Redirected Documents"
            allocated = ctypes.create_unicode_buffer(str(documents))
            calls: list[str] = []

            def known_folder(_folder_id, _flags, _token, out_pointer) -> int:
                calls.append("resolve")
                ctypes.cast(out_pointer, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.cast(
                    allocated,
                    ctypes.c_void_p,
                )
                return 0

            def free_pointer(_pointer) -> None:
                calls.append("free")

            fake_windll = SimpleNamespace(
                shell32=SimpleNamespace(SHGetKnownFolderPath=known_folder),
                ole32=SimpleNamespace(CoTaskMemFree=free_pointer),
            )
            with (
                mock.patch.dict(os.environ, {}, clear=True),
                mock.patch.object(runtime_roots.os, "name", "nt"),
                mock.patch.object(
                    runtime_roots.ctypes,
                    "windll",
                    fake_windll,
                    create=True,
                ),
            ):
                resolved = runtime_roots.resolve_data_root()

            self.assertEqual(
                (documents / runtime_roots.DATA_ROOT_NAME).resolve(),
                resolved,
            )
            self.assertEqual(["resolve", "free"], calls)

    def test_windows_known_folder_api_failure_stops_without_fallback(self) -> None:
        calls: list[str] = []

        def known_folder(_folder_id, _flags, _token, _out_pointer) -> int:
            calls.append("resolve")
            return 5

        fake_windll = SimpleNamespace(
            shell32=SimpleNamespace(SHGetKnownFolderPath=known_folder),
            ole32=SimpleNamespace(
                CoTaskMemFree=lambda _pointer: calls.append("free")
            ),
        )
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(runtime_roots.os, "name", "nt"),
            mock.patch.object(
                runtime_roots.ctypes,
                "windll",
                fake_windll,
                create=True,
            ),
            mock.patch.object(
                runtime_roots.Path,
                "home",
                side_effect=AssertionError("Known Folder failure must not fallback"),
            ),
        ):
            with self.assertRaises(OSError) as caught:
                runtime_roots.resolve_data_root()

        self.assertIn("无法解析当前用户的“文档”目录", str(caught.exception))
        self.assertEqual(["resolve"], calls)

    def test_non_windows_fallback_uses_home_documents(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            with (
                mock.patch.dict(os.environ, {}, clear=True),
                mock.patch.object(runtime_roots.os, "name", "posix"),
                mock.patch.object(runtime_roots.Path, "home", return_value=home),
            ):
                resolved = runtime_roots.resolve_data_root()
            self.assertEqual(
                (home / "Documents" / runtime_roots.DATA_ROOT_NAME).resolve(),
                resolved,
            )

    def test_resolution_is_cached_until_explicit_test_reset(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            first = Path(temporary) / "first"
            second = Path(temporary) / "second"
            with mock.patch.dict(
                os.environ,
                {runtime_roots.DATA_ROOT_ENV: str(first)},
                clear=True,
            ):
                self.assertEqual(first.resolve(), runtime_roots.resolve_data_root())
                os.environ[runtime_roots.DATA_ROOT_ENV] = str(second)
                self.assertEqual(first.resolve(), runtime_roots.resolve_data_root())
                runtime_roots.reset_data_root_cache_for_tests()
                self.assertEqual(second.resolve(), runtime_roots.resolve_data_root())

    def test_ensure_data_layout_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "never-created-before-test"
            with mock.patch.dict(
                os.environ,
                {runtime_roots.DATA_ROOT_ENV: str(data_root)},
                clear=True,
            ):
                runtime_roots.ensure_data_layout()
                runtime_roots.ensure_data_layout()

            self.assertTrue((data_root / "workflow-runtime" / "manifests").is_dir())
            self.assertTrue((data_root / "workflow-runtime" / "reports").is_dir())
            self.assertTrue((data_root / "杯类").is_dir())

    def test_pointer_file_contains_all_resolved_paths_and_uses_injected_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            data_root = base / "data"
            pointer = base / "diagnostics" / "data-root.json"
            with (
                mock.patch.dict(
                    os.environ,
                    {runtime_roots.DATA_ROOT_ENV: str(data_root)},
                    clear=True,
                ),
                mock.patch.object(
                    runtime_roots.Path,
                    "home",
                    side_effect=AssertionError("injected pointer must not use the real home"),
                ),
            ):
                runtime_roots.write_pointer_file(pointer)

            payload = json.loads(pointer.read_text(encoding="utf-8"))
            self.assertEqual(
                {
                    "dataRoot",
                    "repositoryRoot",
                    "workspaceParent",
                    "source",
                    "resolvedAt",
                },
                set(payload),
            )
            self.assertEqual(str(data_root.resolve()), payload["dataRoot"])
            self.assertEqual(
                str(data_root.resolve() / "workflow-runtime"),
                payload["repositoryRoot"],
            )
            self.assertEqual(str(data_root.resolve() / "杯类"), payload["workspaceParent"])
            self.assertEqual("env", payload["source"])
            self.assertIsNotNone(datetime.fromisoformat(payload["resolvedAt"]).tzinfo)
            self.assertEqual([pointer], list((base / "diagnostics").iterdir()))


if __name__ == "__main__":
    unittest.main()
