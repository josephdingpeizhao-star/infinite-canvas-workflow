"""Composite local service that isolates the existing M1 demo from M2 intake."""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable

import batch_creator
import ic_client
import workflow_batch_intake_service
import workflow_demo_service


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE_ROOT = Path.home() / ".infinite-canvas" / "batch-intake"


class CanvasWorkbenchService:
    """Run M1 and M2 in separate threads while sharing one OS process."""

    def __init__(
        self,
        *,
        demo_service: Any,
        intake_service: Any,
        upload_server: Any,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.demo_service = demo_service
        self.intake_service = intake_service
        self.upload_server = upload_server
        self.sleep = sleep
        self.stopping = False
        self.component_status = {"workflow_demo": "not_started", "batch_intake": "not_started"}
        self._threads: dict[str, threading.Thread] = {}
        self._status_lock = threading.Lock()

    def _run_component(self, name: str, service: Any) -> None:
        try:
            service.serve_forever()
        except Exception:
            print(json.dumps({"canvas_workbench": "component_stopped", "component": name}, ensure_ascii=False), flush=True)
        finally:
            with self._status_lock:
                self.component_status[name] = "stopped"
            if name == "batch_intake" and not self.stopping:
                self.upload_server.stop()

    def start(self) -> None:
        if self._threads:
            return
        self.upload_server.start()
        for name, service in (
            ("workflow_demo", self.demo_service),
            ("batch_intake", self.intake_service),
        ):
            with self._status_lock:
                self.component_status[name] = "running"
            thread = threading.Thread(
                target=self._run_component,
                args=(name, service),
                name=f"canvas-workbench-{name}",
                daemon=True,
            )
            self._threads[name] = thread
            thread.start()

    def stop(self) -> None:
        if self.stopping:
            return
        self.stopping = True
        self.demo_service.stopping = True
        self.intake_service.stopping = True
        self.upload_server.stop()
        for thread in tuple(self._threads.values()):
            thread.join(timeout=5.0)

    def serve_forever(self) -> None:
        self.start()
        try:
            while not self.stopping:
                self.sleep(0.25)
        finally:
            self.stop()


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
        workbench = CanvasWorkbenchService(
            demo_service=demo_service,
            intake_service=intake_service,
            upload_server=upload_server,
        )
        print(
            json.dumps(
                {
                    "canvas_workbench": "started",
                    "interval": interval,
                    "upload": "http://127.0.0.1:17372",
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
