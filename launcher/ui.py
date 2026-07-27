from __future__ import annotations

import ctypes
import os
import subprocess
from collections.abc import Sequence

from launcher.process_control import CREATE_NO_WINDOW


def show_message_box(title: str, message: str, error: bool = False) -> None:
    flags = 0x00000010 if error else 0x00000040
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(None, message, title, flags)


def make_browser_opener(command: Sequence[str]):
    configured = tuple(command)

    def open_browser(url: str) -> None:
        if configured:
            expanded = [part.replace("{url}", url) for part in configured]
            if not any("{url}" in part for part in configured):
                expanded.append(url)
            subprocess.Popen(
                expanded,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                creationflags=CREATE_NO_WINDOW,
            )
            return
        if os.name != "nt":
            raise RuntimeError("系统默认浏览器入口仅支持 Windows")
        os.startfile(url)

    return open_browser
