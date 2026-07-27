from __future__ import annotations

import importlib.util
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Mapping, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from launcher.config import load_config
from launcher.logging_utils import configure_launcher_logger
from launcher.orchestrator import (
    HealthProbe,
    ManagedProcessSpec,
    ServiceSpec,
    StopController,
    build_service_specs,
    build_watchdog_spec,
    configured_log_dir,
    configured_state_path,
    make_http_health_checker,
)
from launcher.process_control import ProcessControlError, WindowsProcessManager, matches_identity
from launcher.state_store import StateFileError, StateStore


LAUNCHER_DIR = Path(__file__).resolve().parent
REPO_ROOT = LAUNCHER_DIR.parent
DEFAULT_CONFIG_PATH = LAUNCHER_DIR / "launcher_config.json"
LOCK_MODULE_PATH = REPO_ROOT / "canvas-bridge" / "batch_recycle_lock.py"
DISCONNECTED_STATUSES = frozenset({"waiting_canvas", "not_started"})
NON_FATAL_STATUSES = frozenset({"running"}) | DISCONNECTED_STATUSES


@dataclass(frozen=True)
class LockProbeResult:
    idle: bool
    reason: str
    lock_file: str | None = None
    checked_count: int = 0
    detail: str = ""


def _load_batch_lock_module(path: Path = LOCK_MODULE_PATH) -> ModuleType:
    path = Path(path)
    spec = importlib.util.spec_from_file_location("_canvas_watchdog_batch_recycle_lock", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载批次锁协议。")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in ("_lock_one_byte", "_unlock_one_byte", "_busy_error"):
        if not callable(getattr(module, name, None)):
            raise RuntimeError("批次锁协议缺少看门狗所需入口。")
    return module


class BatchLockProbe:
    def __init__(self, *, lock_module: Any | None = None):
        self.lock_module = lock_module or _load_batch_lock_module()

    def __call__(self, lock_root: Path) -> LockProbeResult:
        root = Path(lock_root)
        try:
            if not root.exists() or not root.is_dir() or root.is_symlink():
                return LockProbeResult(False, "unavailable", detail="锁根不存在或不是普通目录")
            candidates = sorted(
                (path for path in root.iterdir() if path.name.casefold().endswith(".lock")),
                key=lambda path: path.name.casefold(),
            )
        except (OSError, RuntimeError):
            return LockProbeResult(False, "unavailable", detail="锁根无法枚举")

        checked = 0
        for path in candidates:
            checked += 1
            try:
                stem = path.name[:-5]
                if len(stem) != 64 or any(char not in "0123456789abcdef" for char in stem):
                    return LockProbeResult(
                        False,
                        "unavailable",
                        lock_file=path.name,
                        checked_count=checked,
                        detail="锁文件名不符合 SHA-256 载体规则",
                    )
                if path.is_symlink() or not path.is_file() or path.stat().st_size != 1:
                    return LockProbeResult(
                        False,
                        "unavailable",
                        lock_file=path.name,
                        checked_count=checked,
                        detail="锁载体不是普通的一字节文件",
                    )
                with path.open("r+b") as handle:
                    try:
                        self.lock_module._lock_one_byte(handle)
                    except OSError as error:
                        reason = "busy" if self.lock_module._busy_error(error) else "unavailable"
                        detail = "锁正在使用" if reason == "busy" else "锁无法取得"
                        return LockProbeResult(
                            False,
                            reason,
                            lock_file=path.name,
                            checked_count=checked,
                            detail=detail,
                        )
                    except Exception:
                        return LockProbeResult(
                            False,
                            "unavailable",
                            lock_file=path.name,
                            checked_count=checked,
                            detail="锁协议调用异常",
                        )
                    try:
                        self.lock_module._unlock_one_byte(handle)
                    except Exception:
                        return LockProbeResult(
                            False,
                            "unavailable",
                            lock_file=path.name,
                            checked_count=checked,
                            detail="锁无法安全释放",
                        )
            except (OSError, RuntimeError):
                return LockProbeResult(
                    False,
                    "unavailable",
                    lock_file=path.name,
                    checked_count=checked,
                    detail="锁文件无法读取",
                )
        return LockProbeResult(True, "idle", checked_count=checked)


def _probe_state(
    spec: ServiceSpec,
    probe: HealthProbe,
) -> tuple[str, Mapping[str, str]]:
    if probe.responded and probe.status == 200:
        return "ready", {}
    if not probe.responded or probe.status != 503 or not isinstance(probe.body, Mapping):
        return "uncertain", {}
    raw_workers = probe.body.get("workers")
    if not isinstance(raw_workers, Mapping):
        return "uncertain", {}
    statuses: dict[str, str] = {}
    for name in spec.critical_workers:
        worker = raw_workers.get(name)
        status = worker.get("status") if isinstance(worker, Mapping) else None
        if not isinstance(status, str):
            return "uncertain", statuses
        statuses[name] = status
    if any(status == "stopped" for status in statuses.values()):
        return "stopped", statuses
    if any(status not in NON_FATAL_STATUSES for status in statuses.values()):
        return "uncertain", statuses
    if any(status in DISCONNECTED_STATUSES for status in statuses.values()):
        return "disconnected", statuses
    return "uncertain", statuses


class RuntimeStateInspector:
    def __init__(
        self,
        *,
        specs: Sequence[ServiceSpec],
        watchdog_spec: ManagedProcessSpec,
        state_store: StateStore,
        process_manager: Any,
        health_checker: Callable[[ServiceSpec], HealthProbe],
        logger: logging.Logger,
    ):
        self.specs = tuple(specs)
        self.watchdog_spec = watchdog_spec
        self.state_store = state_store
        self.process_manager = process_manager
        self.health_checker = health_checker
        self.logger = logger

    def _read_state(self) -> dict[str, Any] | None:
        try:
            return self.state_store.read()
        except StateFileError as error:
            self.logger.warning("无法确认运行状态，本轮保留服务：%s", error)
            return {}

    def services_are_gone(self) -> bool:
        state = self._read_state()
        if state is None:
            return True
        state_services = state.get("services") if isinstance(state, dict) else {}
        if not isinstance(state_services, Mapping):
            return False
        try:
            for spec in self.specs:
                entry = state_services.get(spec.name)
                pid = entry.get("pid") if isinstance(entry, Mapping) else None
                if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
                    continue
                record = self.process_manager.get_process(pid)
                if record is not None and matches_identity(record, spec.identity_marker_groups):
                    return False
        except ProcessControlError as error:
            self.logger.warning("无法确认服务进程，本轮保留服务：%s", error)
            return False

        for spec in self.specs:
            try:
                probe = self.health_checker(spec)
            except Exception:
                return False
            if probe.responded:
                return False
        return True

    def is_superseded(self, own_pid: int) -> bool:
        state = self._read_state()
        if not isinstance(state, Mapping):
            return False
        entry = state.get("watchdog")
        pid = entry.get("pid") if isinstance(entry, Mapping) else None
        if (
            not isinstance(pid, int)
            or isinstance(pid, bool)
            or pid <= 0
            or pid == own_pid
        ):
            return False
        try:
            record = self.process_manager.get_process(pid)
        except ProcessControlError:
            return False
        return record is not None and matches_identity(
            record,
            self.watchdog_spec.identity_marker_groups,
        )


class WatchdogController:
    def __init__(
        self,
        *,
        workbench_spec: ServiceSpec,
        health_checker: Callable[[ServiceSpec], HealthProbe],
        lock_probe: Callable[[Path], LockProbeResult],
        lock_root: Path,
        stop_services: Callable[[], Any],
        services_are_gone: Callable[[], bool],
        is_superseded: Callable[[], bool],
        disconnect_grace_seconds: float,
        poll_seconds: float,
        suspend_gap_threshold_seconds: float,
        logger: logging.Logger,
        sleep=time.sleep,
        monotonic=time.monotonic,
    ):
        self.workbench_spec = workbench_spec
        self.health_checker = health_checker
        self.lock_probe = lock_probe
        self.lock_root = Path(lock_root)
        self.stop_services = stop_services
        self.services_are_gone = services_are_gone
        self.is_superseded = is_superseded
        self.disconnect_grace_seconds = float(disconnect_grace_seconds)
        self.poll_seconds = float(poll_seconds)
        self.suspend_gap_threshold_seconds = float(suspend_gap_threshold_seconds)
        self.logger = logger
        self.sleep = sleep
        self.monotonic = monotonic

    def run(self, *, max_cycles: int | None = None) -> str:
        armed = False
        disconnected_at: float | None = None
        last_poll_at: float | None = None
        cycles = 0
        while max_cycles is None or cycles < max_cycles:
            cycles += 1
            now = self.monotonic()
            if (
                last_poll_at is not None
                and now - last_poll_at > self.suspend_gap_threshold_seconds
            ):
                disconnected_at = None
                self.logger.info(
                    "检测到休眠或挂起，断连计时已重置 gap=%.3fs threshold=%.3fs",
                    now - last_poll_at,
                    self.suspend_gap_threshold_seconds,
                )
            last_poll_at = now

            if self.services_are_gone():
                self.logger.info("三个业务服务已不在，看门狗自行退出")
                return "services_gone"
            if self.is_superseded():
                self.logger.info("已有另一个身份核验通过的看门狗，本实例退出")
                return "superseded"

            try:
                probe = self.health_checker(self.workbench_spec)
            except Exception:
                probe = HealthProbe(False)
            state, statuses = _probe_state(self.workbench_spec, probe)
            if state == "ready":
                if not armed:
                    armed = True
                    self.logger.info("看门狗已武装：首次观察到工作台全链 200")
                elif disconnected_at is not None:
                    self.logger.info("画布恢复连接，断连计时已清零")
                disconnected_at = None
            elif state == "disconnected":
                if not armed:
                    self.logger.info("尚未武装，仅观察画布断连 statuses=%s", dict(statuses))
                else:
                    if disconnected_at is None:
                        disconnected_at = now
                    elapsed = max(0.0, now - disconnected_at)
                    self.logger.info(
                        "画布断连计时 elapsed=%.3fs grace=%.3fs statuses=%s",
                        elapsed,
                        self.disconnect_grace_seconds,
                        dict(statuses),
                    )
                    if elapsed >= self.disconnect_grace_seconds:
                        try:
                            lock_result = self.lock_probe(self.lock_root)
                        except Exception:
                            lock_result = LockProbeResult(
                                False,
                                "unavailable",
                                detail="锁探测异常",
                            )
                        if not lock_result.idle:
                            self.logger.warning(
                                "锁探测否决停机 reason=%s lock_file=%s checked=%s detail=%s",
                                lock_result.reason,
                                lock_result.lock_file or "<锁根>",
                                lock_result.checked_count,
                                lock_result.detail,
                            )
                        else:
                            self.logger.info(
                                "锁探测通过，准备停止三个业务服务 checked=%s",
                                lock_result.checked_count,
                            )
                            try:
                                stop_result = self.stop_services()
                            except Exception as error:
                                self.logger.error(
                                    "自动停止失败且不再重试 error_type=%s",
                                    type(error).__name__,
                                )
                                return "stop_failed"
                            if stop_result.success:
                                self.logger.info("三个业务服务已自动停止，状态文件已清理")
                                return "stopped"
                            self.logger.error(
                                "自动停止失败且不再重试 failures=%s",
                                tuple(getattr(stop_result, "failures", ())),
                            )
                            return "stop_failed"
            else:
                if disconnected_at is not None:
                    self.logger.info(
                        "断连证据中断，计时已清零 state=%s status=%s",
                        state,
                        probe.status,
                    )
                disconnected_at = None
                if state == "stopped":
                    self.logger.warning("关键工人已停止，看门狗不据此停机 statuses=%s", dict(statuses))
                else:
                    self.logger.warning(
                        "健康信号不确定，本轮保留服务 responded=%s status=%s",
                        probe.responded,
                        probe.status,
                    )
            self.sleep(self.poll_seconds)
        return "test_limit"


def main() -> int:
    default_log_dir = Path.home() / ".infinite-canvas" / "logs"
    logger = configure_launcher_logger(
        default_log_dir / "watchdog.log",
        max_bytes=10 * 1024 * 1024,
        backups=3,
    )
    try:
        config = load_config(DEFAULT_CONFIG_PATH)
        watchdog_config = config["watchdog"]
        runtime = config["runtime"]
        log_dir = configured_log_dir(config)
        logger = configure_launcher_logger(
            log_dir / "watchdog.log",
            max_bytes=int(runtime["log_max_bytes"]),
            backups=int(runtime["log_backups"]),
        )
        if not watchdog_config["enabled"]:
            logger.info("看门狗已在本机配置中关闭")
            return 0

        process_manager = WindowsProcessManager()
        state_store = StateStore(configured_state_path(config))
        pythonw_path = Path(sys.executable).resolve()
        specs = build_service_specs(
            config,
            launcher_dir=LAUNCHER_DIR,
            pythonw_path=pythonw_path,
        )
        watchdog = build_watchdog_spec(
            launcher_dir=LAUNCHER_DIR,
            pythonw_path=pythonw_path,
        )
        health_checker = make_http_health_checker(
            float(runtime["health_request_timeout_seconds"])
        )
        inspector = RuntimeStateInspector(
            specs=specs,
            watchdog_spec=watchdog,
            state_store=state_store,
            process_manager=process_manager,
            health_checker=health_checker,
            logger=logger,
        )
        stopper = StopController(
            specs=specs,
            watchdog_spec=watchdog,
            exclude_pids=(os.getpid(),),
            process_manager=process_manager,
            state_store=state_store,
            terminate_timeout_seconds=float(runtime["terminate_timeout_seconds"]),
            logger=logger,
        )
        workbench = next(spec for spec in specs if spec.name == "workbench")
        logger.info("看门狗开始观察")
        WatchdogController(
            workbench_spec=workbench,
            health_checker=health_checker,
            lock_probe=BatchLockProbe(),
            lock_root=Path(watchdog_config["batch_lock_root"]).expanduser(),
            stop_services=stopper.stop,
            services_are_gone=inspector.services_are_gone,
            is_superseded=lambda: inspector.is_superseded(os.getpid()),
            disconnect_grace_seconds=float(
                watchdog_config["disconnect_grace_seconds"]
            ),
            poll_seconds=float(watchdog_config["poll_seconds"]),
            suspend_gap_threshold_seconds=float(
                watchdog_config["suspend_gap_threshold_seconds"]
            ),
            logger=logger,
        ).run()
        return 0
    except Exception as error:
        logger.error(
            "看门狗异常退出 error_type=%s detail=%s",
            type(error).__name__,
            error,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
