"""One-byte, non-blocking OS locks shared by every batch side-effect entry."""

from __future__ import annotations

import errno
import hashlib
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


DEFAULT_BATCH_LOCK_ROOT = (
    Path.home() / ".infinite-canvas" / "batch-operation-locks"
)


class BatchOperationLockError(RuntimeError):
    pass


class BatchOperationBusy(BatchOperationLockError):
    pass


class BatchOperationLockUnavailable(BatchOperationLockError):
    pass


_THREAD_STATE = threading.local()


def _held_paths() -> set[Path]:
    value = getattr(_THREAD_STATE, "held_paths", None)
    if value is None:
        value = set()
        _THREAD_STATE.held_paths = value
    return value


def _safe_lock_name(batch_id: str) -> str:
    if (
        not isinstance(batch_id, str)
        or not batch_id
        or Path(batch_id).name != batch_id
        or any(char in batch_id for char in ("/", "\\", "\0", "\r", "\n"))
    ):
        raise BatchOperationLockUnavailable("批次号无法用于独占保护。")
    digest = hashlib.sha256(batch_id.encode("utf-8")).hexdigest()
    return f"{digest}.lock"


def _lock_one_byte(handle) -> None:
    handle.seek(0)
    try:
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except ImportError:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_one_byte(handle) -> None:
    handle.seek(0)
    try:
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    except ImportError:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _quiet_close(handle) -> None:
    try:
        handle.close()
    except OSError:
        pass


def _busy_error(exc: OSError) -> bool:
    winerror = getattr(exc, "winerror", None)
    if winerror is not None:
        return winerror in {32, 33, 36}
    return exc.errno in {errno.EACCES, errno.EAGAIN}


class BatchOperationLock:
    """A held lock is released by the OS even if the process exits."""

    def __init__(self, batch_id: str, *, lock_root: Path | None = None) -> None:
        self.batch_id = batch_id
        self.lock_root = (
            lock_root if lock_root is not None else DEFAULT_BATCH_LOCK_ROOT
        )
        self.path = self.lock_root / _safe_lock_name(batch_id)
        self.handle = None
        self._reentrant = False
        self._resolved_path: Path | None = None

    def __enter__(self) -> "BatchOperationLock":
        try:
            self._resolved_path = self.path.resolve(strict=False)
        except (OSError, RuntimeError):
            raise BatchOperationLockUnavailable(
                "批次独占保护文件无法定位。"
            ) from None
        resolved_path = self._resolved_path
        if resolved_path in _held_paths():
            self._reentrant = True
            return self
        try:
            self.lock_root.mkdir(parents=True, exist_ok=True)
            self.handle = self.path.open("a+b")
            if self.path.stat().st_size == 0:
                self.handle.write(b"0")
                self.handle.flush()
            self.handle.seek(0)
        except OSError:
            if self.handle is not None:
                _quiet_close(self.handle)
                self.handle = None
            raise BatchOperationLockUnavailable(
                "批次独占保护文件无法创建或打开。"
            ) from None
        try:
            _lock_one_byte(self.handle)
        except OSError as exc:
            _quiet_close(self.handle)
            self.handle = None
            if _busy_error(exc):
                raise BatchOperationBusy("批次有任务正在运行。") from None
            raise BatchOperationLockUnavailable(
                "批次独占保护无法取得。"
            ) from None
        _held_paths().add(resolved_path)
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        if self._reentrant:
            return
        if self.handle is None:
            return
        resolved_path = self._resolved_path
        try:
            try:
                _unlock_one_byte(self.handle)
            except OSError:
                pass
            finally:
                _quiet_close(self.handle)
        finally:
            self.handle = None
            self._resolved_path = None
            if resolved_path is not None:
                _held_paths().discard(resolved_path)


@contextmanager
def existing_batch_operation(
    batch_id: str,
    *,
    lock_root: Path | None = None,
) -> Iterator[bool]:
    """Hold the shared lock, but preserve legacy work if lock storage is broken.

    ``False`` means lock infrastructure was unavailable. Existing production
    may continue because recycle/restore fail closed under the same condition.
    A genuine busy lock is never swallowed.
    """

    lock = BatchOperationLock(batch_id, lock_root=lock_root)
    try:
        lock.__enter__()
    except BatchOperationLockUnavailable:
        yield False
        return
    try:
        yield True
    finally:
        lock.__exit__(None, None, None)
