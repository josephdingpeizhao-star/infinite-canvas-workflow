import errno
import inspect
import json
import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from launcher.canvas_watchdog import BatchLockProbe, LockProbeResult, WatchdogController
from launcher.canvas_watchdog import RuntimeStateInspector
from launcher.config import LauncherConfigError, load_config
from launcher.orchestrator import (
    HealthProbe,
    LauncherController,
    ManagedProcessSpec,
    ServiceSpec,
    StopController,
)
from launcher.process_control import ProcessRecord
from launcher.state_store import StateStore


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = REPO_ROOT / "launcher" / "launcher_config.json"
CRITICAL_WORKERS = ("batch_intake", "workflow_production", "style_reference_intake")


def workbench_probe(status, *, worker_statuses=None):
    if status == 200:
        return HealthProbe(True, 200, {"workers": {}})
    statuses = worker_statuses or {
        "batch_intake": "running",
        "workflow_production": "running",
        "style_reference_intake": "waiting_canvas",
    }
    return HealthProbe(
        True,
        status,
        {
            "workers": {
                name: {"status": value, "lastStatusAt": 1}
                for name, value in statuses.items()
            }
        },
    )


def service(name, port):
    return ServiceSpec(
        name=name,
        label=name,
        command=(f"{name}.exe", "serve"),
        cwd=Path(r"C:\dp01b"),
        ports=(port,),
        health_url=f"http://127.0.0.1:{port}/health",
        expected_statuses=(200,),
        identity_marker_groups=((f"{name}.exe", "serve"),),
        environment={},
        critical_workers=CRITICAL_WORKERS if name == "workbench" else (),
    )


def watchdog_spec():
    return ManagedProcessSpec(
        name="watchdog",
        label="画布自动收摊看门狗",
        command=(r"C:\Python312\pythonw.exe", r"C:\repo\launcher\canvas_watchdog.py"),
        cwd=Path(r"C:\repo"),
        identity_marker_groups=(("launcher/canvas_watchdog.py",),),
        environment={},
    )


class FakeClock:
    def __init__(self, sleep_steps=None):
        self.value = 0.0
        self.sleep_steps = list(sleep_steps or ())

    def monotonic(self):
        return self.value

    def sleep(self, seconds):
        self.value += self.sleep_steps.pop(0) if self.sleep_steps else seconds


class SequenceCallable:
    def __init__(self, values):
        self.values = list(values)
        self.calls = 0

    def __call__(self, *_args, **_kwargs):
        self.calls += 1
        if len(self.values) > 1:
            return self.values.pop(0)
        return self.values[0]


class ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(self.format(record))


class FakeProcessManager:
    def __init__(self):
        self.next_pid = 100
        self.spawned = []
        self.records = {}
        self.listeners = {}
        self.terminated = []
        self.killed = []

    def add(self, spec, pid, *, command_line=None):
        self.records[pid] = ProcessRecord(
            pid,
            spec.command[0],
            command_line or " ".join(spec.command),
        )
        for port in getattr(spec, "ports", ()):
            self.listeners.setdefault(port, []).append(pid)

    def spawn(self, spec, log_path, *, max_bytes, backups):
        pid = self.next_pid
        self.next_pid += 1
        self.spawned.append((spec.name, Path(log_path), max_bytes, backups))
        self.add(spec, pid)
        return pid

    def get_process(self, pid):
        return self.records.get(pid)

    def listener_pids(self, port):
        return list(self.listeners.get(port, ()))

    def terminate_tree(self, pid, *, force):
        (self.killed if force else self.terminated).append(pid)
        self.records.pop(pid, None)
        for pids in self.listeners.values():
            if pid in pids:
                pids.remove(pid)
        return True

    def wait_for_exit(self, pid, timeout):
        return pid not in self.records


