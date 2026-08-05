from __future__ import annotations

import hashlib
import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from launcher.process_control import ProcessControlError, matches_identity
from launcher.render_credentials import RenderCredentials
from launcher.runtime_paths import base_context, expand_template, resolve_dist_root
from launcher.state_store import StateFileError, StateStore


@dataclass(frozen=True)
class ServiceSpec:
    name: str
    label: str
    command: tuple[str, ...]
    cwd: Path
    ports: tuple[int, ...]
    health_url: str
    expected_statuses: tuple[int, ...]
    identity_marker_groups: tuple[tuple[str, ...], ...]
    environment: Mapping[str, str]
    critical_workers: tuple[str, ...] = ()


@dataclass(frozen=True)
class ManagedProcessSpec:
    name: str
    label: str
    command: tuple[str, ...]
    cwd: Path
    identity_marker_groups: tuple[tuple[str, ...], ...]
    environment: Mapping[str, str]


@dataclass(frozen=True)
class HealthProbe:
    responded: bool
    status: int | None = None
    body: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class StopResult:
    success: bool
    stopped_pids: tuple[int, ...]
    skipped_pids: tuple[int, ...]
    failures: tuple[str, ...]
    used_port_fallback: bool


@dataclass(frozen=True)
class LaunchResult:
    success: bool
    already_running: bool = False


def _spec_from_config(name: str, value: Mapping[str, Any], context: Mapping[str, str]) -> ServiceSpec:
    return ServiceSpec(
        name=name,
        label=str(value["label"]),
        command=tuple(expand_template(item, context) for item in value["command"]),
        cwd=Path(expand_template(str(value["cwd"]), context)).expanduser(),
        ports=tuple(int(port) for port in value["ports"]),
        health_url=str(value["health"]["url"]),
        expected_statuses=tuple(int(status) for status in value["health"]["expected_statuses"]),
        identity_marker_groups=tuple(
            tuple(str(marker) for marker in group) for group in value["identity_marker_groups"]
        ),
        environment={str(key): str(item) for key, item in value.get("environment", {}).items()},
        critical_workers=tuple(str(worker) for worker in value["health"].get("critical_workers", [])),
    )


def build_service_specs(
    config: Mapping[str, Any],
    *,
    launcher_dir: Path,
    pythonw_path: Path,
    render_credentials: RenderCredentials | None = None,
) -> tuple[ServiceSpec, ...]:
    launcher_dir = Path(launcher_dir).resolve()
    dist_root = resolve_dist_root(config, launcher_dir)
    context = {
        **base_context(launcher_dir),
        "launcher_dir": str(launcher_dir),
        "pythonw": str(Path(pythonw_path)),
        "dist_root": str(dist_root),
    }
    agent = _spec_from_config("agent", config["services"]["agent"], context)
    web_mode = str(config["web"]["mode"])
    selected_web = dict(config["web"][web_mode])
    selected_web.update(
        {
            "label": "画布网页服务",
            "ports": config["web"]["ports"],
            "health": config["web"]["health"],
            "environment": {},
        }
    )
    web = _spec_from_config("web", selected_web, context)
    workbench = _spec_from_config("workbench", config["services"]["workbench"], context)
    if render_credentials is not None:
        environment = dict(workbench.environment)
        environment.setdefault("OPENAI_API_KEY", render_credentials.api_key)
        environment.setdefault("OPENAI_BASE_URL", render_credentials.base_url)
        environment.setdefault("RENDER_ALLOW_REAL_EXECUTION", "1")
        if render_credentials.max_images_per_run is not None:
            environment.setdefault(
                "RENDER_MAX_IMAGES",
                str(render_credentials.max_images_per_run),
            )
        workbench = replace(workbench, environment=environment)
    return agent, web, workbench


def build_watchdog_spec(*, launcher_dir: Path, pythonw_path: Path) -> ManagedProcessSpec:
    launcher_dir = Path(launcher_dir).resolve()
    return ManagedProcessSpec(
        name="watchdog",
        label="画布自动收摊看门狗",
        command=(
            str(Path(pythonw_path)),
            str(launcher_dir / "canvas_watchdog.py"),
        ),
        cwd=launcher_dir.parent,
        identity_marker_groups=(("launcher/canvas_watchdog.py",),),
        environment={},
    )


def configured_state_path(config: Mapping[str, Any]) -> Path:
    return Path(config["paths"]["state_file"]).expanduser()


def configured_log_dir(config: Mapping[str, Any]) -> Path:
    return Path(config["paths"]["log_dir"]).expanduser()


