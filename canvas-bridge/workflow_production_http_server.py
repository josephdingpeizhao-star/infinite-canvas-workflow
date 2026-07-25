"""Loopback-only quote and real-PNG transport for M2-b."""

from __future__ import annotations

import hashlib
import hmac
import http.server
import json
import threading
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Mapping

from workflow_production_projection import artifact_from_path
from workflow_qc_summary import QcSummaryInvalid, QcSummaryNotFound, build_qc_summary
from workflow_batch_acceptance import AcceptanceRejected, BatchAcceptanceService
from workflow_style_reference_intake import MAX_STYLE_UPLOAD_BYTES, StyleReferenceUploadRejected
from canvas_readonly_assistant import (
    AssistantQuestionNotFound,
    ReadonlyAssistantError,
)


DEFAULT_PRODUCTION_HOST = "127.0.0.1"
DEFAULT_PRODUCTION_PORT = 17373
UNIT_PRICE_USD = 0.06
TOTAL_IMAGES = 14
MAX_ACCEPTANCE_BODY_BYTES = 64 * 1024
MAX_READONLY_ASSISTANT_BODY_BYTES = 16 * 1024
CONFIG_IDS = tuple([f"main_{index:02d}" for index in range(1, 7)] + [f"detail_{index:02d}" for index in range(1, 9)])
ALLOWED_ORIGINS = frozenset({"http://localhost:3000", "http://127.0.0.1:3000"})
WORKBENCH_WORKERS = (
    "workflow_demo",
    "batch_intake",
    "workflow_production",
    "style_reference_intake",
)
WORKBENCH_CRITICAL_WORKERS = frozenset({"batch_intake", "workflow_production", "style_reference_intake"})
WORKBENCH_STATUSES = frozenset({"not_started", "running", "waiting_canvas", "stopped"})


