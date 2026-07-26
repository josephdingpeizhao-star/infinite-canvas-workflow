"""Resolve the redirected Windows Desktop through SHGetKnownFolderPath."""

from __future__ import annotations

import ctypes
import os
import uuid
from pathlib import Path

from ctypes import wintypes


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


FOLDERID_DESKTOP = _GUID.from_uuid(
    uuid.UUID("B4BFCC3A-DB2C-424C-B029-7FE99A87C641")
)


def desktop_directory() -> Path:
    if os.name != "nt":
        raise OSError("Windows Known Folder API unavailable")
    pointer = ctypes.c_wchar_p()
    result = ctypes.windll.shell32.SHGetKnownFolderPath(
        ctypes.byref(FOLDERID_DESKTOP),
        0,
        None,
        ctypes.byref(pointer),
    )
    if result != 0 or not pointer.value:
        raise OSError("Windows Desktop Known Folder unavailable")
    try:
        return Path(pointer.value)
    finally:
        ctypes.windll.ole32.CoTaskMemFree(
            ctypes.cast(pointer, ctypes.c_void_p)
        )
