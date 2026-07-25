"""Composite local service for isolated M1, M2-a and M2-b workers."""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable

import batch_creator
import ic_client
import workflow_batch_intake_service
import workflow_demo_service
import workflow_production_http_server
import workflow_production_service
import workflow_style_reference_intake
import canvas_readonly_assistant


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE_ROOT = Path.home() / ".infinite-canvas" / "batch-intake"
WORKBENCH_EVENT_NAME = "canvas_workbench.events.jsonl"
CRITICAL_COMPONENTS = frozenset({"batch_intake", "workflow_production", "style_reference_intake"})
ISOLATED_COMPONENTS = frozenset({"workflow_demo"})
WORKER_STATUSES = frozenset({"not_started", "running", "waiting_canvas", "stopped"})
PRODUCTION_DIAGNOSTIC_STEPS = frozenset({"identity", "style_master", "angle_inventory", "main_vc", "detail_vc", "final_prompts"})
PRODUCTION_DIAGNOSTIC_CODES = frozenset({"empty_assistant_response"})


class CriticalWorkerStopped(RuntimeError):
    """A critical worker ended unexpectedly, so the workbench must exit nonzero."""

    def __init__(self, component: str):
        self.component = component
        super().__init__(f"critical canvas workbench worker stopped: {component}")