class WatchdogStateMachineTests(unittest.TestCase):
    def setUp(self):
        self.logger = logging.getLogger(f"watchdog-test-{id(self)}")
        self.logger.handlers.clear()
        self.logger.propagate = False
        self.handler = ListHandler()
        self.logger.addHandler(self.handler)
        self.logger.setLevel(logging.INFO)

    def controller(
        self,
        *,
        health,
        lock_results=None,
        stop_result=None,
        services_gone=None,
        superseded=None,
        clock=None,
        grace=2,
        poll=1,
        suspend_gap=10,
    ):
        active_clock = clock or FakeClock()
        lock_probe = SequenceCallable(
            lock_results or [LockProbeResult(True, "idle", checked_count=0)]
        )
        stop_services = SequenceCallable(
            stop_result or [SimpleNamespace(success=True, failures=())]
        )
        controller = WatchdogController(
            workbench_spec=service("workbench", 17373),
            health_checker=SequenceCallable(health),
            lock_probe=lock_probe,
            lock_root=Path(r"C:\locks"),
            stop_services=stop_services,
            services_are_gone=services_gone or (lambda: False),
            is_superseded=superseded or (lambda: False),
            disconnect_grace_seconds=grace,
            poll_seconds=poll,
            suspend_gap_threshold_seconds=suspend_gap,
            logger=self.logger,
            sleep=active_clock.sleep,
            monotonic=active_clock.monotonic,
        )
        return controller, lock_probe, stop_services

    def test_never_armed_disconnect_never_stops(self):
        clock = FakeClock()
        controller, lock_probe, stop_services = self.controller(
            health=[workbench_probe(503)],
            clock=clock,
            grace=1,
        )

        result = controller.run(max_cycles=5)

        self.assertEqual(result, "test_limit")
        self.assertEqual(lock_probe.calls, 0)
        self.assertEqual(stop_services.calls, 0)

    def test_disconnect_grace_and_intermediate_200_reset_the_timer(self):
        clock = FakeClock()
        controller, lock_probe, stop_services = self.controller(
            health=[
                workbench_probe(200),
                workbench_probe(503),
                workbench_probe(503),
                workbench_probe(200),
                workbench_probe(503),
                workbench_probe(503),
                workbench_probe(503),
            ],
            clock=clock,
            grace=2,
        )

        result = controller.run(max_cycles=8)

        self.assertEqual(result, "stopped")
        self.assertEqual(lock_probe.calls, 1)
        self.assertEqual(stop_services.calls, 1)

    def test_busy_lock_denies_then_idle_next_poll_stops_and_names_lock(self):
        clock = FakeClock()
        controller, lock_probe, stop_services = self.controller(
            health=[workbench_probe(200), workbench_probe(503)],
            lock_results=[
                LockProbeResult(False, "busy", lock_file="abc123.lock", checked_count=1),
                LockProbeResult(True, "idle", checked_count=2),
            ],
            clock=clock,
            grace=1,
        )

        result = controller.run(max_cycles=5)

        self.assertEqual(result, "stopped")
        self.assertEqual(lock_probe.calls, 2)
        self.assertEqual(stop_services.calls, 1)
        self.assertTrue(
            any("abc123.lock" in message and "否决" in message for message in self.handler.messages)
        )

    def test_unavailable_lock_facility_never_stops_and_logs_the_denial(self):
        clock = FakeClock()
        controller, lock_probe, stop_services = self.controller(
            health=[workbench_probe(200), workbench_probe(503)],
            lock_results=[
                LockProbeResult(
                    False,
                    "unavailable",
                    lock_file="broken.lock",
                    checked_count=1,
                    detail="无法打开",
                )
            ],
            clock=clock,
            grace=1,
        )

        result = controller.run(max_cycles=5)

        self.assertEqual(result, "test_limit")
        self.assertGreaterEqual(lock_probe.calls, 1)
        self.assertEqual(stop_services.calls, 0)
        self.assertTrue(any("broken.lock" in message for message in self.handler.messages))

    def test_inflight_busy_lock_keeps_services_running_and_remains_diagnostic(self):
        clock = FakeClock()
        controller, lock_probe, stop_services = self.controller(
            health=[workbench_probe(200), workbench_probe(503)],
            lock_results=[
                LockProbeResult(
                    False,
                    "busy",
                    lock_file="f" * 64 + ".lock",
                    checked_count=1,
                    detail="锁正在使用",
                )
            ],
            clock=clock,
            grace=1,
        )

        result = controller.run(max_cycles=6)

        self.assertEqual(result, "test_limit")
        self.assertGreaterEqual(lock_probe.calls, 2)
        self.assertEqual(stop_services.calls, 0)
        self.assertGreaterEqual(
            sum("f" * 64 + ".lock" in message for message in self.handler.messages),
            2,
        )

    def test_large_suspend_gap_resets_disconnect_timer(self):
        clock = FakeClock([1, 100, 1])
        controller, lock_probe, stop_services = self.controller(
            health=[workbench_probe(200), workbench_probe(503)],
            clock=clock,
            grace=2,
            suspend_gap=10,
        )

        result = controller.run(max_cycles=3)

        self.assertEqual(result, "test_limit")
        self.assertEqual(lock_probe.calls, 0)
        self.assertEqual(stop_services.calls, 0)
        self.assertTrue(any("休眠" in message for message in self.handler.messages))

    def test_stopped_critical_worker_does_not_count_as_disconnect(self):
        clock = FakeClock()
        stopped = workbench_probe(
            503,
            worker_statuses={
                "batch_intake": "running",
                "workflow_production": "stopped",
                "style_reference_intake": "waiting_canvas",
            },
        )
        controller, lock_probe, stop_services = self.controller(
            health=[workbench_probe(200), stopped],
            clock=clock,
            grace=1,
        )

        result = controller.run(max_cycles=5)

        self.assertEqual(result, "test_limit")
        self.assertEqual(lock_probe.calls, 0)
        self.assertEqual(stop_services.calls, 0)

    def test_stop_failure_is_attempted_once_and_watchdog_exits(self):
        clock = FakeClock()
        controller, lock_probe, stop_services = self.controller(
            health=[workbench_probe(200), workbench_probe(503)],
            stop_result=[SimpleNamespace(success=False, failures=("workbench failed",))],
            clock=clock,
            grace=1,
        )

        result = controller.run(max_cycles=5)

        self.assertEqual(result, "stop_failed")
        self.assertEqual(lock_probe.calls, 1)
        self.assertEqual(stop_services.calls, 1)
        self.assertTrue(any("停止失败" in message for message in self.handler.messages))

    def test_services_already_gone_exits_without_health_or_stop(self):
        health = SequenceCallable([workbench_probe(200)])
        stop_services = SequenceCallable([SimpleNamespace(success=True, failures=())])
        controller = WatchdogController(
            workbench_spec=service("workbench", 17373),
            health_checker=health,
            lock_probe=SequenceCallable([LockProbeResult(True, "idle")]),
            lock_root=Path(r"C:\locks"),
            stop_services=stop_services,
            services_are_gone=lambda: True,
            is_superseded=lambda: False,
            disconnect_grace_seconds=1,
            poll_seconds=1,
            suspend_gap_threshold_seconds=10,
            logger=self.logger,
            sleep=lambda _seconds: None,
            monotonic=lambda: 0,
        )

        result = controller.run(max_cycles=1)

        self.assertEqual(result, "services_gone")
        self.assertEqual(health.calls, 0)
        self.assertEqual(stop_services.calls, 0)

    def test_controller_has_no_message_box_dependency(self):
        self.assertNotIn("message_box", inspect.signature(WatchdogController).parameters)


