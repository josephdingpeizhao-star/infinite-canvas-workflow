from __future__ import annotations

import tempfile
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

from windows_recycle_bin import (  # noqa: E402
    FO_DELETE,
    FOF_ALLOWUNDO,
    FOF_NOCONFIRMATION,
    FOF_NOERRORUI,
    FOF_SILENT,
    RecycleBinError,
    WindowsRecycleBinExecutor,
    double_null_path,
)


class WindowsRecycleBinTests(unittest.TestCase):
    def test_builds_exact_double_null_delete_operation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "目标"
            path.mkdir()
            captured: dict[str, object] = {}

            def fake_call(operation, source_list: str) -> int:
                captured["operation"] = operation
                captured["source_list"] = source_list
                return 0

            WindowsRecycleBinExecutor(shell_operation=fake_call)(path)

        operation = captured["operation"]
        self.assertEqual(FO_DELETE, operation.wFunc)
        self.assertEqual(
            FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI,
            operation.fFlags,
        )
        self.assertEqual(str(path) + "\0\0", captured["source_list"])
        self.assertEqual(str(path) + "\0\0", double_null_path(path))

    def test_nonzero_or_aborted_shell_result_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "target"
            path.mkdir()
            with self.assertRaises(RecycleBinError):
                WindowsRecycleBinExecutor(
                    shell_operation=lambda _operation, _source: 5
                )(path)

            def aborted(operation, _source: str) -> int:
                operation.fAnyOperationsAborted = 1
                return 0

            with self.assertRaises(RecycleBinError):
                WindowsRecycleBinExecutor(shell_operation=aborted)(path)

    def test_relative_and_nul_paths_are_rejected_before_shell_call(self) -> None:
        called = False

        def fake(_operation, _source: str) -> int:
            nonlocal called
            called = True
            return 0

        executor = WindowsRecycleBinExecutor(shell_operation=fake)
        with self.assertRaises(RecycleBinError):
            executor(Path("relative"))
        with self.assertRaises(RecycleBinError):
            double_null_path(Path("C:/bad\0name"))
        self.assertFalse(called)


if __name__ == "__main__":
    unittest.main()
