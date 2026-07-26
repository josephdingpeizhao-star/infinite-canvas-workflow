from __future__ import annotations

import errno
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

import batch_recycle_lock as locks  # noqa: E402


class BatchRecycleLockTests(unittest.TestCase):
    def test_missing_lock_root_is_created_with_one_byte_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "missing" / "batch-operation-locks"
            with locks.BatchOperationLock("cup", lock_root=root):
                files = list(root.glob("*.lock"))
                self.assertEqual(1, len(files))
                self.assertEqual(1, files[0].stat().st_size)

    def test_same_thread_nested_lock_is_reentrant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "locks"
            with locks.BatchOperationLock("cup", lock_root=root):
                with locks.BatchOperationLock("cup", lock_root=root):
                    self.assertEqual(1, len(list(root.glob("*.lock"))))

    def test_lock_contention_is_reported_as_busy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock = locks.BatchOperationLock("cup", lock_root=Path(tmp))
            lock.path.write_bytes(b"0")
            handle = mock.Mock()
            handle.close.side_effect = OSError(errno.EIO, "close failed")
            failure = OSError(errno.EACCES, "locked")
            with (
                mock.patch.object(Path, "open", return_value=handle),
                mock.patch.object(locks, "_lock_one_byte", side_effect=failure),
            ):
                with self.assertRaises(locks.BatchOperationBusy):
                    lock.__enter__()
            handle.close.assert_called_once_with()
            self.assertIsNone(lock.handle)

    def test_non_contention_lock_error_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock_root = Path(tmp) / "acquire"
            lock_root.mkdir()
            lock = locks.BatchOperationLock("cup", lock_root=lock_root)
            lock.path.write_bytes(b"0")
            handle = mock.Mock()
            handle.close.side_effect = OSError(errno.EIO, "close failed")
            failure = OSError(errno.EIO, "device error")
            with (
                mock.patch.object(Path, "open", return_value=handle),
                mock.patch.object(locks, "_lock_one_byte", side_effect=failure),
            ):
                with self.assertRaises(locks.BatchOperationLockUnavailable):
                    lock.__enter__()
            handle.close.assert_called_once_with()
            self.assertIsNone(lock.handle)

            create_root = Path(tmp) / "create"
            create_lock = locks.BatchOperationLock("cup", lock_root=create_root)
            create_handle = mock.Mock()
            create_handle.close.side_effect = OSError(errno.EIO, "close failed")
            with mock.patch.object(Path, "open", return_value=create_handle):
                with self.assertRaises(locks.BatchOperationLockUnavailable):
                    create_lock.__enter__()
            create_handle.close.assert_called_once_with()
            self.assertIsNone(create_lock.handle)

            access_denied = PermissionError(errno.EACCES, "access denied")
            access_denied.winerror = 5
            with mock.patch.object(
                locks,
                "_lock_one_byte",
                side_effect=access_denied,
            ):
                with self.assertRaises(locks.BatchOperationLockUnavailable):
                    with locks.BatchOperationLock("cup", lock_root=Path(tmp)):
                        pass

    def test_existing_operation_continues_when_lock_is_unavailable(self) -> None:
        with mock.patch.object(
            locks.BatchOperationLock,
            "__enter__",
            side_effect=locks.BatchOperationLockUnavailable("unavailable"),
        ):
            with locks.existing_batch_operation("cup") as acquired:
                self.assertFalse(acquired)
        original_resolve = Path.resolve

        def fail_lock_resolve(path: Path, *, strict: bool = False) -> Path:
            if path.suffix == ".lock":
                raise OSError(errno.EIO, "resolve unavailable")
            return original_resolve(path, strict=strict)

        with mock.patch.object(Path, "resolve", new=fail_lock_resolve):
            with locks.existing_batch_operation("cup") as acquired:
                self.assertFalse(acquired)
        access_denied = PermissionError(errno.EACCES, "access denied")
        access_denied.winerror = 5
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(
                locks,
                "_lock_one_byte",
                side_effect=access_denied,
            ):
                with locks.existing_batch_operation(
                    "cup",
                    lock_root=Path(tmp),
                ) as acquired:
                    self.assertFalse(acquired)

    def test_existing_operation_does_not_swallow_busy_lock(self) -> None:
        with mock.patch.object(
            locks.BatchOperationLock,
            "__enter__",
            side_effect=locks.BatchOperationBusy("busy"),
        ):
            with self.assertRaises(locks.BatchOperationBusy):
                with locks.existing_batch_operation("cup"):
                    pass

    def test_released_lock_can_be_acquired_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "locks"
            lock = locks.BatchOperationLock("cup", lock_root=root)
            lock.__enter__()
            with mock.patch.object(
                Path,
                "resolve",
                side_effect=OSError(errno.EIO, "exit must not resolve"),
            ):
                lock.__exit__(None, None, None)
            self.assertIsNone(lock.handle)
            with locks.BatchOperationLock("cup", lock_root=root):
                self.assertTrue(root.is_dir())

            faulted = locks.BatchOperationLock("cup", lock_root=root)
            faulted.__enter__()
            resolved_path = faulted._resolved_path
            real_handle = faulted.handle
            broken_handle = mock.Mock()
            broken_handle.close.side_effect = OSError(errno.EIO, "close failed")
            faulted.handle = broken_handle
            try:
                with mock.patch.object(
                    locks,
                    "_unlock_one_byte",
                    side_effect=OSError(errno.EIO, "unlock failed"),
                ):
                    faulted.__exit__(None, None, None)
            finally:
                real_handle.close()
            self.assertIsNone(faulted.handle)
            self.assertIsNone(faulted._resolved_path)
            self.assertNotIn(resolved_path, locks._held_paths())
            with locks.BatchOperationLock("cup", lock_root=root):
                self.assertTrue(root.is_dir())

    def test_unsafe_batch_id_never_creates_lock_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "locks"
            with self.assertRaises(locks.BatchOperationLockUnavailable):
                locks.BatchOperationLock("../cup", lock_root=root)
            self.assertFalse(root.exists())


if __name__ == "__main__":
    unittest.main()
