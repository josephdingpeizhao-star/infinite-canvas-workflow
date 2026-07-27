from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from launcher.logging_utils import open_child_log


CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class ProcessControlError(RuntimeError):
    """A Windows process operation that could not be completed safely."""


@dataclass(frozen=True)
class ProcessRecord:
    pid: int
    executable_path: str
    command_line: str


def _normalized(value: str) -> str:
    return " ".join(value.replace("\\", "/").casefold().split())


def matches_identity(record: ProcessRecord, marker_groups: Sequence[Sequence[str]]) -> bool:
    haystack = _normalized(f"{record.executable_path} {record.command_line}")
    return any(all(_normalized(marker) in haystack for marker in group) for group in marker_groups)


class WindowsProcessManager:
    def __init__(self, *, sleep=time.sleep, monotonic=time.monotonic):
        self.sleep = sleep
        self.monotonic = monotonic

    @staticmethod
    def _run_hidden(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(command),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
            check=False,
        )

    def spawn(self, spec: Any, log_path: Path, *, max_bytes: int, backups: int) -> int:
        environment = os.environ.copy()
        environment.update(spec.environment)
        handle = open_child_log(log_path, max_bytes=max_bytes, backups=backups)
        try:
            process = subprocess.Popen(
                list(spec.command),
                cwd=str(spec.cwd),
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=subprocess.STDOUT,
                close_fds=True,
                creationflags=CREATE_NO_WINDOW,
            )
        except (OSError, ValueError) as error:
            raise ProcessControlError(f"{spec.label}无法启动：{error}") from None
        finally:
            handle.close()
        return process.pid

    def get_process(self, pid: int) -> ProcessRecord | None:
        if not isinstance(pid, int) or pid <= 0:
            return None
        script = (
            f"$p=Get-CimInstance Win32_Process -Filter \"ProcessId = {pid}\";"
            "if($null -eq $p){exit 3};"
            "[pscustomobject]@{pid=[int]$p.ProcessId;executable=[string]$p.ExecutablePath;"
            "commandLine=[string]$p.CommandLine}|ConvertTo-Json -Compress"
        )
        result = self._run_hidden(
            ("powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", script)
        )
        if result.returncode == 3:
            return None
        if result.returncode != 0:
            raise ProcessControlError(f"无法核对 PID {pid} 的进程身份")
        try:
            value = json.loads(result.stdout)
            return ProcessRecord(
                pid=int(value["pid"]),
                executable_path=str(value.get("executable") or ""),
                command_line=str(value.get("commandLine") or ""),
            )
        except (TypeError, ValueError, KeyError, json.JSONDecodeError):
            raise ProcessControlError(f"PID {pid} 的进程身份信息无法识别") from None

    def listener_pids(self, port: int) -> list[int]:
        script = (
            f"$ids=@(Get-NetTCPConnection -State Listen -LocalPort {int(port)} "
            "-ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique);"
            "if($ids.Count -eq 0){'[]'}else{$ids|ConvertTo-Json -Compress}"
        )
        result = self._run_hidden(
            ("powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", script)
        )
        if result.returncode != 0:
            raise ProcessControlError(f"无法检查端口 {port}")
        try:
            value = json.loads(result.stdout.strip() or "[]")
        except json.JSONDecodeError:
            raise ProcessControlError(f"端口 {port} 的占用信息无法识别") from None
        values = value if isinstance(value, list) else [value]
        return sorted({int(pid) for pid in values if isinstance(pid, int) or str(pid).isdigit()})

    def terminate_tree(self, pid: int, *, force: bool) -> bool:
        command = ["taskkill.exe", "/PID", str(pid), "/T"]
        if force:
            command.append("/F")
        result = self._run_hidden(command)
        if result.returncode == 0:
            return True
        return self.get_process(pid) is None

    def wait_for_exit(self, pid: int, timeout: float) -> bool:
        deadline = self.monotonic() + max(0.0, timeout)
        while self.monotonic() < deadline:
            if self.get_process(pid) is None:
                return True
            self.sleep(0.1)
        return self.get_process(pid) is None
