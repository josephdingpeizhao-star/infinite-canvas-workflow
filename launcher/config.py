from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any


class LauncherConfigError(RuntimeError):
    """Configuration that cannot safely start the launcher."""


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise LauncherConfigError(f"{label}不存在：{path}") from None
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise LauncherConfigError(f"{label}不是有效的 JSON：{path}") from None
    if not isinstance(value, dict):
        raise LauncherConfigError(f"{label}必须是一个 JSON 对象：{path}")
    return value


def _required(mapping: dict[str, Any], key: str, location: str, expected_type: type) -> Any:
    if key not in mapping:
        raise LauncherConfigError(f"启动配置缺少 {location}.{key}")
    value = mapping[key]
    if not isinstance(value, expected_type):
        raise LauncherConfigError(f"启动配置 {location}.{key} 的格式不正确")
    return value


def _positive_number(mapping: dict[str, Any], key: str, location: str) -> None:
    value = _required(mapping, key, location, (int, float))
    if isinstance(value, bool) or value <= 0:
        raise LauncherConfigError(f"启动配置 {location}.{key} 必须大于 0")


def _validate_command(value: Any, location: str) -> None:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
        raise LauncherConfigError(f"启动配置 {location} 必须是非空命令数组")


def _validate_service(value: Any, location: str) -> None:
    if not isinstance(value, dict):
        raise LauncherConfigError(f"启动配置 {location} 的格式不正确")
    _required(value, "label", location, str)
    _validate_command(value.get("command"), f"{location}.command")
    _required(value, "cwd", location, str)
    ports = _required(value, "ports", location, list)
    if not ports or any(isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535 for port in ports):
        raise LauncherConfigError(f"启动配置 {location}.ports 必须是有效端口数组")
    health = _required(value, "health", location, dict)
    _required(health, "url", f"{location}.health", str)
    statuses = _required(health, "expected_statuses", f"{location}.health", list)
    if not statuses or any(not isinstance(status, int) for status in statuses):
        raise LauncherConfigError(f"启动配置 {location}.health.expected_statuses 格式不正确")
    critical_workers = health.get("critical_workers", [])
    if not isinstance(critical_workers, list) or not all(
        isinstance(worker, str) and worker for worker in critical_workers
    ):
        raise LauncherConfigError(f"启动配置 {location}.health.critical_workers 格式不正确")
    groups = _required(value, "identity_marker_groups", location, list)
    if not groups or any(
        not isinstance(group, list)
        or not group
        or not all(isinstance(marker, str) and marker for marker in group)
        for group in groups
    ):
        raise LauncherConfigError(f"启动配置 {location}.identity_marker_groups 格式不正确")
    environment = value.get("environment", {})
    if not isinstance(environment, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in environment.items()
    ):
        raise LauncherConfigError(f"启动配置 {location}.environment 格式不正确")


def validate_config(config: dict[str, Any]) -> None:
    paths = _required(config, "paths", "root", dict)
    _required(paths, "state_file", "paths", str)
    _required(paths, "log_dir", "paths", str)

    runtime = _required(config, "runtime", "root", dict)
    for key in (
        "startup_timeout_seconds",
        "connection_ready_timeout_seconds",
        "health_poll_seconds",
        "health_request_timeout_seconds",
        "terminate_timeout_seconds",
        "log_max_bytes",
        "log_backups",
    ):
        _positive_number(runtime, key, "runtime")

    browser = _required(config, "browser", "root", dict)
    _required(browser, "url", "browser", str)
    command = browser.get("command", [])
    if command:
        _validate_command(command, "browser.command")
    elif not isinstance(command, list):
        raise LauncherConfigError("启动配置 browser.command 必须是命令数组")

    services = _required(config, "services", "root", dict)
    for name in ("agent", "workbench"):
        if name not in services:
            raise LauncherConfigError(f"启动配置缺少 services.{name}")
        _validate_service(services[name], f"services.{name}")

    web = _required(config, "web", "root", dict)
    mode = _required(web, "mode", "web", str)
    if mode not in {"dist", "dev"}:
        raise LauncherConfigError("启动配置 web.mode 只能是 dist 或 dev")
    ports = _required(web, "ports", "web", list)
    health = _required(web, "health", "web", dict)
    if not ports or any(not isinstance(port, int) for port in ports):
        raise LauncherConfigError("启动配置 web.ports 格式不正确")
    _required(health, "url", "web.health", str)
    _required(health, "expected_statuses", "web.health", list)
    for web_mode in ("dist", "dev"):
        mode_config = _required(web, web_mode, "web", dict)
        _validate_command(mode_config.get("command"), f"web.{web_mode}.command")
        _required(mode_config, "cwd", f"web.{web_mode}", str)
        groups = _required(mode_config, "identity_marker_groups", f"web.{web_mode}", list)
        if not groups:
            raise LauncherConfigError(f"启动配置 web.{web_mode}.identity_marker_groups 不能为空")
    _required(web["dist"], "root", "web.dist", str)


def load_config(default_path: Path, *, override_path: Path | None = None) -> dict[str, Any]:
    default_path = Path(default_path)
    base = _read_json(default_path, "默认启动配置")
    if override_path is None:
        override_path = Path.home() / ".infinite-canvas" / "launcher" / "config.override.json"
    override_path = Path(override_path)
    if override_path.exists():
        override = _read_json(override_path, "本机覆盖配置")
        config = deep_merge(base, override)
    else:
        config = base
    validate_config(config)
    return config
