from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


class StateFileError(RuntimeError):
    """Launcher state that cannot be trusted."""


def _validate_state(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("version") != 1:
        raise StateFileError("启动器状态文件已损坏：版本无效")
    services = value.get("services")
    if not isinstance(services, dict):
        raise StateFileError("启动器状态文件已损坏：服务清单无效")
    for name, service in services.items():
        if not isinstance(name, str) or not isinstance(service, dict):
            raise StateFileError("启动器状态文件已损坏：服务记录无效")
        pid = service.get("pid")
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            raise StateFileError("启动器状态文件已损坏：PID 无效")
    return value


class StateStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def read(self) -> dict[str, Any] | None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError):
            raise StateFileError(f"启动器状态文件无法读取：{self.path}") from None
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            raise StateFileError(f"启动器状态文件已损坏：{self.path}") from None
        return _validate_state(value)

    def write(self, state: dict[str, Any]) -> None:
        _validate_state(state)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                json.dump(state, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except OSError:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
            raise StateFileError(f"启动器状态文件无法安全写入：{self.path}") from None

    def delete(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            raise StateFileError(f"启动器状态文件无法删除：{self.path}") from None
