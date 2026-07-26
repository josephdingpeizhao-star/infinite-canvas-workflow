"""Small injectable Windows Recycle Bin adapter using only the stdlib."""

from __future__ import annotations

import ctypes
import os
from pathlib import Path
from typing import Any, Callable

from ctypes import wintypes


FO_DELETE = 0x0003
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040
FOF_NOERRORUI = 0x0400


class RecycleBinError(RuntimeError):
    """One exact target could not be handed to the Windows Recycle Bin."""


class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", wintypes.WORD),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", wintypes.LPVOID),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


def double_null_path(path: Path) -> str:
    text = str(path)
    if not path.is_absolute() or "\0" in text:
        raise RecycleBinError("删除目标不是安全的本机绝对路径。")
    return text + "\0\0"


def _system_shell_operation(operation: SHFILEOPSTRUCTW, _source_list: str) -> int:
    if os.name != "nt":
        raise RecycleBinError("当前系统不支持 Windows 回收站。")
    return int(ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation)))


class WindowsRecycleBinExecutor:
    """Send one already-validated file or directory to the Recycle Bin."""

    def __init__(
        self,
        *,
        shell_operation: Callable[[SHFILEOPSTRUCTW, str], int] | None = None,
    ) -> None:
        self.shell_operation = shell_operation or _system_shell_operation

    def __call__(self, path: Path) -> None:
        source_list = double_null_path(Path(path))
        operation = SHFILEOPSTRUCTW()
        operation.wFunc = FO_DELETE
        operation.pFrom = source_list
        operation.pTo = None
        operation.fFlags = (
            FOF_ALLOWUNDO
            | FOF_NOCONFIRMATION
            | FOF_SILENT
            | FOF_NOERRORUI
        )
        operation.fAnyOperationsAborted = False
        result = self.shell_operation(operation, source_list)
        if result != 0 or bool(operation.fAnyOperationsAborted):
            raise RecycleBinError(
                "文件未能进入 Windows 回收站，本次删除已停止。"
            )
