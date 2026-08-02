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

from batch_intake_contract import (
    BatchIntakeContractError,
    batch_intake_contract_sha256,
)
from batch_recycle_lock import (
    BatchOperationBusy,
    BatchOperationLock,
    BatchOperationLockUnavailable,
)
from batch_recycle_service import BatchRecycleError
from category_recipes import (
    CategoryRecipeError,
    installed_category_metadata,
    load_manifest_category,
)
from codex_dev_downstream import manifest_config_ids
from executor_contract import ExecutorExecutionError
from workflow_production_projection import artifact_from_path
from workflow_qc_summary import QcSummaryInvalid, QcSummaryNotFound, build_qc_summary
from workflow_batch_acceptance import AcceptanceRejected, BatchAcceptanceService
from workflow_style_reference_intake import MAX_STYLE_UPLOAD_BYTES, StyleReferenceUploadRejected
from canvas_readonly_assistant import (
    AssistantQuestionNotFound,
    ReadonlyAssistantError,
)
from canvas_command_assistant import (
    CommandAssistantError,
    CommandDraftNotFound,
)
from project_deletion_service import ProjectDeletionError
import run_controller
from white_bg_recovery import (
    WhiteBgRecoveryError,
    archive_recompute_artifacts,
    evaluate_rebind_eligibility,
    rollback_recompute_archive,
    scan_white_bg_recovery,
)


DEFAULT_PRODUCTION_HOST = "127.0.0.1"
DEFAULT_PRODUCTION_PORT = 17373
UNIT_PRICE_USD = 0.06
MAX_ACCEPTANCE_BODY_BYTES = 64 * 1024
MAX_READONLY_ASSISTANT_BODY_BYTES = 16 * 1024
MAX_COMMAND_ASSISTANT_BODY_BYTES = 16 * 1024
MAX_BATCH_RECYCLE_BODY_BYTES = 256
MAX_REBIND_RECOMPUTE_BODY_BYTES = 256
MAX_PROJECT_DELETION_BODY_BYTES = 64 * 1024
DRAIN_CAP = 256 * 1024
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


class RebindRecomputeRejected(ProductionHttpError):
    def __init__(self, status: int, error_code: str, message: str):
        super().__init__(status, message)
        self.error_code = error_code