class BatchLockProbeTests(unittest.TestCase):
    class IdleLockModule:
        @staticmethod
        def _lock_one_byte(_handle):
            return None

        @staticmethod
        def _unlock_one_byte(_handle):
            return None

        @staticmethod
        def _busy_error(_error):
            return False

    class BusyLockModule(IdleLockModule):
        @staticmethod
        def _lock_one_byte(_handle):
            raise OSError(errno.EACCES, "busy")

        @staticmethod
        def _busy_error(_error):
            return True

    def test_all_idle_lock_files_are_acquired_and_released(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / f"{'a' * 64}.lock").write_bytes(b"0")
            (root / f"{'b' * 64}.lock").write_bytes(b"0")
            result = BatchLockProbe(lock_module=self.IdleLockModule())(root)

        self.assertTrue(result.idle)
        self.assertEqual(result.checked_count, 2)

    def test_busy_result_contains_the_exact_lock_filename(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            lock_name = f"{'d' * 64}.lock"
            (root / lock_name).write_bytes(b"0")
            result = BatchLockProbe(lock_module=self.BusyLockModule())(root)

        self.assertFalse(result.idle)
        self.assertEqual(result.reason, "busy")
        self.assertEqual(result.lock_file, lock_name)

    def test_missing_lock_root_is_unavailable_not_idle(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "missing"
            result = BatchLockProbe(lock_module=self.IdleLockModule())(root)

        self.assertFalse(result.idle)
        self.assertEqual(result.reason, "unavailable")

    def test_malformed_lock_file_denies_and_names_the_file(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            lock_name = f"{'e' * 64}.lock"
            (root / lock_name).write_bytes(b"01")
            result = BatchLockProbe(lock_module=self.IdleLockModule())(root)

        self.assertFalse(result.idle)
        self.assertEqual(result.reason, "unavailable")
        self.assertEqual(result.lock_file, lock_name)

    def test_non_sha256_lock_filename_is_an_abnormal_facility(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "not-a-sha256.lock").write_bytes(b"0")
            result = BatchLockProbe(lock_module=self.IdleLockModule())(root)

        self.assertFalse(result.idle)
        self.assertEqual(result.reason, "unavailable")
        self.assertEqual(result.lock_file, "not-a-sha256.lock")


class WatchdogLauncherIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.specs = (
            service("agent", 17371),
            service("web", 3000),
            service("workbench", 17373),
        )
        self.watchdog = watchdog_spec()
        self.logger = logging.getLogger(f"watchdog-launcher-test-{id(self)}")
        self.logger.handlers.clear()
        self.logger.addHandler(logging.NullHandler())

    def launcher(self, temp, process, store, opened):
        return LauncherController(
            specs=self.specs,
            watchdog_spec=self.watchdog,
            process_manager=process,
            state_store=store,
            health_checker=lambda _spec: HealthProbe(True, 200, {"workers": {}}),
            browser_opener=opened.append,
            message_box=lambda *_args: self.fail("不应弹窗"),
            browser_url="http://localhost:3000/canvas/test",
            log_dir=Path(temp) / "logs",
            startup_timeout_seconds=2,
            connection_ready_timeout_seconds=2,
            health_poll_seconds=1,
            terminate_timeout_seconds=1,
            log_max_bytes=1024,
            log_backups=2,
            logger=self.logger,
            sleep=lambda _seconds: None,
            monotonic=FakeClock().monotonic,
        )

    def test_idempotent_running_services_without_watchdog_record_launches_it(self):
        with tempfile.TemporaryDirectory() as temp:
            process = FakeProcessManager()
            store = StateStore(Path(temp) / "state.json")
            opened = []

            result = self.launcher(temp, process, store, opened).run()
            state = store.read()

        self.assertTrue(result.success)
        self.assertTrue(result.already_running)
        self.assertEqual([name for name, *_rest in process.spawned], ["watchdog"])
        self.assertIn("watchdog", state)
        self.assertNotIn("watchdog", state["services"])
        self.assertEqual(opened, ["http://localhost:3000/canvas/test"])

    def test_existing_verified_watchdog_is_reused_without_spawning(self):
        with tempfile.TemporaryDirectory() as temp:
            process = FakeProcessManager()
            process.add(self.watchdog, 77)
            store = StateStore(Path(temp) / "state.json")
            store.write(
                {
                    "version": 1,
                    "started_at": "2026-07-27T00:00:00Z",
                    "services": {},
                    "watchdog": {
                        "pid": 77,
                        "command_summary": "pythonw.exe launcher/canvas_watchdog.py",
                        "command_digest": "x",
                        "started_at": "2026-07-27T00:00:00Z",
                    },
                }
            )
            opened = []

            result = self.launcher(temp, process, store, opened).run()

        self.assertTrue(result.success)
        self.assertEqual(process.spawned, [])

    def test_manual_stop_stops_watchdog_before_services(self):
        with tempfile.TemporaryDirectory() as temp:
            process = FakeProcessManager()
            store = StateStore(Path(temp) / "state.json")
            entries = {}
            for spec, pid in zip(self.specs, (1, 2, 3)):
                process.add(spec, pid)
                entries[spec.name] = {"pid": pid}
            process.add(self.watchdog, 9)
            store.write(
                {
                    "version": 1,
                    "started_at": "2026-07-27T00:00:00Z",
                    "services": entries,
                    "watchdog": {"pid": 9},
                }
            )

            result = StopController(
                specs=self.specs,
                watchdog_spec=self.watchdog,
                process_manager=process,
                state_store=store,
                terminate_timeout_seconds=1,
                logger=self.logger,
            ).stop()

        self.assertTrue(result.success)
        self.assertEqual(process.terminated, [9, 3, 2, 1])

    def test_watchdog_excludes_its_own_pid_and_success_removes_state(self):
        with tempfile.TemporaryDirectory() as temp:
            process = FakeProcessManager()
            store = StateStore(Path(temp) / "state.json")
            process.add(self.watchdog, 9)
            entries = {}
            for spec, pid in zip(self.specs, (1, 2, 3)):
                process.add(spec, pid)
                entries[spec.name] = {"pid": pid}
            store.write(
                {
                    "version": 1,
                    "started_at": "2026-07-27T00:00:00Z",
                    "services": entries,
                    "watchdog": {"pid": 9},
                }
            )

            result = StopController(
                specs=self.specs,
                watchdog_spec=self.watchdog,
                exclude_pids=(9,),
                process_manager=process,
                state_store=store,
                terminate_timeout_seconds=1,
                logger=self.logger,
            ).stop()

            self.assertTrue(result.success)
            self.assertEqual(process.terminated, [3, 2, 1])
            self.assertFalse(store.path.exists())


class RuntimeStateInspectorTests(unittest.TestCase):
    def setUp(self):
        self.specs = (
            service("agent", 17371),
            service("web", 3000),
            service("workbench", 17373),
        )
        self.watchdog = watchdog_spec()
        self.logger = logging.getLogger(f"watchdog-inspector-test-{id(self)}")
        self.logger.handlers.clear()
        self.logger.addHandler(logging.NullHandler())

    def inspector(self, store, process, health):
        return RuntimeStateInspector(
            specs=self.specs,
            watchdog_spec=self.watchdog,
            state_store=store,
            process_manager=process,
            health_checker=health,
            logger=self.logger,
        )

    def test_all_recorded_services_gone_and_no_health_response_returns_true(self):
        with tempfile.TemporaryDirectory() as temp:
            store = StateStore(Path(temp) / "state.json")
            store.write(
                {
                    "version": 1,
                    "started_at": "2026-07-27T00:00:00Z",
                    "services": {
                        spec.name: {"pid": pid}
                        for spec, pid in zip(self.specs, (1, 2, 3))
                    },
                }
            )
            result = self.inspector(
                store,
                FakeProcessManager(),
                lambda _spec: HealthProbe(False),
            ).services_are_gone()

        self.assertTrue(result)

    def test_old_launcher_state_without_services_stays_alive_while_health_responds(self):
        with tempfile.TemporaryDirectory() as temp:
            store = StateStore(Path(temp) / "state.json")
            store.write(
                {
                    "version": 1,
                    "started_at": "2026-07-27T00:00:00Z",
                    "services": {},
                    "watchdog": {"pid": 9},
                }
            )
            result = self.inspector(
                store,
                FakeProcessManager(),
                lambda spec: (
                    workbench_probe(503)
                    if spec.name == "workbench"
                    else HealthProbe(True, 200)
                ),
            ).services_are_gone()

        self.assertFalse(result)

    def test_later_verified_watchdog_supersedes_the_current_instance(self):
        with tempfile.TemporaryDirectory() as temp:
            store = StateStore(Path(temp) / "state.json")
            store.write(
                {
                    "version": 1,
                    "started_at": "2026-07-27T00:00:00Z",
                    "services": {},
                    "watchdog": {"pid": 22},
                }
            )
            process = FakeProcessManager()
            process.add(self.watchdog, 22)
            inspector = self.inspector(
                store,
                process,
                lambda _spec: HealthProbe(True, 200),
            )

            self.assertTrue(inspector.is_superseded(own_pid=21))
            self.assertFalse(inspector.is_superseded(own_pid=22))


class WatchdogConfigAndShortcutTests(unittest.TestCase):
    def test_default_watchdog_config_is_enabled_and_conservative(self):
        config = load_config(DEFAULT_CONFIG, override_path=REPO_ROOT / "missing-override.json")

        self.assertEqual(
            config["watchdog"],
            {
                "enabled": True,
                "disconnect_grace_seconds": 600,
                "poll_seconds": 15,
                "batch_lock_root": "~/.infinite-canvas/batch-operation-locks",
                "suspend_gap_threshold_seconds": 60,
            },
        )

    def test_invalid_watchdog_config_has_a_human_readable_error(self):
        base = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
        base["watchdog"] = {
            "enabled": "yes",
            "disconnect_grace_seconds": 600,
            "poll_seconds": 15,
            "batch_lock_root": "~/.infinite-canvas/batch-operation-locks",
            "suspend_gap_threshold_seconds": 60,
        }
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "launcher_config.json"
            path.write_text(json.dumps(base), encoding="utf-8")
            with self.assertRaisesRegex(LauncherConfigError, "watchdog.enabled"):
                load_config(path, override_path=Path(raw) / "none.json")

    def test_shortcut_script_defaults_to_start_only_and_maintenance_adds_stop(self):
        path = REPO_ROOT / "launcher" / "创建桌面入口.bat"
        raw = path.read_bytes()
        text = raw.decode("gbk")

        self.assertEqual(text.splitlines()[0].lower(), "@chcp 936 >nul")
        self.assertNotIn("\n", text.replace("\r\n", ""))
        self.assertIn('set "CREATE_STOP=0"', text)
        self.assertIn('if /I "%~1"=="维护" set "CREATE_STOP=1"', text)
        self.assertIn('if "%CREATE_STOP%"=="1" (', text)
        self.assertIn("默认模式只创建“无限画布工作台”", text)
        self.assertNotIn("remove-item", text.casefold())
        self.assertNotIn("del ", text.casefold())


if __name__ == "__main__":
    unittest.main()
