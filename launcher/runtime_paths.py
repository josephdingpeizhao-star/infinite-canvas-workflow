from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Mapping


def expand_template(value: str, context: Mapping[str, str]) -> str:
    try:
        return value.format_map(context)
    except KeyError as error:
        raise RuntimeError(f"启动配置包含未知占位符：{error.args[0]}") from None


def repo_root(launcher_dir: Path) -> Path:
    return Path(launcher_dir).parent


def project_root(launcher_dir: Path) -> Path:
    return repo_root(launcher_dir).parent


def find_bun() -> str:
    executable = shutil.which("bun")
    return str(Path(executable).resolve()) if executable else "bun"


def base_context(launcher_dir: Path) -> dict[str, str]:
    return {
        "repo_root": str(repo_root(launcher_dir)),
        "project_root": str(project_root(launcher_dir)),
        "bun": find_bun(),
    }


def resolve_dist_root(config: Mapping[str, Any], launcher_dir: Path) -> Path:
    raw_root = str(config["web"]["dist"]["root"])
    return Path(expand_template(raw_root, base_context(launcher_dir))).expanduser()