def _drain_unread_request_body(
    stream: Any,
    *,
    declared_length: int | None,
    consumed_length: int,
) -> int:
    if (
        type(declared_length) is not int
        or type(consumed_length) is not int
        or declared_length <= 0
        or declared_length > DRAIN_CAP
        or consumed_length < 0
    ):
        return 0
    remaining = min(max(declared_length - consumed_length, 0), DRAIN_CAP)
    drained = 0
    while drained < remaining:
        try:
            chunk = stream.read(remaining - drained)
        except Exception:
            break
        if not chunk:
            break
        drained += len(chunk)
    return drained


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
        command_assistant_service: Any | None = None,
        batch_recycle_service: Any | None = None,
        project_deletion_service: Any | None = None,
        batch_lock_root: Path | None = None,
    ):
        self.repository_root = repository_root.resolve()
        self.token = token
        self.style_acceptor = style_acceptor
        self.health_provider = health_provider
        self.assistant_service = assistant_service
        self.command_assistant_service = command_assistant_service
        self.batch_recycle_service = batch_recycle_service
        self.project_deletion_service = project_deletion_service
        self.batch_lock_root = batch_lock_root
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

    def batch_categories(self) -> dict[str, Any]:
        try:
            categories = installed_category_metadata(self.repository_root)
            contract_hash = batch_intake_contract_sha256(self.repository_root)
        except (CategoryRecipeError, BatchIntakeContractError):
            raise ProductionHttpError(503, "batch categories unavailable") from None
        return {
            "ok": True,
            "contractHash": contract_hash,
            "categories": list(categories),
        }

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
        try:
            load_manifest_category(self.repository_root, value)
        except CategoryRecipeError:
            raise ProductionHttpError(409, "category recipe unavailable") from None
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
        if source not in {"renders", "repaired"}:
            raise ProductionHttpError(404, "output not found")
        manifest, _manifest_path, workspace = self._manifest(batch_id)
        try:
            expected_ids = manifest_config_ids(manifest, self.repository_root)
        except ExecutorExecutionError:
            raise ProductionHttpError(409, "batch image counts invalid") from None
        if config_id not in expected_ids:
            raise ProductionHttpError(404, "output not found")
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
        try:
            expected_ids = manifest_config_ids(manifest, self.repository_root)
        except ExecutorExecutionError:
            raise ProductionHttpError(409, "batch image counts invalid") from None
        outputs = manifest.get("outputs") if isinstance(manifest.get("outputs"), dict) else {}
        ready: set[str] = set()
        for config_id in expected_ids:
            for key in ("renders", "repaired"):
                for root in _first_paths(outputs.get(key)):
                    target = root / f"{config_id}.png" if root.suffix.lower() != ".png" else root
                    if _inside(target, workspace) and target.is_file() and self._accepted(target):
                        ready.add(config_id)
        total_count = len(expected_ids)
        remaining = total_count - len(ready)
        return {
            "ok": True,
            "batchId": batch_id,
            "totalCount": total_count,
            "expectedConfigIds": list(expected_ids),
            "readyCount": len(ready),
            "remainingCount": remaining,
            "estimatedUnitUsd": UNIT_PRICE_USD,
            "estimatedTotalUsd": round(remaining * UNIT_PRICE_USD, 2),
            "estimatedMinutes": (30 if not ready else 0) + round(remaining * 1.8),
        }

    def rebind_recompute(self, batch_id: str) -> dict[str, Any]:
        # Resolve the batch read-only before creating/opening a lock file, then
        # resolve it again while locked so qualification uses one fresh view.
        self._manifest(batch_id)
        try:
            with BatchOperationLock(batch_id, lock_root=self.batch_lock_root):
                manifest, manifest_path, _workspace = self._manifest(batch_id)
                try:
                    scan = scan_white_bg_recovery(manifest)
                except WhiteBgRecoveryError as exc:
                    raise RebindRecomputeRejected(
                        409,
                        "recompute_state_invalid",
                        str(exc),
                    ) from None
                quote = self.quote(batch_id)
                ready_count = quote.get("readyCount")
                if type(ready_count) is not int or ready_count < 0:
                    raise RebindRecomputeRejected(
                        409,
                        "render_outputs_unavailable",
                        "无法确认本批是否已有成图，请稍后重试。",
                    )
                try:
                    eligibility = evaluate_rebind_eligibility(scan, ready_count)
                except WhiteBgRecoveryError as exc:
                    raise RebindRecomputeRejected(
                        409,
                        "recompute_state_invalid",
                        str(exc),
                    ) from None
                if not eligibility.eligible:
                    raise RebindRecomputeRejected(
                        409,
                        eligibility.code,
                        eligibility.message,
                    )
                journal_path = run_controller.journal_path(manifest_path, batch_id)
                try:
                    manifests_path = self.repository_root / "manifests"
                    manifests_root = manifests_path.resolve(strict=False)
                    journal_resolved = journal_path.resolve(strict=False)
                    if (
                        manifests_path.is_symlink()
                        or journal_path.is_symlink()
                        or not _inside(manifests_root, self.repository_root)
                        or manifests_root == self.repository_root
                        or not _inside(journal_resolved, manifests_root)
                        or journal_resolved.parent != manifests_root
                    ):
                        raise OSError("journal boundary is unsafe")
                    journal_size = journal_path.stat().st_size
                    if not journal_path.is_file():
                        raise OSError("journal is not a regular file")
                    journal_existed = True
                except FileNotFoundError:
                    journal_size = 0
                    journal_existed = False
                except (OSError, RuntimeError):
                    raise RebindRecomputeRejected(
                        503,
                        "recompute_journal_unavailable",
                        "暂时无法准备本次重排记录，请稍后重试。",
                    ) from None
                try:
                    archived = archive_recompute_artifacts(
                        manifest,
                        self.repository_root,
                    )
                except WhiteBgRecoveryError as exc:
                    raise RebindRecomputeRejected(
                        409,
                        "recompute_archive_failed",
                        str(exc),
                    ) from None
                try:
                    run_controller.append_event(
                        journal_path,
                        "white_bg_rebind_recompute",
                        missing=list(scan.missing_files),
                        remaining_count=scan.remaining_count,
                        superseded=list(archived.superseded),
                        superseded_dir=archived.superseded_dir,
                    )
                except OSError:
                    journal_restore_error: OSError | None = None
                    try:
                        if journal_existed:
                            with journal_path.open("r+b") as handle:
                                handle.truncate(journal_size)
                        else:
                            try:
                                journal_path.unlink()
                            except FileNotFoundError:
                                pass
                    except OSError as exc:
                        journal_restore_error = exc
                    try:
                        rollback_recompute_archive(
                            manifest,
                            self.repository_root,
                            archived,
                        )
                    except WhiteBgRecoveryError:
                        raise RebindRecomputeRejected(
                            500,
                            "recompute_journal_rollback_failed",
                            "重排记录写入失败，且无法自动恢复派生产物。请停止操作并检查本批。",
                        ) from None
                    if journal_restore_error is not None:
                        raise RebindRecomputeRejected(
                            500,
                            "recompute_journal_cleanup_failed",
                            "派生产物已恢复，但无法确认重排记录是否完整。请停止操作并检查本批。",
                        ) from None
                    raise RebindRecomputeRejected(
                        503,
                        "recompute_journal_failed",
                        "本次重排记录写入失败，原有派生产物已恢复。请稍后重试。",
                    ) from None
        except BatchOperationBusy:
            raise RebindRecomputeRejected(
                409,
                "batch_busy",
                "本批当前有任务正在运行，请稍后再试。",
            ) from None
        except BatchOperationLockUnavailable:
            raise RebindRecomputeRejected(
                503,
                "batch_lock_unavailable",
                "暂时无法取得批次独占保护，请稍后再试。",
            ) from None
        return {
            "ok": True,
            "batchId": batch_id,
            "message": "缺失白底图已剔除，已回到角度入库步骤。",
            "missing": list(scan.missing_files),
            "remainingCount": scan.remaining_count,
            "superseded": list(archived.superseded),
            "supersededDir": archived.superseded_dir,
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

    def command_assistant_submit(self, payload: Any) -> dict[str, Any]:
        if self.command_assistant_service is None:
            raise CommandDraftNotFound("指令助手未启动")
        if not isinstance(payload, dict) or set(payload) != {"utterance"}:
            raise CommandAssistantError("指令助手请求格式无效")
        try:
            return self.command_assistant_service.submit(payload.get("utterance"))
        except CommandAssistantError:
            raise
        except ValueError as exc:
            raise CommandAssistantError(str(exc)) from None

    def command_assistant_status(self, request_id: str) -> dict[str, Any]:
        if self.command_assistant_service is None:
            raise CommandDraftNotFound("指令助手未启动")
        try:
            return self.command_assistant_service.status(request_id)
        except CommandAssistantError:
            raise
        except KeyError:
            raise CommandDraftNotFound("命令草稿编号不存在") from None

    def batch_recycle(self, batch_id: str) -> dict[str, Any]:
        if self.batch_recycle_service is None:
            raise ProductionHttpError(404, "batch recycle unavailable")
        outcome = self.batch_recycle_service.recycle(
            batch_id,
            source_entry="workbench",
        )
        if outcome.batch_id != batch_id or outcome.status != "recycled":
            raise RuntimeError("invalid batch recycle outcome")
        return {
            "ok": True,
            "batchId": batch_id,
            "status": "recycled",
            "message": "批次已移入回收站。",
        }

    def project_deletion_preview(self, payload: Any) -> dict[str, Any]:
        if self.project_deletion_service is None:
            raise ProductionHttpError(404, "project deletion unavailable")
        if not isinstance(payload, dict) or set(payload) != {"batchIds"}:
            raise ProductionHttpError(400, "invalid project deletion preview")
        return self.project_deletion_service.preview(payload.get("batchIds"))

    def project_deletion_execute(self, payload: Any) -> dict[str, Any]:
        if self.project_deletion_service is None:
            raise ProductionHttpError(404, "project deletion unavailable")
        if not isinstance(payload, dict) or set(payload) != {
            "requestId",
            "batchIds",
        }:
            raise ProductionHttpError(400, "invalid project deletion execute")
        return self.project_deletion_service.execute(
            payload.get("requestId"),
            payload.get("batchIds"),
        )


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
        command_assistant_service: Any | None = None,
        batch_recycle_service: Any | None = None,
        project_deletion_service: Any | None = None,
        batch_lock_root: Path | None = None,
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
            command_assistant_service=command_assistant_service,
            batch_recycle_service=batch_recycle_service,
            project_deletion_service=project_deletion_service,
            batch_lock_root=batch_lock_root,
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

            def _declared_content_length(self) -> int | None:
                values = self.headers.get_all("Content-Length") or []
                if len(values) != 1:
                    return None
                try:
                    return int(values[0])
                except ValueError:
                    return None

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

            def _batch_recycle_error(
                self,
                batch_id: str,
                exc: BatchRecycleError,
                *,
                origin: str | None,
            ) -> None:
                self._assistant_response(
                    409,
                    {
                        "ok": False,
                        "error": "batch_recycle_rejected",
                        "batchId": batch_id,
                        "message": str(exc),
                    },
                    origin=origin,
                )

            def _rebind_recompute_error(
                self,
                batch_id: str,
                exc: RebindRecomputeRejected,
                *,
                origin: str | None,
            ) -> None:
                self._assistant_response(
                    exc.status,
                    {
                        "ok": False,
                        "error": exc.error_code,
                        "batchId": batch_id,
                        "message": str(exc),
                    },
                    origin=origin,
                )

            def _project_deletion_error(
                self,
                exc: ProjectDeletionError,
                *,
                origin: str | None,
            ) -> None:
                self._assistant_response(
                    409,
                    {
                        "ok": False,
                        "error": "project_deletion_rejected",
                        "batchId": exc.batch_id,
                        "message": str(exc),
                    },
                    origin=origin,
                )

            def _assistant_error(
                self,
                exc: ReadonlyAssistantError | CommandAssistantError,
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
                    if segments == ["batch-categories"]:
                        payload = json.dumps(
                            application.batch_categories(),
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
                    if len(segments) == 3 and segments[:2] == ["command-assistant", "drafts"]:
                        try:
                            snapshot = application.command_assistant_status(segments[2])
                        except CommandAssistantError as exc:
                            self._assistant_error(exc, origin=origin)
                            return
                        self._assistant_response(200, snapshot, origin=origin)
                        return
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
                declared_body_length = (
                    None
                    if self.headers.get("Transfer-Encoding")
                    else self._declared_content_length()
                )
                consumed_body_length = 0
                try:
                    origin = self._origin(required=True)
                    application.authorize(self.headers.get("x-canvas-agent-token") or "")
                    assistant_path = urllib.parse.urlsplit(self.path)
                    assistant_segments = [
                        urllib.parse.unquote(item)
                        for item in assistant_path.path.split("/")
                        if item
                    ]
                    is_readonly_assistant = assistant_segments == [
                        "readonly-assistant",
                        "questions",
                    ]
                    is_command_assistant = assistant_segments == [
                        "command-assistant",
                        "drafts",
                    ]
                    is_project_deletion_preview = (
                        assistant_segments == ["project-deletion", "preview"]
                        and not assistant_path.query
                        and not assistant_path.fragment
                    )
                    is_project_deletion_execute = (
                        assistant_segments == ["project-deletion", "execute"]
                        and not assistant_path.query
                        and not assistant_path.fragment
                    )
                    if is_project_deletion_preview or is_project_deletion_execute:
                        if self.headers.get("Transfer-Encoding"):
                            raise ProductionHttpError(
                                400, "transfer encoding rejected"
                            )
                        deletion_lengths = (
                            self.headers.get_all("Content-Length") or []
                        )
                        if len(deletion_lengths) != 1:
                            raise ProductionHttpError(411, "length required")
                        try:
                            deletion_length = int(deletion_lengths[0])
                        except ValueError:
                            raise ProductionHttpError(400, "bad length") from None
                        declared_body_length = deletion_length
                        if not 0 < deletion_length <= MAX_PROJECT_DELETION_BODY_BYTES:
                            raise ProductionHttpError(413, "body too large")
                        content_types = self.headers.get_all("Content-Type") or []
                        if len(content_types) != 1:
                            raise ProductionHttpError(415, "json required")
                        media_type = (
                            content_types[0].split(";", 1)[0].strip().lower()
                        )
                        if media_type != "application/json":
                            raise ProductionHttpError(415, "json required")
                        deletion_data = self.rfile.read(deletion_length)
                        consumed_body_length = len(deletion_data)
                        if len(deletion_data) != deletion_length:
                            raise ProductionHttpError(400, "short body")
                        try:
                            deletion_payload = json.loads(
                                deletion_data.decode("utf-8")
                            )
                        except (UnicodeError, json.JSONDecodeError):
                            raise ProductionHttpError(400, "bad json") from None
                        try:
                            deletion_result = (
                                application.project_deletion_preview(
                                    deletion_payload
                                )
                                if is_project_deletion_preview
                                else application.project_deletion_execute(
                                    deletion_payload
                                )
                            )
                        except ProjectDeletionError as exc:
                            self._project_deletion_error(exc, origin=origin)
                            return
                        self._assistant_response(
                            200,
                            deletion_result,
                            origin=origin,
                        )
                        return
                    if (
                        assistant_segments
                        and assistant_segments[0] == "project-deletion"
                    ):
                        raise ProductionHttpError(404, "not found")
                    if is_readonly_assistant or is_command_assistant:
                        if self.headers.get("Transfer-Encoding"):
                            raise ProductionHttpError(400, "transfer encoding rejected")
                        assistant_lengths = self.headers.get_all("Content-Length") or []
                        if len(assistant_lengths) != 1:
                            raise ProductionHttpError(411, "length required")
                        try:
                            assistant_length = int(assistant_lengths[0])
                        except ValueError:
                            raise ProductionHttpError(400, "bad length") from None
                        declared_body_length = assistant_length
                        assistant_limit = (
                            MAX_READONLY_ASSISTANT_BODY_BYTES
                            if is_readonly_assistant
                            else MAX_COMMAND_ASSISTANT_BODY_BYTES
                        )
                        if not 0 < assistant_length <= assistant_limit:
                            raise ProductionHttpError(413, "body too large")
                        if not (self.headers.get("Content-Type") or "").lower().startswith("application/json"):
                            raise ProductionHttpError(415, "json required")
                        assistant_data = self.rfile.read(assistant_length)
                        consumed_body_length = len(assistant_data)
                        if len(assistant_data) != assistant_length:
                            raise ProductionHttpError(400, "short body")
                        try:
                            assistant_payload = json.loads(assistant_data.decode("utf-8"))
                        except (UnicodeError, json.JSONDecodeError):
                            raise ProductionHttpError(400, "bad json") from None
                        try:
                            assistant_snapshot = (
                                application.assistant_submit(assistant_payload)
                                if is_readonly_assistant
                                else application.command_assistant_submit(
                                    assistant_payload
                                )
                            )
                        except (ReadonlyAssistantError, CommandAssistantError) as exc:
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
                    raw_recycle_parts = assistant_path.path.split("/")
                    in_batch_recycle_namespace = (
                        len(raw_recycle_parts) > 1
                        and raw_recycle_parts[0] == ""
                        and raw_recycle_parts[1] == "batch-recycle"
                    )
                    is_batch_recycle = (
                        in_batch_recycle_namespace
                        and len(raw_recycle_parts) == 3
                        and bool(raw_recycle_parts[2])
                        and not assistant_path.query
                        and not assistant_path.fragment
                    )
                    if in_batch_recycle_namespace:
                        if not is_batch_recycle:
                            raise ProductionHttpError(404, "not found")
                        batch_id = urllib.parse.unquote(raw_recycle_parts[2])
                        if (
                            Path(batch_id).name != batch_id
                            or any(
                                char in batch_id
                                for char in ("/", "\\", "\0", "\r", "\n")
                            )
                        ):
                            raise ProductionHttpError(400, "bad route")
                        if self.headers.get("Transfer-Encoding"):
                            raise ProductionHttpError(
                                400, "transfer encoding rejected"
                            )
                        recycle_lengths = (
                            self.headers.get_all("Content-Length") or []
                        )
                        if len(recycle_lengths) != 1:
                            raise ProductionHttpError(411, "length required")
                        try:
                            recycle_length = int(recycle_lengths[0])
                        except ValueError:
                            raise ProductionHttpError(400, "bad length") from None
                        declared_body_length = recycle_length
                        if not 0 < recycle_length <= MAX_BATCH_RECYCLE_BODY_BYTES:
                            raise ProductionHttpError(413, "body too large")
                        recycle_content_types = (
                            self.headers.get_all("Content-Type") or []
                        )
                        if len(recycle_content_types) != 1:
                            raise ProductionHttpError(415, "json required")
                        recycle_media_type = (
                            recycle_content_types[0]
                            .split(";", 1)[0]
                            .strip()
                            .lower()
                        )
                        if recycle_media_type != "application/json":
                            raise ProductionHttpError(415, "json required")
                        recycle_data = self.rfile.read(recycle_length)
                        consumed_body_length = len(recycle_data)
                        if len(recycle_data) != recycle_length:
                            raise ProductionHttpError(400, "short body")
                        try:
                            recycle_payload = json.loads(
                                recycle_data.decode("utf-8")
                            )
                        except (UnicodeError, json.JSONDecodeError):
                            raise ProductionHttpError(400, "bad json") from None
                        if not isinstance(recycle_payload, dict) or recycle_payload:
                            raise ProductionHttpError(
                                400, "empty object required"
                            )
                        try:
                            recycle_result = application.batch_recycle(batch_id)
                        except BatchRecycleError as exc:
                            self._batch_recycle_error(
                                batch_id,
                                exc,
                                origin=origin,
                            )
                            return
                        self._assistant_response(
                            200,
                            recycle_result,
                            origin=origin,
                        )
                        return
                    is_rebind_recompute = (
                        len(assistant_segments) == 3
                        and assistant_segments[0] == "workflow-production"
                        and assistant_segments[2] == "rebind-recompute"
                        and not assistant_path.query
                        and not assistant_path.fragment
                    )
                    if is_rebind_recompute:
                        batch_id = assistant_segments[1]
                        if (
                            not batch_id
                            or Path(batch_id).name != batch_id
                            or any(
                                char in batch_id
                                for char in ("/", "\\", "\0", "\r", "\n")
                            )
                        ):
                            raise ProductionHttpError(400, "bad route")
                        if self.headers.get("Transfer-Encoding"):
                            raise ProductionHttpError(
                                400, "transfer encoding rejected"
                            )
                        rebind_lengths = self.headers.get_all("Content-Length") or []
                        if len(rebind_lengths) != 1:
                            raise ProductionHttpError(411, "length required")
                        try:
                            rebind_length = int(rebind_lengths[0])
                        except ValueError:
                            raise ProductionHttpError(400, "bad length") from None
                        declared_body_length = rebind_length
                        if not 0 < rebind_length <= MAX_REBIND_RECOMPUTE_BODY_BYTES:
                            raise ProductionHttpError(413, "body too large")
                        rebind_content_types = self.headers.get_all("Content-Type") or []
                        if len(rebind_content_types) != 1:
                            raise ProductionHttpError(415, "json required")
                        rebind_media_type = (
                            rebind_content_types[0].split(";", 1)[0].strip().lower()
                        )
                        if rebind_media_type != "application/json":
                            raise ProductionHttpError(415, "json required")
                        rebind_data = self.rfile.read(rebind_length)
                        consumed_body_length = len(rebind_data)
                        if len(rebind_data) != rebind_length:
                            raise ProductionHttpError(400, "short body")
                        try:
                            rebind_payload = json.loads(rebind_data.decode("utf-8"))
                        except (UnicodeError, json.JSONDecodeError):
                            raise ProductionHttpError(400, "bad json") from None
                        if not isinstance(rebind_payload, dict) or rebind_payload:
                            raise ProductionHttpError(400, "empty object required")
                        try:
                            rebind_result = application.rebind_recompute(batch_id)
                        except RebindRecomputeRejected as exc:
                            self._rebind_recompute_error(
                                batch_id,
                                exc,
                                origin=origin,
                            )
                            return
                        self._assistant_response(
                            200,
                            rebind_result,
                            origin=origin,
                        )
                        return
                    if (
                        assistant_segments
                        and assistant_segments[0] == "workflow-production"
                        and "rebind-recompute" in assistant_segments
                    ):
                        raise ProductionHttpError(404, "not found")
                    if self.headers.get("Transfer-Encoding"):
                        raise ProductionHttpError(400, "transfer encoding rejected")
                    length_values = self.headers.get_all("Content-Length") or []
                    if len(length_values) != 1:
                        raise ProductionHttpError(411, "length required")
                    try:
                        length = int(length_values[0])
                    except ValueError:
                        raise ProductionHttpError(400, "bad length") from None
                    declared_body_length = length
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
                    consumed_body_length = len(data)
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
                    _drain_unread_request_body(
                        self.rfile,
                        declared_length=declared_body_length,
                        consumed_length=consumed_body_length,
                    )
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