def _decode_health_body(payload: bytes) -> Mapping[str, Any] | None:
    if len(payload) > 65_536:
        return None
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def make_http_health_checker(timeout: float) -> Callable[[ServiceSpec], HealthProbe]:
    def check(spec: ServiceSpec) -> HealthProbe:
        request = urllib.request.Request(spec.health_url, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read(65_537)
                return HealthProbe(True, int(response.status), _decode_health_body(payload))
        except urllib.error.HTTPError as error:
            payload = error.read(65_537)
            return HealthProbe(True, int(error.code), _decode_health_body(payload))
        except (OSError, TimeoutError, urllib.error.URLError):
            return HealthProbe(False)

    return check


def _coerce_probe(value: HealthProbe | bool) -> HealthProbe:
    if isinstance(value, HealthProbe):
        return value
    return HealthProbe(responded=bool(value), status=200 if value else None)


def _startup_ready(spec: ServiceSpec, probe: HealthProbe) -> bool:
    if spec.name == "workbench":
        return probe.responded and probe.status in {200, 503}
    return probe.responded and probe.status in spec.expected_statuses


def _critical_worker_stopped(spec: ServiceSpec, probe: HealthProbe) -> str | None:
    if not spec.critical_workers or not isinstance(probe.body, Mapping):
        return None
    raw_workers = probe.body.get("workers")
    workers = raw_workers if isinstance(raw_workers, Mapping) else probe.body
    for name in spec.critical_workers:
        worker = workers.get(name) if isinstance(workers, Mapping) else None
        if isinstance(worker, Mapping) and worker.get("status") == "stopped":
            return name
    return None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _command_summary(command: Sequence[str]) -> str:
    if not command:
        return ""
    summary = " ".join([Path(command[0]).name, *command[1:]])
    return summary[:512]


def _command_digest(command: Sequence[str]) -> str:
    return hashlib.sha256("\0".join(command).encode("utf-8")).hexdigest()


def _stop_verified_pid(
    process_manager: Any,
    spec: ServiceSpec | ManagedProcessSpec,
    pid: int,
    *,
    terminate_timeout_seconds: float,
    logger: logging.Logger,
) -> str:
    try:
        record = process_manager.get_process(pid)
    except ProcessControlError as error:
        logger.error("进程身份核对失败 service=%s pid=%s error=%s", spec.name, pid, error)
        return "failed"
    if record is None:
        return "gone"
    if not matches_identity(record, spec.identity_marker_groups):
        logger.warning("跳过身份不符进程 service=%s pid=%s", spec.name, pid)
        return "skipped"
    logger.info("请求停止 service=%s pid=%s", spec.name, pid)
    try:
        process_manager.terminate_tree(pid, force=False)
        if process_manager.wait_for_exit(pid, terminate_timeout_seconds):
            return "stopped"
        logger.warning("普通停止超时，升级强制停止 service=%s pid=%s", spec.name, pid)
        process_manager.terminate_tree(pid, force=True)
        if process_manager.wait_for_exit(pid, terminate_timeout_seconds):
            return "killed"
    except ProcessControlError as error:
        logger.error("停止失败 service=%s pid=%s error=%s", spec.name, pid, error)
    return "failed"


class StopController:
    def __init__(
        self,
        *,
        specs: Sequence[ServiceSpec],
        process_manager: Any,
        state_store: StateStore,
        terminate_timeout_seconds: float,
        logger: logging.Logger,
        watchdog_spec: ManagedProcessSpec | None = None,
        exclude_pids: Sequence[int] = (),
    ):
        self.specs = tuple(specs)
        self.process_manager = process_manager
        self.state_store = state_store
        self.terminate_timeout_seconds = terminate_timeout_seconds
        self.logger = logger
        self.watchdog_spec = watchdog_spec
        self.exclude_pids = {
            int(pid)
            for pid in exclude_pids
            if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0
        }

    def stop(self) -> StopResult:
        stopped: list[int] = []
        skipped: list[int] = []
        failures: list[str] = []
        attempted: set[int] = set()
        used_port_fallback = False
        try:
            state = self.state_store.read()
        except StateFileError as error:
            state = None
            used_port_fallback = True
            self.logger.warning("%s；改用端口与身份双重核对", error)

        if self.watchdog_spec is not None:
            watchdog_entry = (state or {}).get("watchdog")
            watchdog_pid = (
                watchdog_entry.get("pid")
                if isinstance(watchdog_entry, dict)
                else None
            )
            if (
                isinstance(watchdog_pid, int)
                and not isinstance(watchdog_pid, bool)
                and watchdog_pid > 0
            ):
                attempted.add(watchdog_pid)
                if watchdog_pid in self.exclude_pids:
                    self.logger.info("排除看门狗自身 PID=%s", watchdog_pid)
                else:
                    outcome = _stop_verified_pid(
                        self.process_manager,
                        self.watchdog_spec,
                        watchdog_pid,
                        terminate_timeout_seconds=self.terminate_timeout_seconds,
                        logger=self.logger,
                    )
                    if outcome in {"stopped", "killed"}:
                        stopped.append(watchdog_pid)
                    elif outcome == "skipped":
                        skipped.append(watchdog_pid)
                    elif outcome == "failed":
                        failures.append(f"{self.watchdog_spec.label}（PID {watchdog_pid}）未能停止")

        state_services = (state or {}).get("services", {})
        for spec in reversed(self.specs):
            entry = state_services.get(spec.name)
            if not isinstance(entry, dict):
                continue
            pid = int(entry["pid"])
            attempted.add(pid)
            outcome = _stop_verified_pid(
                self.process_manager,
                spec,
                pid,
                terminate_timeout_seconds=self.terminate_timeout_seconds,
                logger=self.logger,
            )
            if outcome in {"stopped", "killed"}:
                stopped.append(pid)
            elif outcome == "skipped":
                skipped.append(pid)
            elif outcome == "failed":
                failures.append(f"{spec.label}（PID {pid}）未能停止")

        for spec in reversed(self.specs):
            for port in spec.ports:
                try:
                    pids = self.process_manager.listener_pids(port)
                except ProcessControlError as error:
                    failures.append(str(error))
                    continue
                if pids:
                    used_port_fallback = True
                for pid in pids:
                    if pid in attempted:
                        continue
                    attempted.add(pid)
                    outcome = _stop_verified_pid(
                        self.process_manager,
                        spec,
                        pid,
                        terminate_timeout_seconds=self.terminate_timeout_seconds,
                        logger=self.logger,
                    )
                    if outcome in {"stopped", "killed"}:
                        stopped.append(pid)
                    elif outcome == "skipped":
                        skipped.append(pid)
                        failures.append(f"端口 {port} 被无关进程占用，已安全跳过")
                    elif outcome == "failed":
                        failures.append(f"{spec.label}（端口 {port}）未能停止")

        if not failures:
            try:
                self.state_store.delete()
            except StateFileError as error:
                failures.append(str(error))
        return StopResult(
            success=not failures,
            stopped_pids=tuple(stopped),
            skipped_pids=tuple(skipped),
            failures=tuple(failures),
            used_port_fallback=used_port_fallback,
        )


class LauncherController:
    def __init__(
        self,
        *,
        specs: Sequence[ServiceSpec],
        process_manager: Any,
        state_store: StateStore,
        health_checker: Callable[[ServiceSpec], HealthProbe | bool],
        browser_opener: Callable[[str], None],
        message_box: Callable[[str, str, bool], None],
        browser_url: str,
        log_dir: Path,
        startup_timeout_seconds: float,
        health_poll_seconds: float,
        terminate_timeout_seconds: float,
        log_max_bytes: int,
        log_backups: int,
        logger: logging.Logger,
        sleep=time.sleep,
        monotonic=time.monotonic,
        connection_ready_timeout_seconds: float | None = None,
        watchdog_spec: ManagedProcessSpec | None = None,
    ):
        self.specs = tuple(specs)
        self.process_manager = process_manager
        self.state_store = state_store
        self.health_checker = health_checker
        self.browser_opener = browser_opener
        self.message_box = message_box
        self.browser_url = browser_url
        self.log_dir = Path(log_dir)
        self.startup_timeout_seconds = startup_timeout_seconds
        self.health_poll_seconds = health_poll_seconds
        self.terminate_timeout_seconds = terminate_timeout_seconds
        self.log_max_bytes = log_max_bytes
        self.log_backups = log_backups
        self.logger = logger
        self.sleep = sleep
        self.monotonic = monotonic
        self.connection_ready_timeout_seconds = (
            startup_timeout_seconds
            if connection_ready_timeout_seconds is None
            else connection_ready_timeout_seconds
        )
        self.watchdog_spec = watchdog_spec

    def _stop_controller(self) -> StopController:
        return StopController(
            specs=self.specs,
            process_manager=self.process_manager,
            state_store=self.state_store,
            terminate_timeout_seconds=self.terminate_timeout_seconds,
            logger=self.logger,
            watchdog_spec=self.watchdog_spec,
        )

    def _has_listener(self) -> bool:
        for spec in self.specs:
            for port in spec.ports:
                if self.process_manager.listener_pids(port):
                    return True
        return False

    def _probe(self, spec: ServiceSpec) -> HealthProbe:
        return _coerce_probe(self.health_checker(spec))

    def _wait_startup_ready(self, spec: ServiceSpec, deadline: float) -> HealthProbe | None:
        while self.monotonic() < deadline:
            probe = self._probe(spec)
            self.logger.info(
                "第一段轮询 service=%s responded=%s status=%s",
                spec.name,
                probe.responded,
                probe.status,
            )
            stopped_worker = _critical_worker_stopped(spec, probe)
            if stopped_worker is not None:
                raise RuntimeError(f"{spec.label}的关键工人 {stopped_worker} 已停止。")
            if _startup_ready(spec, probe):
                return probe
            self.sleep(self.health_poll_seconds)
        return None

    def _workbench_pid(
        self,
        spawned: Sequence[tuple[ServiceSpec | ManagedProcessSpec, int]],
    ) -> int | None:
        for spec, pid in reversed(tuple(spawned)):
            if spec.name == "workbench":
                return pid
        try:
            state = self.state_store.read()
        except StateFileError:
            return None
        entry = ((state or {}).get("services") or {}).get("workbench")
        return int(entry["pid"]) if isinstance(entry, dict) and isinstance(entry.get("pid"), int) else None

    def _ensure_watchdog(
        self,
        spawned: list[tuple[ServiceSpec | ManagedProcessSpec, int]],
    ) -> None:
        spec = self.watchdog_spec
        if spec is None:
            return
        state = self.state_store.read()
        entry = (state or {}).get("watchdog")
        pid = entry.get("pid") if isinstance(entry, dict) else None
        if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
            record = self.process_manager.get_process(pid)
            if record is not None and matches_identity(record, spec.identity_marker_groups):
                self.logger.info("复用已有看门狗 pid=%s", pid)
                return
            self.logger.warning("看门狗状态已失效，准备拉起新实例 pid=%s", pid)

        self.logger.info("拉起看门狗")
        watchdog_pid = self.process_manager.spawn(
            spec,
            self.log_dir / "watchdog.log",
            max_bytes=self.log_max_bytes,
            backups=self.log_backups,
        )
        next_state = state or {
            "version": 1,
            "started_at": _utc_now(),
            "services": {},
        }
        next_state["watchdog"] = {
            "pid": watchdog_pid,
            "command_summary": _command_summary(spec.command),
            "command_digest": _command_digest(spec.command),
            "started_at": _utc_now(),
        }
        try:
            self.state_store.write(next_state)
        except StateFileError:
            _stop_verified_pid(
                self.process_manager,
                spec,
                watchdog_pid,
                terminate_timeout_seconds=self.terminate_timeout_seconds,
                logger=self.logger,
            )
            raise
        spawned.append((spec, watchdog_pid))
        self.logger.info("看门狗已启动 pid=%s", watchdog_pid)

    def _wait_connection_ready(
        self,
        workbench: ServiceSpec,
        *,
        initial_probe: HealthProbe,
        spawned: Sequence[tuple[ServiceSpec | ManagedProcessSpec, int]],
    ) -> str:
        if initial_probe.status == 200:
            self.logger.info("第二段无需等待：工作台已全链就绪")
            return "ready"
        started_at = self.monotonic()
        deadline = started_at + self.connection_ready_timeout_seconds
        workbench_pid = self._workbench_pid(spawned)
        while self.monotonic() < deadline:
            probe = self._probe(workbench)
            elapsed = self.monotonic() - started_at
            self.logger.info(
                "第二段轮询 elapsed=%.3fs responded=%s status=%s",
                elapsed,
                probe.responded,
                probe.status,
            )
            stopped_worker = _critical_worker_stopped(workbench, probe)
            if stopped_worker is not None:
                raise RuntimeError(f"{workbench.label}的关键工人 {stopped_worker} 已停止。")
            if workbench_pid is not None and self.process_manager.get_process(workbench_pid) is None:
                raise RuntimeError(f"{workbench.label}进程已退出。")
            if probe.status == 200:
                self.logger.info("全链就绪 second_stage_elapsed=%.3fs", elapsed)
                return "ready"
            self.sleep(self.health_poll_seconds)
        self.logger.warning(
            "第二段连接等待超时 elapsed=%.3fs，服务保持运行",
            self.monotonic() - started_at,
        )
        return "timeout"

    def _cleanup_spawned(
        self,
        spawned: Sequence[tuple[ServiceSpec | ManagedProcessSpec, int]],
    ) -> None:
        cleanup_failed = False
        for spec, pid in reversed(tuple(spawned)):
            outcome = _stop_verified_pid(
                self.process_manager,
                spec,
                pid,
                terminate_timeout_seconds=self.terminate_timeout_seconds,
                logger=self.logger,
            )
            cleanup_failed = cleanup_failed or outcome in {"failed", "skipped"}
        if spawned and not cleanup_failed:
            try:
                self.state_store.delete()
            except StateFileError as error:
                self.logger.error("%s", error)

    def _fail(
        self,
        message: str,
        spawned: Sequence[tuple[ServiceSpec | ManagedProcessSpec, int]],
    ) -> LaunchResult:
        self.logger.error("启动失败：%s", message)
        self._cleanup_spawned(spawned)
        detail = f"{message}\n请查看日志：{self.log_dir / 'launcher.log'}"
        self.message_box("无限画布工作台", detail, True)
        return LaunchResult(False)

    def _connection_timeout(self, *, already_running: bool) -> LaunchResult:
        self.message_box(
            "无限画布工作台",
            "画布已经打开，但工作台尚未与画布连接。通常需要在画布页面确认本地助手已连接；"
            "服务仍在运行。要停止请使用「停止画布工作台」。"
            f"\n请查看日志：{self.log_dir / 'launcher.log'}",
            True,
        )
        return LaunchResult(False, already_running=already_running)

    def run(self) -> LaunchResult:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.logger.info("开始启动无限画布工作台")
        spawned: list[tuple[ServiceSpec | ManagedProcessSpec, int]] = []
        try:
            first_stage_started = self.monotonic()
            initial_probes = {spec.name: self._probe(spec) for spec in self.specs}
            initial_ready = {
                spec.name: _startup_ready(spec, initial_probes[spec.name]) for spec in self.specs
            }
            self.logger.info("启动前第一段状态 %s", initial_ready)
            workbench = next(spec for spec in self.specs if spec.name == "workbench")
            stopped_worker = _critical_worker_stopped(workbench, initial_probes["workbench"])
            if stopped_worker is not None:
                return self._fail(f"{workbench.label}的关键工人 {stopped_worker} 已停止。", spawned)
            if all(initial_ready.values()):
                self._ensure_watchdog(spawned)
                self.browser_opener(self.browser_url)
                self.logger.info(
                    "第一段就绪并打开画布 elapsed=%.3fs",
                    self.monotonic() - first_stage_started,
                )
                connection = self._wait_connection_ready(
                    workbench,
                    initial_probe=initial_probes["workbench"],
                    spawned=spawned,
                )
                if connection == "ready":
                    return LaunchResult(True, already_running=True)
                return self._connection_timeout(already_running=True)

            if any(initial_ready.values()) or self.state_store.path.exists() or self._has_listener():
                self.logger.info("发现部分运行或旧状态，先执行安全清理")
                stopped = self._stop_controller().stop()
                if not stopped.success:
                    return self._fail("检测到部分服务，但未能在身份核对后安全清理。", spawned)

            deadline = self.monotonic() + self.startup_timeout_seconds
            state: dict[str, Any] = {
                "version": 1,
                "started_at": _utc_now(),
                "services": {},
            }
            self.state_store.write(state)
            for spec in self.specs:
                log_path = self.log_dir / f"{spec.name}.log"
                self.logger.info("拉起服务 service=%s", spec.name)
                pid = self.process_manager.spawn(
                    spec,
                    log_path,
                    max_bytes=self.log_max_bytes,
                    backups=self.log_backups,
                )
                spawned.append((spec, pid))
                state["services"][spec.name] = {
                    "pid": pid,
                    "ports": list(spec.ports),
                    "command_summary": _command_summary(spec.command),
                    "command_digest": _command_digest(spec.command),
                    "started_at": _utc_now(),
                }
                self.state_store.write(state)
                probe = self._wait_startup_ready(spec, deadline)
                if probe is None:
                    return self._fail(f"{spec.label}未在规定时间内就绪。", spawned)

            self._ensure_watchdog(spawned)
            self.browser_opener(self.browser_url)
            self.logger.info(
                "第一段就绪并打开画布 elapsed=%.3fs",
                self.monotonic() - first_stage_started,
            )
            connection = self._wait_connection_ready(
                workbench,
                initial_probe=probe,
                spawned=spawned,
            )
            if connection == "timeout":
                return self._connection_timeout(already_running=False)
            return LaunchResult(True)
        except (OSError, RuntimeError, StateFileError, ProcessControlError) as error:
            return self._fail(str(error), spawned)