class WorkbenchEventLedger:
    """Sanitized append-only worker status evidence under the protected state root."""

    def __init__(
        self,
        state_root: Path,
        *,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self.state_root = batch_creator.require_state_root(state_root)
        self.path = self.state_root / WORKBENCH_EVENT_NAME
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._write_lock = threading.Lock()

    def record(self, worker: str, status: str) -> None:
        if worker not in CRITICAL_COMPONENTS | ISOLATED_COMPONENTS or status not in WORKER_STATUSES:
            raise ValueError("工作台状态事件不在允许范围内")
        self._append(
            {
                "event": "worker_status",
                "worker": worker,
                "status": status,
                "recorded_at": self.clock_ms(),
            }
        )

    def record_execution_failure(self, worker: str, step: str, code: str) -> None:
        if worker != "workflow_production" or step not in PRODUCTION_DIAGNOSTIC_STEPS or code not in PRODUCTION_DIAGNOSTIC_CODES:
            raise ValueError("工作台执行失败事件不在允许范围内")
        self._append(
            {
                "event": "execution_failure",
                "worker": worker,
                "step": step,
                "code": code,
                "recorded_at": self.clock_ms(),
            }
        )

    def _append(self, entry: dict[str, int | str]) -> None:
        state_root = batch_creator.require_state_root(self.state_root)
        try:
            unsafe = self.path.is_symlink()
            is_junction = getattr(self.path, "is_junction", None)
            unsafe = unsafe or bool(is_junction and is_junction())
            if self.path.parent.resolve(strict=True) != state_root or unsafe:
                raise OSError
            if self.path.exists() and not self.path.is_file():
                raise OSError
        except (OSError, RuntimeError):
            raise RuntimeError("工作台事件账本路径不安全，服务已停止。") from None
        line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._write_lock:
            try:
                flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(self.path, flags, 0o600)
                with os.fdopen(descriptor, "a", encoding="utf-8", newline="\n") as handle:
                    handle.write(line)
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError:
                raise RuntimeError("工作台事件账本无法安全写入，服务已停止。") from None


class CanvasWorkbenchService:
    """Run M1 and M2 in separate threads while sharing one OS process."""

    def __init__(
        self,
        *,
        demo_service: Any,
        intake_service: Any,
        upload_server: Any,
        production_service: Any | None = None,
        style_service: Any | None = None,
        production_http_server: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock_ms: Callable[[], int] | None = None,
        event_ledger: WorkbenchEventLedger | None = None,
    ) -> None:
        self.demo_service = demo_service
        self.intake_service = intake_service
        self.upload_server = upload_server
        self.production_service = production_service
        self.style_service = style_service
        self.production_http_server = production_http_server
        self.sleep = sleep
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.event_ledger = event_ledger
        self.stopping = False
        self.component_status = {
            "workflow_demo": "not_started",
            "batch_intake": "not_started",
        }
        if production_service is not None:
            self.component_status["workflow_production"] = "not_started"
        if style_service is not None:
            self.component_status["style_reference_intake"] = "not_started"
        initialized_at = self.clock_ms()
        self.component_status_at = {
            name: initialized_at for name in self.component_status
        }
        self._threads: dict[str, threading.Thread] = {}
        self._status_lock = threading.Lock()
        self._stop_lock = threading.Lock()
        self._stop_started = False
        self._fatal_component: str | None = None
        if self.style_service is not None and hasattr(self.style_service, "set_status_callback"):
            self.style_service.set_status_callback(
                lambda status: self._set_component_status("style_reference_intake", status)
            )
        if self.production_http_server is not None and hasattr(
            self.production_http_server, "set_health_provider"
        ):
            self.production_http_server.set_health_provider(self.health_snapshot)

    def _set_component_status(self, name: str, status: str) -> None:
        if status not in WORKER_STATUSES:
            raise ValueError("工作台工人状态无效")
        changed = False
        with self._status_lock:
            if self.component_status[name] != status:
                self.component_status[name] = status
                self.component_status_at[name] = self.clock_ms()
                changed = True
        if changed and self.event_ledger is not None:
            self.event_ledger.record(name, status)

    def health_snapshot(self) -> tuple[bool, dict[str, dict[str, int | str]]]:
        with self._status_lock:
            fatal_component = self._fatal_component
            workers = {
                name: {
                    "status": status,
                    "lastStatusAt": self.component_status_at[name],
                }
                for name, status in self.component_status.items()
            }
        active_critical = CRITICAL_COMPONENTS & workers.keys()
        healthy = fatal_component is None and bool(active_critical) and all(
            workers[name]["status"] == "running" for name in active_critical
        )
        return healthy, workers

    def _run_component(self, name: str, service: Any) -> None:
        try:
            service.serve_forever()
        except Exception:
            print(json.dumps({"canvas_workbench": "component_stopped", "component": name}, ensure_ascii=False), flush=True)
        finally:
            unexpected = not self.stopping
            if unexpected and name in CRITICAL_COMPONENTS:
                with self._status_lock:
                    if self._fatal_component is None:
                        self._fatal_component = name
            self._set_component_status(name, "stopped")

    def start(self) -> None:
        if self._threads:
            return
        self.upload_server.start()
        if self.production_http_server is not None:
            self.production_http_server.start()
        components = [
            ("workflow_demo", self.demo_service),
            ("batch_intake", self.intake_service),
        ]
        if self.production_service is not None:
            components.append(("workflow_production", self.production_service))
        if self.style_service is not None:
            components.append(("style_reference_intake", self.style_service))
        for name, service in components:
            self._set_component_status(name, "running")
            thread = threading.Thread(
                target=self._run_component,
                args=(name, service),
                name=f"canvas-workbench-{name}",
                daemon=True,
            )
            self._threads[name] = thread
            thread.start()

    def stop(self) -> None:
        with self._stop_lock:
            if self._stop_started:
                return
            self._stop_started = True
            self.stopping = True
        self.demo_service.stopping = True
        self.intake_service.stopping = True
        if self.production_service is not None:
            self.production_service.stopping = True
        if self.style_service is not None:
            self.style_service.stopping = True
        self.upload_server.stop()
        if self.production_http_server is not None:
            self.production_http_server.stop()
        for thread in tuple(self._threads.values()):
            thread.join(timeout=5.0)

    def serve_forever(self) -> None:
        self.start()
        fatal_component: str | None = None
        try:
            while not self.stopping:
                self.sleep(0.25)
                with self._status_lock:
                    fatal_component = self._fatal_component
                if fatal_component is not None:
                    break
        finally:
            self.stop()
        if fatal_component is not None:
            raise CriticalWorkerStopped(fatal_component)


def _load_existing_local_agent_token() -> str:
    config = ic_client.load_agent_config()
    parsed = urllib.parse.urlparse(config["url"])
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise RuntimeError("canvas-agent 配置不是安全的本机地址")
    return config["token"]


def cmd_serve_canvas_workbench(
    manifest_path: Path,
    interval: float,
    *,
    test_workspace_root: Path | None = None,
) -> None:
    manifest_path = manifest_path.resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    demo_root = Path(str((manifest.get("workspace") or {}).get("root") or ""))
    state_root = batch_creator.prepare_state_root(DEFAULT_STATE_ROOT)
    token = _load_existing_local_agent_token()
    creator = batch_creator.BatchCreator(
        REPO_ROOT,
        state_root,
        test_root=test_workspace_root,
    )

    with (
        workflow_demo_service.WorkflowDemoServiceLock(demo_root),
        workflow_batch_intake_service.BatchIntakeServiceLock(state_root),
    ):
        demo_service = workflow_demo_service.WorkflowDemoService(manifest_path, interval=interval)
        intake_service = workflow_batch_intake_service.WorkflowBatchIntakeService(
            REPO_ROOT,
            state_root,
            creator=creator,
            interval=interval,
            upload_port=workflow_batch_intake_service.DEFAULT_UPLOAD_PORT,
        )
        upload_server = workflow_batch_intake_service.BatchUploadServer(
            intake_service,
            token=token,
            host=workflow_batch_intake_service.DEFAULT_UPLOAD_HOST,
            port=workflow_batch_intake_service.DEFAULT_UPLOAD_PORT,
        )
        event_ledger = WorkbenchEventLedger(state_root)
        assistant_service = canvas_readonly_assistant.CanvasReadonlyAssistant(REPO_ROOT)
        production_service = workflow_production_service.WorkflowProductionService(
            REPO_ROOT,
            interval=interval,
            diagnostic_recorder=lambda step, code: event_ledger.record_execution_failure(
                "workflow_production", step, code
            ),
        )
        style_service = workflow_style_reference_intake.WorkflowStyleReferenceService(
            REPO_ROOT,
            client=ic_client,
            interval=interval,
            upload_port=workflow_production_http_server.DEFAULT_PRODUCTION_PORT,
        )
        production_http = workflow_production_http_server.WorkflowProductionHttpServer(
            repository_root=REPO_ROOT,
            token=token,
            host=workflow_production_http_server.DEFAULT_PRODUCTION_HOST,
            port=workflow_production_http_server.DEFAULT_PRODUCTION_PORT,
            style_acceptor=style_service,
            assistant_service=assistant_service,
        )
        workbench = CanvasWorkbenchService(
            demo_service=demo_service,
            intake_service=intake_service,
            upload_server=upload_server,
            production_service=production_service,
            style_service=style_service,
            production_http_server=production_http,
            event_ledger=event_ledger,
        )
        print(
            json.dumps(
                {
                    "canvas_workbench": "started",
                    "interval": interval,
                    "upload": "http://127.0.0.1:17372",
                    "production": "http://127.0.0.1:17373",
                    "test_mode": test_workspace_root is not None,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        try:
            workbench.serve_forever()
        except KeyboardInterrupt:
            workbench.stop()
        print(json.dumps({"canvas_workbench": "stopped"}, ensure_ascii=False), flush=True)
