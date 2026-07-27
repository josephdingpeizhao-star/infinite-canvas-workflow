from __future__ import annotations

import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from launcher.config import LauncherConfigError, load_config
from launcher.logging_utils import configure_launcher_logger
from launcher.orchestrator import (
    StopController,
    build_service_specs,
    configured_log_dir,
    configured_state_path,
)
from launcher.process_control import WindowsProcessManager
from launcher.state_store import StateStore
from launcher.ui import show_message_box


LAUNCHER_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = LAUNCHER_DIR / "launcher_config.json"


def _resolve_pythonw() -> Path:
    current = Path(sys.executable).resolve()
    candidate = current if current.name.casefold() == "pythonw.exe" else current.with_name("pythonw.exe")
    if not candidate.is_file():
        raise RuntimeError("未找到 pythonw.exe，无法安全读取启动配置。")
    return candidate


def main() -> int:
    log_dir = Path.home() / ".infinite-canvas" / "logs"
    logger = configure_launcher_logger(
        log_dir / "launcher.log",
        max_bytes=10 * 1024 * 1024,
        backups=3,
    )
    try:
        config = load_config(DEFAULT_CONFIG_PATH)
        runtime = config["runtime"]
        log_dir = configured_log_dir(config)
        logger = configure_launcher_logger(
            log_dir / "launcher.log",
            max_bytes=int(runtime["log_max_bytes"]),
            backups=int(runtime["log_backups"]),
        )
        logger.info("开始停止无限画布工作台")
        result = StopController(
            specs=build_service_specs(
                config,
                launcher_dir=LAUNCHER_DIR,
                pythonw_path=_resolve_pythonw(),
            ),
            process_manager=WindowsProcessManager(),
            state_store=StateStore(configured_state_path(config)),
            terminate_timeout_seconds=float(runtime["terminate_timeout_seconds"]),
            logger=logger,
        ).stop()
        if result.success:
            message = "画布工作台已停止。" if result.stopped_pids else "画布工作台当前未运行。"
            if result.skipped_pids:
                message += f" 已安全跳过 {len(result.skipped_pids)} 个身份不符的进程。"
            show_message_box("无限画布工作台", message, False)
            logger.info("停止完成 stopped=%s skipped=%s", result.stopped_pids, result.skipped_pids)
            return 0
        show_message_box(
            "无限画布工作台",
            f"部分服务未能安全停止，请查看日志：{log_dir / 'launcher.log'}",
            True,
        )
        logger.error("停止未完成 failures=%s", result.failures)
        return 1
    except (LauncherConfigError, OSError, RuntimeError) as error:
        logger.error("停止入口失败：%s", error)
        show_message_box(
            "无限画布工作台",
            f"{error}\n请查看日志：{log_dir / 'launcher.log'}",
            True,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
