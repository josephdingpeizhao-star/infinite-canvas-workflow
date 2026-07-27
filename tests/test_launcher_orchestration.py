import logging
import tempfile
import unittest
from pathlib import Path

from launcher.orchestrator import HealthProbe, LauncherController, ServiceSpec
from launcher.process_control import ProcessRecord
from launcher.state_store import StateStore


def service(name: str, port: int) -> ServiceSpec:
    return ServiceSpec(
        name=name,
        label={"agent": "canvas-agent", "web": "画布网页服务", "workbench": "画布工作台服务"}[name],
        command=(f"{name}.exe", "serve"),
        cwd=Path(r"C:\dp01"),
        ports=(port,),
        health_url=f"http://127.0.0.1:{port}/health",
        expected_statuses=(200,),
        identity_marker_groups=((f"{name}.exe", "serve"),),
        environment={},
        critical_workers=(
            ("batch_intake", "workflow_production", "style_reference_intake")
            if name == "workbench"
            else ()
        ),
    )


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def monotonic(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class FakeHealth:
    def __init__(self, responses):
        self.responses = {key: list(value) for key, value in responses.items()}

    def __call__(self, spec):
        values = self.responses[spec.name]
        if len(values) > 1:
            return values.pop(0)
        return values[0]


class FakeProcessManager:
    def __init__(self):
        self.next_pid = 100
        self.spawned = []
        self.records = {}
        self.listeners = {}
        self.terminated = []
        self.killed = []

    def add_existing(self, spec, pid):
        self.records[pid] = ProcessRecord(pid, spec.command[0], " ".join(spec.command))
        for port in spec.ports:
            self.listeners.setdefault(port, []).append(pid)

    def spawn(self, spec, log_path, *, max_bytes, backups):
        pid = self.next_pid
        self.next_pid += 1
        self.spawned.append((spec.name, log_path))
        self.add_existing(spec, pid)
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


class LauncherOrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.specs = (service("agent", 17371), service("web", 3000), service("workbench", 17373))
        self.logger = logging.getLogger(f"launcher-test-{id(self)}")
        self.logger.addHandler(logging.NullHandler())

    def controller(
        self,
        temp,
        process,
        health,
        clock,
        opened,
        messages,
        timeout=4,
        connection_timeout=2,
    ):
        return LauncherController(
            specs=self.specs,
            process_manager=process,
            state_store=StateStore(Path(temp) / "launcher_state.json"),
            health_checker=health,
            browser_opener=opened.append,
            message_box=lambda title, message, error: messages.append((title, message, error)),
            browser_url="http://localhost:3000/canvas/test",
            log_dir=Path(temp) / "logs",
            startup_timeout_seconds=timeout,
            health_poll_seconds=1,
            terminate_timeout_seconds=1,
            log_max_bytes=1024,
            log_backups=2,
            logger=self.logger,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            connection_ready_timeout_seconds=connection_timeout,
        )

    def test_all_ready_opens_the_browser_exactly_once_without_spawning(self):
        with tempfile.TemporaryDirectory() as temp:
            process = FakeProcessManager()
            opened, messages = [], []
            controller = self.controller(
                temp,
                process,
                FakeHealth({"agent": [True], "web": [True], "workbench": [True]}),
                FakeClock(),
                opened,
                messages,
            )

            result = controller.run()

        self.assertTrue(result.success)
        self.assertTrue(result.already_running)
        self.assertEqual(opened, ["http://localhost:3000/canvas/test"])
        self.assertEqual(process.spawned, [])
        self.assertEqual(messages, [])

    def test_timeout_reports_the_service_and_cleans_the_spawned_process(self):
        with tempfile.TemporaryDirectory() as temp:
            process = FakeProcessManager()
            opened, messages = [], []
            controller = self.controller(
                temp,
                process,
                FakeHealth({"agent": [False], "web": [False], "workbench": [False]}),
                FakeClock(),
                opened,
                messages,
                timeout=2,
            )

            result = controller.run()

        self.assertFalse(result.success)
        self.assertEqual([name for name, _ in process.spawned], ["agent"])
        self.assertEqual(process.terminated, [100])
        self.assertEqual(opened, [])
        self.assertEqual(len(messages), 1)
        self.assertIn("canvas-agent", messages[0][1])
        self.assertIn(str(Path(temp) / "logs" / "launcher.log"), messages[0][1])

    def test_partial_health_safely_stops_owned_state_then_restarts_all_services(self):
        with tempfile.TemporaryDirectory() as temp:
            process = FakeProcessManager()
            process.add_existing(self.specs[0], 900)
            store = StateStore(Path(temp) / "launcher_state.json")
            store.write(
                {
                    "version": 1,
                    "started_at": "2026-07-27T00:00:00Z",
                    "services": {
                        "agent": {
                            "pid": 900,
                            "ports": [17371],
                            "command_summary": "agent.exe serve",
                            "command_digest": "x",
                            "started_at": "2026-07-27T00:00:00Z",
                        }
                    },
                }
            )
            opened, messages = [], []
            clock = FakeClock()
            controller = LauncherController(
                specs=self.specs,
                process_manager=process,
                state_store=store,
                health_checker=FakeHealth(
                    {
                        "agent": [True, True],
                        "web": [False, True],
                        "workbench": [False, True],
                    }
                ),
                browser_opener=opened.append,
                message_box=lambda title, message, error: messages.append((title, message, error)),
                browser_url="http://localhost:3000/canvas/test",
                log_dir=Path(temp) / "logs",
                startup_timeout_seconds=4,
                health_poll_seconds=1,
                terminate_timeout_seconds=1,
                log_max_bytes=1024,
                log_backups=2,
                logger=self.logger,
                sleep=clock.sleep,
                monotonic=clock.monotonic,
                connection_ready_timeout_seconds=2,
            )

            result = controller.run()

        self.assertTrue(result.success)
        self.assertEqual(process.terminated[0], 900)
        self.assertEqual([name for name, _ in process.spawned], ["agent", "web", "workbench"])
        self.assertEqual(opened, ["http://localhost:3000/canvas/test"])
        self.assertEqual(messages, [])

    def test_workbench_503_counts_as_running_opens_once_then_200_exits_silently(self):
        waiting = HealthProbe(
            True,
            503,
            {
                "workers": {
                    "batch_intake": {"status": "running"},
                    "workflow_production": {"status": "running"},
                    "style_reference_intake": {"status": "waiting_canvas"},
                }
            },
        )
        ready = HealthProbe(True, 200, {"workers": {}})
        with tempfile.TemporaryDirectory() as temp:
            process = FakeProcessManager()
            opened, messages = [], []
            controller = self.controller(
                temp,
                process,
                FakeHealth({"agent": [True], "web": [True], "workbench": [waiting, ready]}),
                FakeClock(),
                opened,
                messages,
            )

            result = controller.run()

        self.assertTrue(result.success)
        self.assertTrue(result.already_running)
        self.assertEqual(opened, ["http://localhost:3000/canvas/test"])
        self.assertEqual(process.spawned, [])
        self.assertEqual(messages, [])

    def test_second_stage_timeout_keeps_all_services_and_state_then_warns_once(self):
        waiting = HealthProbe(
            True,
            503,
            {
                "workers": {
                    "batch_intake": {"status": "running"},
                    "workflow_production": {"status": "running"},
                    "style_reference_intake": {"status": "waiting_canvas"},
                }
            },
        )
        with tempfile.TemporaryDirectory() as temp:
            process = FakeProcessManager()
            opened, messages = [], []
            controller = self.controller(
                temp,
                process,
                FakeHealth(
                    {
                        "agent": [False, True],
                        "web": [False, True],
                        "workbench": [False, waiting],
                    }
                ),
                FakeClock(),
                opened,
                messages,
                connection_timeout=2,
            )

            result = controller.run()
            state_exists = (Path(temp) / "launcher_state.json").exists()

        self.assertFalse(result.success)
        self.assertEqual(opened, ["http://localhost:3000/canvas/test"])
        self.assertEqual(process.terminated, [])
        self.assertEqual(process.killed, [])
        self.assertTrue(state_exists)
        self.assertEqual(len(messages), 1)
        self.assertIn("服务仍在运行", messages[0][1])

    def test_stopped_critical_worker_is_an_early_failure_and_cleans_this_start(self):
        stopped = HealthProbe(
            True,
            503,
            {
                "workers": {
                    "batch_intake": {"status": "running"},
                    "workflow_production": {"status": "stopped"},
                    "style_reference_intake": {"status": "waiting_canvas"},
                }
            },
        )
        with tempfile.TemporaryDirectory() as temp:
            process = FakeProcessManager()
            opened, messages = [], []
            controller = self.controller(
                temp,
                process,
                FakeHealth(
                    {
                        "agent": [False, True],
                        "web": [False, True],
                        "workbench": [False, stopped],
                    }
                ),
                FakeClock(),
                opened,
                messages,
            )

            result = controller.run()

        self.assertFalse(result.success)
        self.assertEqual(opened, [])
        self.assertEqual(process.terminated, [102, 101, 100])
        self.assertEqual(len(messages), 1)
        self.assertIn("workflow_production", messages[0][1])


if __name__ == "__main__":
    unittest.main()
