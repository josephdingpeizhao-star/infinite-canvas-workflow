from __future__ import annotations

import sys
from logging import Logger
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from launcher.config import LauncherConfigError, load_config
from launcher.logging_utils import configure_launcher_logger
from launcher.orchestrator import (
    LauncherController,
    build_service_specs,
    build_watchdog_spec,
    configured_log_dir,
    configured_state_path,
    make_http_health_checker,
)
from launcher.process_control import WindowsProcessManager
from launcher.render_credentials import (
    DEFAULT_RENDER_CREDENTIALS_PATH,
    RenderCredentials,
    RenderCredentialsError,
    load_render_credentials,
)
from launcher.state_store import StateStore
from launcher.static_server import StaticServerError, validate_dist_root
from launcher.ui import make_browser_opener, show_message_box


LAUNCHER_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = LAUNCHER_DIR / "launcher_config.json"


def _resolve_pythonw() -> Path:
    current = Path(sys.executable).resolve()
    candidate = current if current.name.casefold() == "pythonw.exe" else current.with_name("pythonw.exe")
    if not candidate.is_file():
        raise RuntimeError("未找到 pythonw.exe，无法保证启动过程无黑窗。")
    return candidate


def _configured_render_credentials_path(config: dict[str, object]) -> Path:
    paths = config.get("paths")
    raw_path = paths.get("render_credentials_file") if isinstance(paths, dict) else None
    if raw_path is None:
        return DEFAULT_RENDER_CREDENTIALS_PATH.expanduser()
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise RenderCredentialsError("渲染凭据文件路径配置无效")
    return Path(raw_path).expanduser()


def _load_render_credentials_for_launcher(
    config: dict[str, object],
    logger: Logger,
) -> RenderCredentials | None:
    try:
        credentials = load_render_credentials(_configured_render_credentials_path(config))
    except RenderCredentialsError as error:
        logger.warning("渲染凭据文件无效：%s", error)
        return None
    if credentials is None:
        logger.info("未找到渲染凭据文件（本次启动不含出图能力）")
        return None
    logger.info("已加载渲染凭据（出图已启用）")
    return credentials


def main() -> int:
    log_dir = Path.home() / ".infinite-canvas" / "logs"
    logger = configure_launcher_logger(
        log_dir / "launcher.log",
        max_bytes=10 * 1024 * 1024,
        backups=3,
    )
    try:
        config = load_config(DEFAULT_CONFIG_PATH)
        log_dir = configured_log_dir(config)
        runtime = config["runtime"]
        logger = configure_launcher_logger(
            log_dir / "launcher.log",
            max_bytes=int(runtime["log_max_bytes"]),
            backups=int(runtime["log_backups"]),
        )
        pythonw_path = _resolve_pythonw()
        if config["web"]["mode"] == "dist":
            validate_dist_root(Path(config["web"]["dist"]["root"]).expanduser())
        render_credentials = _load_render_credentials_for_launcher(config, logger)
        specs = build_service_specs(
            config,
            launcher_dir=LAUNCHER_DIR,
            pythonw_path=pythonw_path,
            render_credentials=render_credentials,
        )
        controller = LauncherController(
            specs=specs,
            watchdog_spec=(
                build_watchdog_spec(
                    launcher_dir=LAUNCHER_DIR,
                    pythonw_path=pythonw_path,
                )
                if config["watchdog"]["enabled"]
                else None
            ),
            process_manager=WindowsProcessManager(),
            state_store=StateStore(configured_state_path(config)),
            health_checker=make_http_health_checker(float(runtime["health_request_timeout_seconds"])),
            browser_opener=make_browser_opener(config["browser"].get("command", [])),
            message_box=show_message_box,
            browser_url=str(config["browser"]["url"]),
            log_dir=log_dir,
            startup_timeout_seconds=float(runtime["startup_timeout_seconds"]),
            connection_ready_timeout_seconds=float(runtime["connection_ready_timeout_seconds"]),
            health_poll_seconds=float(runtime["health_poll_seconds"]),
            terminate_timeout_seconds=float(runtime["terminate_timeout_seconds"]),
            log_max_bytes=int(runtime["log_max_bytes"]),
            log_backups=int(runtime["log_backups"]),
            logger=logger,
        )
        return 0 if controller.run().success else 1
    except (LauncherConfigError, StaticServerError, OSError, RuntimeError) as error:
        logger.error("启动入口失败：%s", error)
        show_message_box(
            "无限画布工作台",
            f"{error}\n请查看日志：{log_dir / 'launcher.log'}",
            True,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
