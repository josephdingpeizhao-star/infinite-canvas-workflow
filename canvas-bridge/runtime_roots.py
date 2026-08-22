"""Resolve the immutable program root and the per-user workflow data root."""

from __future__ import annotations

import ctypes
import json
import os
import tempfile
import uuid
from ctypes import wintypes
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path


PROGRAM_ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT_ENV = "INFINITE_CANVAS_DATA_ROOT"
DATA_ROOT_NAME = "无限画布工作流"


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def from_uuid(cls, value: uuid.UUID) -> "_GUID":
        raw = value.bytes_le
        return cls(
            int.from_bytes(raw[0:4], "little"),
            int.from_bytes(raw[4:6], "little"),
            int.from_bytes(raw[6:8], "little"),
            (ctypes.c_ubyte * 8)(*raw[8:16]),
        )


FOLDERID_DOCUMENTS = _GUID.from_uuid(
    uuid.UUID("FDD39AD0-238F-46AF-ADB4-6C85480369C7")
)


def _windows_documents_directory() -> Path:
    pointer = ctypes.c_wchar_p()
    try:
        try:
            result = ctypes.windll.shell32.SHGetKnownFolderPath(
                ctypes.byref(FOLDERID_DOCUMENTS),
                0,
                None,
                ctypes.byref(pointer),
            )
        except (AttributeError, OSError) as exc:
            raise OSError("Windows 无法调用“文档”目录解析接口") from exc
        if result != 0 or not pointer.value:
            raise OSError("Windows 无法解析当前用户的“文档”目录")
        return Path(pointer.value)
    finally:
        pointer_address = ctypes.cast(pointer, ctypes.c_void_p).value
        if pointer_address:
            ctypes.windll.ole32.CoTaskMemFree(ctypes.c_void_p(pointer_address))


@lru_cache(maxsize=1)
def _resolved_data_root() -> tuple[Path, str]:
    if DATA_ROOT_ENV in os.environ:
        raw = os.environ[DATA_ROOT_ENV]
        if not raw.strip():
            raise ValueError(
                f"环境变量 {DATA_ROOT_ENV} 已设置但为空；请填写绝对路径后重试"
            )
        configured = Path(raw)
        if not configured.is_absolute():
            raise ValueError(
                f"环境变量 {DATA_ROOT_ENV} 必须是绝对路径，当前值不会被使用：{raw!r}"
            )
        return configured.resolve(strict=False), "env"

    if os.name == "nt":
        try:
            documents = _windows_documents_directory()
        except OSError as exc:
            raise OSError(
                "无法解析当前用户的“文档”目录；为避免把数据写入错误位置，启动已停止"
            ) from exc
        return (documents / DATA_ROOT_NAME).resolve(strict=False), "documents"

    return (
        Path.home() / "Documents" / DATA_ROOT_NAME
    ).resolve(strict=False), "fallback"


def resolve_data_root() -> Path:
    """Return the cached per-user data root without creating it."""

    return _resolved_data_root()[0]


def repository_root() -> Path:
    """Return the data-side repository root used by ledgers and reports."""

    return resolve_data_root() / "workflow-runtime"


def ensure_data_layout() -> None:
    """Create the stable data directories. Safe to call on every startup."""

    data_root = resolve_data_root()
    repo_root = data_root / "workflow-runtime"
    for directory in (
        repo_root / "manifests",
        repo_root / "reports",
        data_root / "杯类",
    ):
        directory.mkdir(parents=True, exist_ok=True)


def write_pointer_file(path: Path | None = None) -> None:
    """Write the fixed diagnostic pointer for locating this process's data root."""

    data_root, source = _resolved_data_root()
    repo_root = data_root / "workflow-runtime"
    target = path if path is not None else Path.home() / ".infinite-canvas" / "data-root.json"
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "dataRoot": str(data_root),
        "repositoryRoot": str(repo_root),
        "workspaceParent": str(data_root / "杯类"),
        "source": source,
        "resolvedAt": datetime.now(timezone.utc).isoformat(),
    }
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            json.dump(payload, temporary_file, ensure_ascii=False, indent=2)
            temporary_file.write("\n")
        os.replace(temporary_path, target)
    except Exception:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def reset_data_root_cache_for_tests() -> None:
    """Clear the process-wide resolution cache for isolated tests only."""

    _resolved_data_root.cache_clear()
