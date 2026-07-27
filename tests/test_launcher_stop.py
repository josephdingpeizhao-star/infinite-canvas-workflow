import logging
import tempfile
import unittest
from pathlib import Path

from launcher.orchestrator import ServiceSpec, StopController
from launcher.process_control import ProcessRecord
from launcher.state_store import StateStore


def spec(name, port):
    return ServiceSpec(
        name=name,
        label=name,
        command=(f"{name}.exe", "serve"),
        cwd=Path(r"C:\dp01"),
        ports=(port,),
        health_url=f"http://127.0.0.1:{port}/health",
        expected_statuses=(200,),
        identity_marker_groups=((f"{name}.exe", "serve"),),
        environment={},
    )


class StopProcessManager:
    def __init__(self):
        self.records = {}
        self.listeners = {}
        self.terminated = []
        self.killed = []
        self.timeout_once = set()
        self.forced = set()

    def add(self, service, pid, *, command_line=None):
        self.records[pid] = ProcessRecord(
            pid,
            service.command[0],
            command_line or " ".join(service.command),
        )
        for port in service.ports:
            self.listeners.setdefault(port, []).append(pid)

    def get_process(self, pid):
        return self.records.get(pid)

    def listener_pids(self, port):
        return list(self.listeners.get(port, ()))

    def terminate_tree(self, pid, *, force):
        if force:
            self.killed.append(pid)
            self.forced.add(pid)
            self._remove(pid)
        else:
            self.terminated.append(pid)
            if pid not in self.timeout_once:
                self._remove(pid)
        return True

    def wait_for_exit(self, pid, timeout):
        if pid in self.timeout_once and pid not in self.forced:
            return False
        return pid not in self.records

    def _remove(self, pid):
        self.records.pop(pid, None)
        for pids in self.listeners.values():
            if pid in pids:
                pids.remove(pid)


class LauncherStopTests(unittest.TestCase):
    def setUp(self):
        self.specs = (spec("agent", 17371), spec("web", 3000), spec("workbench", 17373))
        self.logger = logging.getLogger(f"stop-test-{id(self)}")
        self.logger.addHandler(logging.NullHandler())

    @staticmethod
    def state_for(entries):
        return {
            "version": 1,
            "started_at": "2026-07-27T00:00:00Z",
            "services": {
                name: {
                    "pid": pid,
                    "ports": list(ports),
                    "command_summary": f"{name}.exe serve",
                    "command_digest": "x",
                    "started_at": "2026-07-27T00:00:00Z",
                }
                for name, pid, ports in entries
            },
        }

    def controller(self, process, store):
        return StopController(
            specs=self.specs,
            process_manager=process,
            state_store=store,
            terminate_timeout_seconds=1,
            logger=self.logger,
        )

    def test_normal_stop_uses_workbench_web_agent_order_and_removes_state(self):
        with tempfile.TemporaryDirectory() as raw:
            store = StateStore(Path(raw) / "state.json")
            store.write(
                self.state_for(
                    [
                        ("agent", 1, (17371,)),
                        ("web", 2, (3000,)),
                        ("workbench", 3, (17373,)),
                    ]
                )
            )
            process = StopProcessManager()
            for service, pid in zip(self.specs, (1, 2, 3)):
                process.add(service, pid)

            result = self.controller(process, store).stop()

            self.assertTrue(result.success)
            self.assertEqual(process.terminated, [3, 2, 1])
            self.assertFalse(store.path.exists())

    def test_terminate_timeout_escalates_to_force_kill(self):
        with tempfile.TemporaryDirectory() as raw:
            store = StateStore(Path(raw) / "state.json")
            store.write(self.state_for([("agent", 10, (17371,))]))
            process = StopProcessManager()
            process.add(self.specs[0], 10)
            process.timeout_once.add(10)

            result = self.controller(process, store).stop()

        self.assertTrue(result.success)
        self.assertEqual(process.terminated, [10])
        self.assertEqual(process.killed, [10])

    def test_reused_pid_with_wrong_identity_is_skipped_without_killing(self):
        with tempfile.TemporaryDirectory() as raw:
            store = StateStore(Path(raw) / "state.json")
            store.write(self.state_for([("agent", 20, (17371,))]))
            process = StopProcessManager()
            process.records[20] = ProcessRecord(20, "notepad.exe", "notepad.exe notes.txt")

            result = self.controller(process, store).stop()

        self.assertTrue(result.success)
        self.assertEqual(process.terminated, [])
        self.assertEqual(result.skipped_pids, (20,))

    def test_corrupt_state_uses_port_and_identity_fallback(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "state.json"
            path.write_text("{broken", encoding="utf-8")
            store = StateStore(path)
            process = StopProcessManager()
            process.add(self.specs[0], 30)

            result = self.controller(process, store).stop()

        self.assertTrue(result.success)
        self.assertEqual(process.terminated, [30])
        self.assertTrue(result.used_port_fallback)


if __name__ == "__main__":
    unittest.main()