class ProductionHttpError(ValueError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(parent.resolve(strict=False))
        return True
    except ValueError:
        return False


def _first_paths(value: Any) -> tuple[Path, ...]:
    values = value if isinstance(value, list) else [value]
    return tuple(Path(item) for item in values if isinstance(item, str) and item)


class WorkflowProductionHttpApplication:
    def __init__(
        self,
        repository_root: Path,
        token: str,
        *,
        style_acceptor: Any | None = None,
        health_provider: Callable[[], tuple[bool, Mapping[str, Any]]] | None = None,
        assistant_service: Any | None = None,
    ):
        self.repository_root = repository_root.resolve()
        self.token = token
        self.style_acceptor = style_acceptor
        self.health_provider = health_provider
        self.assistant_service = assistant_service
        self.acceptance = BatchAcceptanceService(self.repository_root)
        if not token:
            raise ValueError("真实图片端点缺少本机令牌")

    def authorize(self, provided: str) -> None:
        if not hmac.compare_digest(self.token.encode("utf-8"), provided.encode("utf-8")):
            raise ProductionHttpError(401, "unauthorized")

    def set_health_provider(
        self,
        provider: Callable[[], tuple[bool, Mapping[str, Any]]],
    ) -> None:
        self.health_provider = provider

    def health(self) -> tuple[bool, dict[str, Any]]:
        try:
            healthy, raw_workers = self.health_provider() if self.health_provider else (False, {})
        except Exception:
            healthy, raw_workers = False, {}
        workers: dict[str, dict[str, int | str]] = {}
        for name in WORKBENCH_WORKERS:
            raw = raw_workers.get(name) if isinstance(raw_workers, Mapping) else None
            if not isinstance(raw, Mapping):
                continue
            status = raw.get("status")
            last_status_at = raw.get("lastStatusAt")
            if status not in WORKBENCH_STATUSES or type(last_status_at) is not int:
                continue
            workers[name] = {"status": status, "lastStatusAt": last_status_at}
        critical_running = all(
            workers.get(name, {}).get("status") == "running"
            for name in WORKBENCH_CRITICAL_WORKERS
        )
        return bool(healthy) and critical_running, {"workers": workers}

    def _manifest(self, batch_id: str) -> tuple[dict[str, Any], Path, Path]:
        if not batch_id or Path(batch_id).name != batch_id or any(char in batch_id for char in ("/", "\\", "\0")):
            raise ProductionHttpError(400, "invalid batch")
        path = self.repository_root / "manifests" / f"{batch_id}.batch_manifest.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ProductionHttpError(404, "batch not found") from None
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise ProductionHttpError(409, "batch unavailable") from None
        if not isinstance(value, dict) or value.get("product_id") != batch_id:
            raise ProductionHttpError(409, "batch mismatch")
        workspace_value = (value.get("workspace") or {}).get("root") if isinstance(value.get("workspace"), dict) else None
        if not isinstance(workspace_value, str) or not workspace_value:
            raise ProductionHttpError(409, "workspace missing")
        workspace = Path(workspace_value).resolve()
        try:
            marker = json.loads((workspace / ".canvas_batch").read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise ProductionHttpError(409, "workspace marker missing") from None
        if not isinstance(marker, dict) or marker.get("type") != "canvas-batch-v1" or marker.get("product_id") != batch_id:
            raise ProductionHttpError(409, "workspace marker mismatch")
        return value, path, workspace

    def _output(self, batch_id: str, config_id: str, source: str = "renders") -> Path:
        if config_id not in CONFIG_IDS:
            raise ProductionHttpError(404, "output not found")
        if source not in {"renders", "repaired"}:
            raise ProductionHttpError(404, "output not found")
        manifest, _manifest_path, workspace = self._manifest(batch_id)
        outputs = manifest.get("outputs") if isinstance(manifest.get("outputs"), dict) else {}
        matches: list[Path] = []
        for root in _first_paths(outputs.get(source)):
            target = root / f"{config_id}.png" if root.suffix.lower() != ".png" else root
            if not _inside(target, workspace):
                raise ProductionHttpError(409, "output boundary mismatch")
            if target.is_file() and target.stem == config_id:
                matches.append(target)
        unique = {item.resolve(): item for item in matches}
        if len(unique) != 1:
            raise ProductionHttpError(404 if not unique else 409, "output not found")
        return next(iter(unique.values()))

    @staticmethod
    def _accepted(path: Path) -> bool:
        try:
            artifact = artifact_from_path("batch", path)
        except (OSError, ValueError):
            return False
        if artifact.kind == "main":
            return artifact.width == artifact.height
        return artifact.width * 4 == artifact.height * 3

    def quote(self, batch_id: str) -> dict[str, Any]:
        manifest, _manifest_path, workspace = self._manifest(batch_id)
        outputs = manifest.get("outputs") if isinstance(manifest.get("outputs"), dict) else {}
        ready: set[str] = set()
        for config_id in CONFIG_IDS:
            for key in ("renders", "repaired"):
                for root in _first_paths(outputs.get(key)):
                    target = root / f"{config_id}.png" if root.suffix.lower() != ".png" else root
                    if _inside(target, workspace) and target.is_file() and self._accepted(target):
                        ready.add(config_id)
        remaining = TOTAL_IMAGES - len(ready)
        return {
            "ok": True,
            "batchId": batch_id,
            "totalCount": TOTAL_IMAGES,
            "readyCount": len(ready),
            "remainingCount": remaining,
            "estimatedUnitUsd": UNIT_PRICE_USD,
            "estimatedTotalUsd": round(remaining * UNIT_PRICE_USD, 2),
            "estimatedMinutes": (30 if not ready else 0) + round(remaining * 1.8),
        }

    def output_bytes(
        self,
        batch_id: str,
        config_id: str,
        source: str = "renders",
    ) -> tuple[bytes, str]:
        path = self._output(batch_id, config_id, source)
        if not self._accepted(path):
            raise ProductionHttpError(409, "output not accepted")
        data = path.read_bytes()
        return data, hashlib.sha256(data).hexdigest()

    def qc_summary(self, batch_id: str) -> dict[str, Any]:
        try:
            return build_qc_summary(self.repository_root, batch_id)
        except QcSummaryNotFound:
            raise ProductionHttpError(404, "qc summary not found") from None
        except QcSummaryInvalid:
            raise ProductionHttpError(409, "qc summary invalid") from None

    def acceptance_status(self, batch_id: str) -> dict[str, Any]:
        try:
            return self.acceptance.status(batch_id)
        except AcceptanceRejected as exc:
            raise ProductionHttpError(exc.status, "acceptance status rejected") from None

    def acceptance_closeout(self, batch_id: str, payload: Any) -> dict[str, Any]:
        try:
            return self.acceptance.close(batch_id, payload)
        except AcceptanceRejected as exc:
            raise ProductionHttpError(exc.status, "acceptance closeout rejected") from None

    def style_upload(self, batch_id: str, request_id: str, node_id: str, data: bytes) -> dict[str, Any]:
        if self.style_acceptor is None:
            raise ProductionHttpError(404, "style intake unavailable")
        try:
            outcome = self.style_acceptor.accept_upload(batch_id, request_id, node_id, data)
        except StyleReferenceUploadRejected as exc:
            raise ProductionHttpError(exc.http_status, "style upload rejected") from None
        return {"ok": True, "sha256": outcome.sha256, "completed": outcome.completed}

    def assistant_submit(self, payload: Any) -> dict[str, Any]:
        if self.assistant_service is None:
            raise AssistantQuestionNotFound("只读助手未启动")
        if not isinstance(payload, dict) or set(payload) - {"question", "history"}:
            raise ReadonlyAssistantError("只读助手请求格式无效")
        try:
            return self.assistant_service.submit(
                payload.get("question"),
                payload.get("history", []),
            )
        except ReadonlyAssistantError:
            raise
        except ValueError as exc:
            raise ReadonlyAssistantError(str(exc)) from None

    def assistant_status(self, request_id: str) -> dict[str, Any]:
        if self.assistant_service is None:
            raise AssistantQuestionNotFound("只读助手未启动")
        try:
            return self.assistant_service.status(request_id)
        except ReadonlyAssistantError:
            raise
        except KeyError:
            raise AssistantQuestionNotFound("问答编号不存在") from None


class _LocalThreadingHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class WorkflowProductionHttpServer:
    def __init__(
        self,
        *,
        repository_root: Path,
        token: str,
        host: str = DEFAULT_PRODUCTION_HOST,
        port: int = DEFAULT_PRODUCTION_PORT,
        style_acceptor: Any | None = None,
        health_provider: Callable[[], tuple[bool, Mapping[str, Any]]] | None = None,
        assistant_service: Any | None = None,
    ) -> None:
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("真实图片端点只允许本机回环地址")
        if not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError("真实图片端口无效")
        self.application = WorkflowProductionHttpApplication(
            repository_root,
            token,
            style_acceptor=style_acceptor,
            health_provider=health_provider,
            assistant_service=assistant_service,
        )
        self.host = host
        self.port = port
        self._server: _LocalThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def bound_port(self) -> int:
        return int(self._server.server_address[1]) if self._server else self.port

    def _handler_type(self):
        application = self.application

        class Handler(http.server.BaseHTTPRequestHandler):
            server_version = "CanvasProduction/1"

            def log_message(self, _format: str, *_args: Any) -> None:
                return

            def _origin(self, *, required: bool = False) -> str | None:
                values = self.headers.get_all("Origin") or []
                if len(values) > 1:
                    raise ProductionHttpError(400, "bad origin")
                origin = values[0] if values else None
                if required and origin not in ALLOWED_ORIGINS:
                    raise ProductionHttpError(403, "forbidden origin")
                if origin is not None and origin not in ALLOWED_ORIGINS:
                    raise ProductionHttpError(403, "forbidden origin")
                return origin

            def _send_cors(self, origin: str | None) -> None:
                if origin in ALLOWED_ORIGINS:
                    self.send_header("Access-Control-Allow-Origin", origin)
                    self.send_header("Vary", "Origin")

            def _error(self, status: int, *, origin: str | None = None) -> None:
                data = json.dumps({"ok": False, "error": "request_rejected"}).encode("utf-8")
                self.send_response(status)
                self.send_header("content-type", "application/json; charset=utf-8")
                self.send_header("content-length", str(len(data)))
                self._send_cors(origin)
                self.end_headers()
                self.wfile.write(data)

            def _assistant_response(
                self,
                status: int,
                body: Mapping[str, Any],
                *,
                origin: str | None,
            ) -> None:
                data = json.dumps(body, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("content-type", "application/json; charset=utf-8")
                self.send_header("cache-control", "no-store")
                self.send_header("content-length", str(len(data)))
                self._send_cors(origin)
                self.end_headers()
                self.wfile.write(data)

            def _assistant_error(
                self,
                exc: ReadonlyAssistantError,
                *,
                origin: str | None,
            ) -> None:
                self._assistant_response(
                    exc.http_status,
                    {
                        "ok": False,
                        "error": exc.error_code,
                        "message": str(exc),
                    },
                    origin=origin,
                )

            def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
                origin: str | None = None
                try:
                    origin = self._origin()
                    path = urllib.parse.urlsplit(self.path)
                    segments = [urllib.parse.unquote(item) for item in path.path.split("/") if item]
                    if segments == ["workbench-health"]:
                        healthy, body = application.health()
                        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
                        self.send_response(200 if healthy else 503)
                        self.send_header("content-type", "application/json; charset=utf-8")
                        self.send_header("cache-control", "no-store")
                        self.send_header("content-length", str(len(payload)))
                        self._send_cors(origin)
                        self.end_headers()
                        self.wfile.write(payload)
                        return
                    application.authorize(self.headers.get("x-canvas-agent-token") or "")
                    if len(segments) == 3 and segments[:2] == ["readonly-assistant", "questions"]:
                        try:
                            snapshot = application.assistant_status(segments[2])
                        except ReadonlyAssistantError as exc:
                            self._assistant_error(exc, origin=origin)
                            return
                        self._assistant_response(200, snapshot, origin=origin)
                        return
                    if len(segments) == 3 and segments[0] == "workflow-production" and segments[2] == "quote":
                        payload = json.dumps(application.quote(segments[1]), ensure_ascii=False).encode("utf-8")
                        self.send_response(200)
                        self.send_header("content-type", "application/json; charset=utf-8")
                        self.send_header("cache-control", "no-store")
                        self.send_header("content-length", str(len(payload)))
                        self._send_cors(origin)
                        self.end_headers()
                        self.wfile.write(payload)
                        return
                    if len(segments) == 3 and segments[0] == "workflow-production" and segments[2] == "qc-summary":
                        payload = json.dumps(application.qc_summary(segments[1]), ensure_ascii=False).encode("utf-8")
                        self.send_response(200)
                        self.send_header("content-type", "application/json; charset=utf-8")
                        self.send_header("cache-control", "no-store")
                        self.send_header("content-length", str(len(payload)))
                        self._send_cors(origin)
                        self.end_headers()
                        self.wfile.write(payload)
                        return
                    if len(segments) == 3 and segments[0] == "workflow-production" and segments[2] == "acceptance-closeout":
                        payload = json.dumps(application.acceptance_status(segments[1]), ensure_ascii=False).encode("utf-8")
                        self.send_response(200)
                        self.send_header("content-type", "application/json; charset=utf-8")
                        self.send_header("cache-control", "no-store")
                        self.send_header("content-length", str(len(payload)))
                        self._send_cors(origin)
                        self.end_headers()
                        self.wfile.write(payload)
                        return
                    if len(segments) == 4 and segments[0] == "workflow-production" and segments[2] == "outputs":
                        data, sha256 = application.output_bytes(segments[1], segments[3], "renders")
                        self.send_response(200)
                        self.send_header("content-type", "image/png")
                        self.send_header("cache-control", "no-store")
                        self.send_header("x-content-sha256", sha256)
                        self.send_header("Access-Control-Expose-Headers", "x-content-sha256")
                        self.send_header("content-length", str(len(data)))
                        self._send_cors(origin)
                        self.end_headers()
                        self.wfile.write(data)
                        return
                    if len(segments) == 5 and segments[0] == "workflow-production" and segments[2] == "outputs":
                        data, sha256 = application.output_bytes(segments[1], segments[4], segments[3])
                        self.send_response(200)
                        self.send_header("content-type", "image/png")
                        self.send_header("cache-control", "no-store")
                        self.send_header("x-content-sha256", sha256)
                        self.send_header("Access-Control-Expose-Headers", "x-content-sha256")
                        self.send_header("content-length", str(len(data)))
                        self._send_cors(origin)
                        self.end_headers()
                        self.wfile.write(data)
                        return
                    raise ProductionHttpError(404, "not found")
                except ProductionHttpError as exc:
                    self._error(exc.status, origin=origin)
                except (BrokenPipeError, ConnectionResetError):
                    return
                except Exception:
                    self._error(500, origin=origin)

            def do_OPTIONS(self) -> None:  # noqa: N802 - stdlib callback name
                try:
                    origin = self._origin(required=True)
                    if self.headers.get("Access-Control-Request-Method") not in {"GET", "POST"}:
                        raise ProductionHttpError(400, "bad preflight")
                    self.send_response(204)
                    self._send_cors(origin)
                    self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                    self.send_header("Access-Control-Allow-Headers", "x-canvas-agent-token, content-type")
                    self.send_header("Access-Control-Max-Age", "600")
                    self.send_header("content-length", "0")
                    self.end_headers()
                except ProductionHttpError as exc:
                    self._error(exc.status)

            def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
                origin: str | None = None
                try:
                    origin = self._origin(required=True)
                    application.authorize(self.headers.get("x-canvas-agent-token") or "")
                    assistant_path = urllib.parse.urlsplit(self.path)
                    assistant_segments = [
                        urllib.parse.unquote(item)
                        for item in assistant_path.path.split("/")
                        if item
                    ]
                    if assistant_segments == ["readonly-assistant", "questions"]:
                        if self.headers.get("Transfer-Encoding"):
                            raise ProductionHttpError(400, "transfer encoding rejected")
                        assistant_lengths = self.headers.get_all("Content-Length") or []
                        if len(assistant_lengths) != 1:
                            raise ProductionHttpError(411, "length required")
                        try:
                            assistant_length = int(assistant_lengths[0])
                        except ValueError:
                            raise ProductionHttpError(400, "bad length") from None
                        if not 0 < assistant_length <= MAX_READONLY_ASSISTANT_BODY_BYTES:
                            raise ProductionHttpError(413, "body too large")
                        if not (self.headers.get("Content-Type") or "").lower().startswith("application/json"):
                            raise ProductionHttpError(415, "json required")
                        assistant_data = self.rfile.read(assistant_length)
                        if len(assistant_data) != assistant_length:
                            raise ProductionHttpError(400, "short body")
                        try:
                            assistant_payload = json.loads(assistant_data.decode("utf-8"))
                        except (UnicodeError, json.JSONDecodeError):
                            raise ProductionHttpError(400, "bad json") from None
                        try:
                            assistant_snapshot = application.assistant_submit(
                                assistant_payload
                            )
                        except ReadonlyAssistantError as exc:
                            self._assistant_error(exc, origin=origin)
                            return
                        assistant_status = (
                            202
                            if assistant_snapshot.get("status") == "working"
                            else 200
                        )
                        self._assistant_response(
                            assistant_status,
                            assistant_snapshot,
                            origin=origin,
                        )
                        return
                    if self.headers.get("Transfer-Encoding"):
                        raise ProductionHttpError(400, "transfer encoding rejected")
                    length_values = self.headers.get_all("Content-Length") or []
                    if len(length_values) != 1:
                        raise ProductionHttpError(411, "length required")
                    try:
                        length = int(length_values[0])
                    except ValueError:
                        raise ProductionHttpError(400, "bad length") from None
                    path = urllib.parse.urlsplit(self.path)
                    segments = [urllib.parse.unquote(item) for item in path.path.split("/") if item]
                    is_acceptance = (
                        len(segments) == 3
                        and segments[0] == "workflow-production"
                        and segments[2] == "acceptance-closeout"
                    )
                    if is_acceptance:
                        if not 0 < length <= MAX_ACCEPTANCE_BODY_BYTES:
                            raise ProductionHttpError(413, "body too large")
                    else:
                        if not 0 < length <= MAX_STYLE_UPLOAD_BYTES:
                            raise ProductionHttpError(413, "body too large")
                        if len(segments) != 5 or segments[0] != "style-reference-intake" or segments[3] != "files":
                            raise ProductionHttpError(404, "not found")
                        if any(not item or any(char in item for char in ("/", "\\", "\0")) for item in segments[1:]):
                            raise ProductionHttpError(400, "bad route")
                    data = self.rfile.read(length)
                    if len(data) != length:
                        raise ProductionHttpError(400, "short body")
                    if is_acceptance:
                        if not (self.headers.get("Content-Type") or "").lower().startswith("application/json"):
                            raise ProductionHttpError(415, "json required")
                        try:
                            request_payload = json.loads(data.decode("utf-8"))
                        except (UnicodeError, json.JSONDecodeError):
                            raise ProductionHttpError(400, "bad json") from None
                        payload = json.dumps(
                            application.acceptance_closeout(segments[1], request_payload),
                            ensure_ascii=False,
                        ).encode("utf-8")
                        self.send_response(200)
                        self.send_header("content-type", "application/json; charset=utf-8")
                        self.send_header("cache-control", "no-store")
                        self.send_header("content-length", str(len(payload)))
                        self._send_cors(origin)
                        self.end_headers()
                        self.wfile.write(payload)
                        return
                    payload = json.dumps(
                        application.style_upload(segments[1], segments[2], segments[4], data),
                        ensure_ascii=False,
                    ).encode("utf-8")
                    self.send_response(200)
                    self.send_header("content-type", "application/json; charset=utf-8")
                    self.send_header("cache-control", "no-store")
                    self.send_header("content-length", str(len(payload)))
                    self._send_cors(origin)
                    self.end_headers()
                    self.wfile.write(payload)
                except ProductionHttpError as exc:
                    self._error(exc.status, origin=origin)
                except (BrokenPipeError, ConnectionResetError):
                    return
                except Exception:
                    self._error(500, origin=origin)

        return Handler

    def set_health_provider(
        self,
        provider: Callable[[], tuple[bool, Mapping[str, Any]]],
    ) -> None:
        self.application.set_health_provider(provider)

    def start(self) -> None:
        if self._server is not None:
            return
        self._server = _LocalThreadingHTTPServer((self.host, self.port), self._handler_type())
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="canvas-workflow-production-http",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        server = self._server
        thread = self._thread
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if thread:
            thread.join(timeout=5.0)
        self._server = None
        self._thread = None
