from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from batch_intake_controller import BatchIntakeRequest, ConfirmedFacts, SourceImage
from batch_recycle_lock import (
    BatchOperationBusy,
    BatchOperationLock,
    BatchOperationLockUnavailable,
)
from codex_dev_downstream import ExecutorExecutionError, parse_user_confirmed_requirements
import windows_desktop


STATE_MARKER_NAME = ".canvas_batch_intake_state"
STATE_MARKER_CONTENT = "canvas-batch-intake-state-v1\n"
TEST_ROOT_MARKER_NAME = ".canvas_intake_test_root"
TEST_ROOT_MARKER_CONTENT = "canvas-intake-test-root-v1\n"
WORKSPACE_MARKER_NAME = ".canvas_batch"
FROZEN_PRODUCT_ID = "shuiping_20260712"

_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_UNSAFE_PRODUCT = re.compile(r"[<>:\"/\\|?*\x00-\x1f\s]+")
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
_DEFAULT_BATCH_LOCK_FACTORY = object()


class BatchCreationError(RuntimeError):
    """A safe, user-facing refusal from the local batch transaction."""

    def __init__(self, code: str, user_message: str) -> None:
        super().__init__(user_message)
        self.code = code
        self.user_message = user_message


@dataclass(frozen=True)
class UploadedFile:
    source_node_id: str
    path: Path
    name: str
    size: int
    mime_type: str
    sha256: str


@dataclass(frozen=True)
class CreatedAsset:
    source_node_id: str
    name: str
    relative_path: str
    byte_count: int
    expected_sha256: str
    uploaded_sha256: str
    destination_sha256: str

    def receipt_dict(self) -> dict[str, Any]:
        return {
            "source_node_id": self.source_node_id,
            "name": self.name,
            "relative_path": self.relative_path,
            "bytes": self.byte_count,
            "expected_sha256": self.expected_sha256,
            "uploaded_sha256": self.uploaded_sha256,
            "destination_sha256": self.destination_sha256,
        }


@dataclass(frozen=True)
class BatchCreationResult:
    request_id: str
    product_id: str
    image_count: int
    facts: dict[str, Any]
    workspace_root: Path
    receipt_path: Path
    manifest_path: Path
    assets: tuple[CreatedAsset, ...]

    def receipt_dict(self) -> dict[str, Any]:
        return {
            "batchId": self.product_id,
            "imageCount": self.image_count,
            "facts": dict(self.facts),
        }


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(str(path)))


def _is_unsafe_reparse(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        return bool(is_junction and is_junction())
    except OSError:
        return True


def _assert_regular_unlinked(path: Path, *, code: str, message: str) -> None:
    if _is_unsafe_reparse(path):
        raise BatchCreationError("reparse_point", "检测到目录联接或符号链接，已停止登记。")
    try:
        valid = path.is_file()
    except OSError:
        valid = False
    if not valid:
        raise BatchCreationError(code, message)


def _assert_directory_unlinked(path: Path, *, code: str, message: str) -> Path:
    raw = _absolute(path)
    if _is_unsafe_reparse(raw):
        raise BatchCreationError("reparse_point", "检测到目录联接或符号链接，已停止登记。")
    try:
        if not raw.is_dir():
            raise BatchCreationError(code, message)
        return raw.resolve(strict=True)
    except BatchCreationError:
        raise
    except (OSError, RuntimeError):
        raise BatchCreationError(code, message) from None


def _atomic_text(path: Path, content: str, *, request_id: str) -> None:
    digest = hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:16]
    temporary = path.with_name(f".{path.name}.{digest}.tmp")
    if temporary.exists():
        raise BatchCreationError("temporary_exists", "发现未完成的登记临时文件，已停止以保护现场。")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            if temporary.is_file() and not _is_unsafe_reparse(temporary):
                temporary.unlink()
        except OSError:
            pass


def _atomic_write_json(path: Path, data: Mapping[str, Any], *, request_id: str) -> None:
    _atomic_text(
        path,
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        request_id=request_id,
    )


def prepare_state_root(path: Path) -> Path:
    """Create or re-open the one marker-protected runtime root."""

    raw = _absolute(Path(path))
    if raw.exists():
        if _is_unsafe_reparse(raw) or not raw.is_dir():
            raise BatchCreationError("unsafe_state_root", "批次登记运行目录不安全，服务未启动。")
    else:
        raw.mkdir(parents=True, exist_ok=False)
    marker = raw / STATE_MARKER_NAME
    if marker.exists():
        _assert_regular_unlinked(
            marker,
            code="unsafe_state_root",
            message="批次登记运行目录的安全标记无效，服务未启动。",
        )
        try:
            if marker.read_text(encoding="utf-8") != STATE_MARKER_CONTENT:
                raise BatchCreationError("unsafe_state_root", "批次登记运行目录的安全标记无效，服务未启动。")
        except UnicodeError:
            raise BatchCreationError("unsafe_state_root", "批次登记运行目录的安全标记无效，服务未启动。") from None
    else:
        try:
            if any(raw.iterdir()):
                raise BatchCreationError("unsafe_state_root", "批次登记运行目录没有安全标记，服务未启动。")
            _atomic_text(marker, STATE_MARKER_CONTENT, request_id="state-root-marker-v1")
        except BatchCreationError:
            raise
        except OSError:
            raise BatchCreationError("unsafe_state_root", "无法建立批次登记运行目录，服务未启动。") from None
    return require_state_root(raw)


def require_state_root(path: Path) -> Path:
    raw = _assert_directory_unlinked(
        Path(path),
        code="unsafe_state_root",
        message="批次登记运行目录不可用，服务未启动。",
    )
    marker = raw / STATE_MARKER_NAME
    _assert_regular_unlinked(
        marker,
        code="unsafe_state_root",
        message="批次登记运行目录的安全标记无效，服务未启动。",
    )
    try:
        if marker.read_text(encoding="utf-8") != STATE_MARKER_CONTENT:
            raise BatchCreationError("unsafe_state_root", "批次登记运行目录的安全标记无效，服务未启动。")
    except UnicodeError:
        raise BatchCreationError("unsafe_state_root", "批次登记运行目录的安全标记无效，服务未启动。") from None
    return raw


def _require_test_root(path: Path) -> Path:
    raw = _assert_directory_unlinked(
        path,
        code="unsafe_test_root",
        message="隔离验收目录不可用，已停止登记。",
    )
    marker = raw / TEST_ROOT_MARKER_NAME
    if _is_unsafe_reparse(marker) or not marker.is_file():
        raise BatchCreationError("unsafe_test_root", "隔离验收目录缺少安全标记，已停止登记。")
    try:
        content = marker.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise BatchCreationError("unsafe_test_root", "隔离验收目录安全标记无效，已停止登记。") from None
    if content != TEST_ROOT_MARKER_CONTENT:
        raise BatchCreationError("unsafe_test_root", "隔离验收目录安全标记无效，已停止登记。")
    return raw


def _safe_filename(name: str) -> bool:
    if not isinstance(name, str) or not name or name != name.strip() or name.endswith((".", " ")):
        return False
    if name in {".", ".."} or any(character in name for character in ("/", "\\", "\x00")):
        return False
    if any(ord(character) < 32 for character in name):
        return False
    if any(character in name for character in '<>:"|?*'):
        return False
    return name.split(".", 1)[0].upper() not in _WINDOWS_RESERVED


def _clean_product_type(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value.strip())
    cleaned = _UNSAFE_PRODUCT.sub("_", normalized)
    cleaned = re.sub(r"_+", "_", cleaned).strip(" ._")
    if not cleaned or cleaned in {".", ".."} or cleaned.split(".", 1)[0].upper() in _WINDOWS_RESERVED:
        raise BatchCreationError("invalid_product_type", "产品品类无法生成安全的批次号，请修改品类后重试。")
    if len(cleaned) > 80:
        raise BatchCreationError("invalid_product_type", "产品品类过长，请缩短后重试。")
    return cleaned


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _workspace_marker(request_id: str, product_id: str) -> str:
    return json.dumps(
        {"type": "canvas-batch-v1", "request_id": request_id, "product_id": product_id},
        ensure_ascii=False,
        separators=(",", ":"),
    ) + "\n"


def _safe_remove_owned_directory(path: Path, *, marker_content: str, allowed_parent: Path) -> bool:
    raw = _absolute(path)
    parent = allowed_parent.resolve(strict=True)
    try:
        if raw.parent.resolve(strict=True) != parent or _is_unsafe_reparse(raw) or not raw.is_dir():
            return False
        marker = raw / WORKSPACE_MARKER_NAME
        if _is_unsafe_reparse(marker) or not marker.is_file():
            return False
        if marker.read_text(encoding="utf-8") != marker_content:
            return False
        shutil.rmtree(raw)
        return True
    except (OSError, RuntimeError, UnicodeError):
        return False


def _safe_remove_exact_empty_directory(path: Path, *, allowed_parent: Path) -> bool:
    """Remove only the exact empty stage whose parent was already approved."""

    raw = _absolute(path)
    try:
        parent = allowed_parent.resolve(strict=True)
        if raw.parent.resolve(strict=True) != parent or _is_unsafe_reparse(raw) or not raw.is_dir():
            return False
        if any(raw.iterdir()):
            return False
        raw.rmdir()
        return True
    except (OSError, RuntimeError):
        return False


def _publish_workspace(stage: Path, target: Path) -> None:
    if target.exists():
        raise BatchCreationError("batch_exists", "这个批次已经存在，未覆盖任何文件。")
    os.rename(stage, target)


def _publish_manifest_no_replace(temporary: Path, destination: Path) -> None:
    """Atomically publish a completed file while refusing an existing name."""

    if os.name == "nt":
        os.rename(temporary, destination)
    else:
        os.link(temporary, destination)


def _atomic_repository_manifest(path: Path, manifest: Mapping[str, Any], *, request_id: str) -> None:
    if path.exists():
        raise BatchCreationError("batch_exists", "这个批次已经存在，未覆盖任何文件。")
    digest = hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:16]
    temporary = path.with_name(f".{path.name}.{digest}.publish")
    if temporary.exists():
        raise BatchCreationError("temporary_exists", "发现未完成的清单临时文件，已停止以保护现场。")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            _publish_manifest_no_replace(temporary, path)
        except FileExistsError:
            raise BatchCreationError("batch_exists", "这个批次已经存在，未覆盖任何文件。") from None
    finally:
        try:
            if temporary.is_file() and not _is_unsafe_reparse(temporary):
                temporary.unlink()
        except OSError:
            pass


def _verify_published_workspace(target: Path, assets: Sequence[CreatedAsset]) -> None:
    """Re-hash the final, published paths before exposing the repository fact."""

    for asset in assets:
        destination = target.joinpath(*asset.relative_path.split("/"))
        _assert_regular_unlinked(
            destination,
            code="integrity_mismatch",
            message="最终工作区中的原图无法核对，已停止且未创建批次清单。",
        )
        try:
            byte_count = destination.stat().st_size
            final_hash = _sha256_file(destination)
        except OSError:
            raise BatchCreationError(
                "integrity_mismatch",
                "最终工作区中的原图无法核对，已停止且未创建批次清单。",
            ) from None
        if (
            byte_count != asset.byte_count
            or final_hash != asset.expected_sha256
            or final_hash != asset.uploaded_sha256
            or final_hash != asset.destination_sha256
        ):
            raise BatchCreationError(
                "integrity_mismatch",
                "最终工作区中的原图与磁盘原图不一致，已停止且未创建批次清单。",
            )


class BatchCreator:
    """Build one external batch as a fail-closed local transaction."""

    def __init__(
        self,
        repo_root: Path | None = None,
        state_root: Path | None = None,
        *,
        test_root: Path | None = None,
        today: Callable[[], date] = date.today,
        desktop_locator: Callable[[], Path] | None = None,
        batch_lock_factory: Callable[..., Any] | None | object = (
            _DEFAULT_BATCH_LOCK_FACTORY
        ),
        batch_lock_root: Path | None = None,
    ) -> None:
        self.repo_root = _assert_directory_unlinked(
            repo_root or Path(__file__).resolve().parents[1],
            code="invalid_repository",
            message="项目目录不可用，已停止登记。",
        )
        default_state = Path.home() / ".infinite-canvas" / "batch-intake"
        self.state_root = prepare_state_root(state_root or default_state)
        self._today = today
        actual_repo = Path(__file__).resolve().parents[1]
        self.desktop_locator = (
            desktop_locator
            if desktop_locator is not None
            else (
                windows_desktop.desktop_directory
                if self.repo_root.resolve() == actual_repo
                else None
            )
        )
        self.batch_lock_factory = (
            None
            if batch_lock_factory is _DEFAULT_BATCH_LOCK_FACTORY
            and test_root is not None
            else (
                BatchOperationLock
                if batch_lock_factory is _DEFAULT_BATCH_LOCK_FACTORY
                else batch_lock_factory
            )
        )
        self.batch_lock_root = (
            Path(batch_lock_root)
            if batch_lock_root is not None
            else self.state_root.parent / "batch-operation-locks"
        )
        self.workspace_parent = (
            _require_test_root(test_root) if test_root is not None else self._production_parent()
        )

    def _production_parent(self) -> Path:
        manifest_path = self.repo_root / "manifests" / f"{FROZEN_PRODUCT_ID}.batch_manifest.json"
        anchor_parent: Path | None = None
        if manifest_path.exists() or _is_unsafe_reparse(manifest_path):
            _assert_regular_unlinked(
                manifest_path,
                code="invalid_repository",
                message="无法核对既有批次目录，已停止登记。",
            )
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                root_text = manifest["workspace"]["root"]
            except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError):
                raise BatchCreationError("invalid_repository", "无法核对既有批次目录，已停止登记。") from None
            if manifest.get("product_id") != FROZEN_PRODUCT_ID or not isinstance(root_text, str):
                raise BatchCreationError("invalid_repository", "无法核对既有批次目录，已停止登记。")
            frozen_root = _absolute(Path(root_text))
            if frozen_root.name != FROZEN_PRODUCT_ID:
                raise BatchCreationError("invalid_repository", "无法核对既有批次目录，已停止登记。")
            anchor_parent = _assert_directory_unlinked(
                frozen_root.parent,
                code="invalid_repository",
                message="既有批次的父目录不可用，已停止登记。",
            )

        desktop_parent: Path | None = None
        if self.desktop_locator is not None:
            try:
                desktop = _absolute(Path(self.desktop_locator()))
                desktop_parent = _assert_directory_unlinked(
                    desktop / "杯类",
                    code="invalid_repository",
                    message="Windows 桌面批次目录不可用，已停止登记。",
                )
            except (OSError, RuntimeError, BatchCreationError):
                desktop_parent = None
        if anchor_parent is not None:
            if (
                desktop_parent is not None
                and anchor_parent.resolve() != desktop_parent.resolve()
            ):
                raise BatchCreationError(
                    "workspace_root_mismatch",
                    "既有批次目录与 Windows 桌面位置不一致，已安全停止。",
                )
            return anchor_parent
        if desktop_parent is not None:
            return desktop_parent
        raise BatchCreationError(
            "invalid_repository",
            "无法核对批次工作区父目录，已停止登记。",
        )

    def product_id_for(self, request: BatchIntakeRequest) -> str:
        try:
            value = self._today()
        except Exception:
            raise BatchCreationError("invalid_date", "无法读取本机日期，已停止登记。") from None
        if not isinstance(value, date):
            raise BatchCreationError("invalid_date", "无法读取本机日期，已停止登记。")
        return f"{_clean_product_type(request.facts.product_type)}_{value:%Y%m%d}"

    def _target_paths(self, product_id: str) -> tuple[Path, Path]:
        target = self.workspace_parent / product_id
        manifest = self.repo_root / "manifests" / f"{product_id}.batch_manifest.json"
        try:
            if target.resolve(strict=False).parent != self.workspace_parent.resolve(strict=True):
                raise BatchCreationError("path_outside_root", "批次路径超出批准目录，已停止登记。")
            manifests_root = (self.repo_root / "manifests").resolve(strict=True)
            if manifest.resolve(strict=False).parent != manifests_root:
                raise BatchCreationError("path_outside_root", "批次清单路径超出项目目录，已停止登记。")
        except (OSError, RuntimeError):
            raise BatchCreationError("path_outside_root", "无法验证批次路径，已停止登记。") from None
        journal = self.repo_root / "manifests" / f"{product_id}.events.jsonl"
        if product_id == FROZEN_PRODUCT_ID and (
            target.exists() or manifest.exists() or journal.exists()
        ):
            raise BatchCreationError("frozen_batch", "首批已经关账并受保护，不能重新登记或覆盖。")
        return target, manifest

    def _completed_path(self, request_id: str) -> Path:
        digest = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
        return self.state_root / "completed" / f"{digest}.json"

    def _request_already_committed(self, request_id: str) -> bool:
        if self._completed_path(request_id).is_file():
            return True
        try:
            children = list(self.workspace_parent.iterdir())
        except OSError:
            raise BatchCreationError("unsafe_workspace", "无法核对批次目录，已停止登记。") from None
        for child in children:
            if _is_unsafe_reparse(child) or not child.is_dir():
                continue
            receipt_path = child / "manifests" / "batch_intake_receipt.json"
            if _is_unsafe_reparse(receipt_path) or not receipt_path.is_file():
                continue
            try:
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if isinstance(receipt, Mapping) and receipt.get("request_id") == request_id:
                return True
        return False

    def _validate_uploads(
        self,
        request: BatchIntakeRequest,
        uploaded_files: Sequence[UploadedFile],
    ) -> tuple[tuple[SourceImage, UploadedFile, str], ...]:
        expected = {source.node_id: source for source in request.source_images}
        if not expected or len(expected) != len(request.source_images):
            raise BatchCreationError("invalid_uploads", "原图清单不完整，未创建批次。")
        uploads: dict[str, UploadedFile] = {}
        for upload in uploaded_files:
            if not isinstance(upload, UploadedFile) or upload.source_node_id in uploads:
                raise BatchCreationError("invalid_uploads", "收到的原图与画布清单不一致，未创建批次。")
            uploads[upload.source_node_id] = upload
        if set(uploads) != set(expected):
            raise BatchCreationError("invalid_uploads", "收到的原图与画布清单不一致，未创建批次。")

        names = [source.name for source in request.source_images]
        if any(not _safe_filename(name) for name in names) or len({name.casefold() for name in names}) != len(names):
            raise BatchCreationError("unsafe_filename", "原图文件名不安全或有重复，请修改后重试。")
        validated: list[tuple[SourceImage, UploadedFile, str]] = []
        for source in request.source_images:
            upload = uploads[source.node_id]
            if upload.name != source.name or upload.mime_type != source.mime_type:
                raise BatchCreationError("invalid_uploads", "收到的原图与画布清单不一致，未创建批次。")
            path = Path(upload.path)
            _assert_regular_unlinked(
                path,
                code="unsafe_source",
                message="有一张上传原图不可读取，未创建批次。",
            )
            try:
                stat_size = path.stat().st_size
                actual_hash = _sha256_file(path)
            except OSError:
                raise BatchCreationError("unsafe_source", "有一张上传原图不可读取，未创建批次。") from None
            upload_hash = upload.sha256.lower() if isinstance(upload.sha256, str) else ""
            expected_hash = source.expected_sha256.lower()
            if (
                type(upload.size) is not int
                or upload.size <= 0
                or upload.size != source.size
                or stat_size != source.size
                or not _SHA256.fullmatch(upload_hash)
                or not _SHA256.fullmatch(expected_hash)
                or upload_hash != expected_hash
                or actual_hash != expected_hash
            ):
                raise BatchCreationError(
                    "integrity_mismatch",
                    "浏览器保存的图片与磁盘原图不一致，已立即停止且未创建批次。",
                )
            validated.append((source, upload, actual_hash))
        return tuple(validated)

    def _dry_run_plan(self, request: BatchIntakeRequest, product_id: str, target: Path) -> tuple[dict[str, Any], tuple[Path, ...]]:
        facts = request.facts
        command = [
            sys.executable,
            str(self.repo_root / "scripts" / "build_batch_manifest.py"),
            "--product-id",
            product_id,
            "--product-type",
            facts.product_type,
            "--category",
            request.category,
            "--height-cm",
            str(facts.height_cm),
            "--main-count",
            str(facts.main_image_count),
            "--detail-count",
            str(facts.detail_image_count),
            "--handheld-main",
            str(facts.handheld_main),
            "--handheld-detail",
            str(facts.handheld_detail),
            "--allow-clear-water",
            str(facts.allow_clear_water).lower(),
            "--forbid-pouring-and-heating",
            str(facts.forbid_pouring_and_heating).lower(),
            "--missing-d-no-retake",
            str(facts.missing_d_no_retake).lower(),
            "--workspace-root",
            str(target),
            "--dry-run",
        ]
        if facts.length_cm is not None:
            command.extend(("--length-cm", str(facts.length_cm)))
        if facts.width_cm is not None:
            command.extend(("--width-cm", str(facts.width_cm)))
        environment = os.environ.copy()
        environment["PYTHONUTF8"] = "1"
        environment["PYTHONIOENCODING"] = "utf-8"
        try:
            completed = subprocess.run(
                command,
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
                env=environment,
            )
            result = json.loads(completed.stdout) if completed.returncode == 0 else None
        except (OSError, UnicodeError, json.JSONDecodeError):
            result = None
        if not isinstance(result, Mapping) or result.get("status") != "planned":
            raise BatchCreationError("planning_failed", "批次结构预检没有通过，未创建任何批次文件。")
        manifest = result.get("manifest_data")
        directory_values = result.get("directories")
        if not isinstance(manifest, dict) or not isinstance(directory_values, list):
            raise BatchCreationError("planning_failed", "批次结构预检结果无效，未创建任何批次文件。")
        try:
            parsed = parse_user_confirmed_requirements(manifest, self.repo_root)
        except ExecutorExecutionError:
            raise BatchCreationError("planning_failed", "批次商品信息预检没有通过，未创建任何批次文件。") from None
        planned_facts = ConfirmedFacts(
            product_type=parsed.product_type,
            length_cm=parsed.length_cm,
            width_cm=parsed.width_cm,
            height_cm=parsed.height_cm,
            main_image_count=parsed.main_image_count,
            detail_image_count=parsed.detail_image_count,
            handheld_main=parsed.handheld_main,
            handheld_detail=parsed.handheld_detail,
            allow_clear_water=parsed.allow_clear_water,
            forbid_pouring_and_heating=parsed.forbid_pouring_and_heating,
            missing_d_no_retake=parsed.missing_d_no_retake,
        )
        if planned_facts != request.facts or manifest.get("category") != request.category:
            raise BatchCreationError("planning_failed", "批次商品信息预检结果不一致，未创建任何批次文件。")
        try:
            planned_root = Path(manifest["workspace"]["root"]).resolve(strict=False)
            if planned_root != target.resolve(strict=False):
                raise BatchCreationError("planning_failed", "批次路径预检结果不一致，未创建任何批次文件。")
            directories = tuple(Path(value).resolve(strict=False) for value in directory_values if isinstance(value, str))
            if len(directories) != len(directory_values) or any(
                path != planned_root and not path.is_relative_to(planned_root) for path in directories
            ):
                raise BatchCreationError("planning_failed", "批次目录预检结果越界，未创建任何批次文件。")
        except (KeyError, TypeError, OSError, RuntimeError):
            raise BatchCreationError("planning_failed", "批次路径预检结果无效，未创建任何批次文件。") from None
        return manifest, directories

    def _build_stage(
        self,
        stage: Path,
        target: Path,
        manifest: Mapping[str, Any],
        directories: Sequence[Path],
        request: BatchIntakeRequest,
        validated: Sequence[tuple[SourceImage, UploadedFile, str]],
        product_id: str,
        marker_content: str,
    ) -> tuple[CreatedAsset, ...]:
        stage.mkdir(exist_ok=False)
        (stage / WORKSPACE_MARKER_NAME).write_text(marker_content, encoding="utf-8", newline="\n")
        target_resolved = target.resolve(strict=False)
        for planned in directories:
            relative = planned.relative_to(target_resolved)
            (stage / relative).mkdir(parents=True, exist_ok=True)
        (stage / "manifests").mkdir(parents=True, exist_ok=True)
        white_bg = stage / "inputs" / "white_bg"
        white_bg.mkdir(parents=True, exist_ok=True)

        assets: list[CreatedAsset] = []
        asset_manifest_entries: list[dict[str, Any]] = []
        for index, (source, upload, uploaded_hash) in enumerate(validated, start=1):
            destination = white_bg / source.name
            shutil.copyfile(upload.path, destination)
            destination_hash = _sha256_file(destination)
            if destination.stat().st_size != source.size or destination_hash != source.expected_sha256:
                raise BatchCreationError(
                    "integrity_mismatch",
                    "原图复制前后不一致，已立即停止且未创建批次。",
                )
            relative_path = f"inputs/white_bg/{source.name}"
            assets.append(
                CreatedAsset(
                    source_node_id=source.node_id,
                    name=source.name,
                    relative_path=relative_path,
                    byte_count=source.size,
                    expected_sha256=source.expected_sha256,
                    uploaded_sha256=uploaded_hash,
                    destination_sha256=destination_hash,
                )
            )
            asset_manifest_entries.append(
                {
                    "asset_id": f"white_bg_{index:03d}",
                    "file_path": relative_path,
                    "asset_role": "white_bg",
                    "is_single_product_white_bg": True,
                    "is_set_group_shot": False,
                    "is_style_reference": False,
                    "bound_angle_slot": "",
                    "component_id": "",
                    "notes": "",
                }
            )

        asset_manifest = json.loads(
            (self.repo_root / "manifests" / "asset_manifest.template.json").read_text(encoding="utf-8")
        )
        asset_manifest["assets"] = asset_manifest_entries
        receipt = {
            "receipt_type": "canvas_batch_intake_v1",
            "request_id": request.request_id,
            "product_id": product_id,
            "category": request.category,
            "contract_hash": request.contract_hash,
            "image_count": len(assets),
            "facts": request.facts.as_dict(),
            "source_node_ids": [source.node_id for source in request.source_images],
            "assets": [asset.receipt_dict() for asset in assets],
        }
        _atomic_write_json(
            stage / "manifests" / "asset_manifest.json",
            asset_manifest,
            request_id=request.request_id,
        )
        _atomic_write_json(
            stage / "manifests" / "batch_intake_receipt.json",
            receipt,
            request_id=request.request_id,
        )
        return tuple(assets)

    def create(
        self,
        request: BatchIntakeRequest,
        uploaded_files: Sequence[UploadedFile],
    ) -> BatchCreationResult:
        product_id = self.product_id_for(request)
        target, manifest_path = self._target_paths(product_id)
        journal_path = manifest_path.parent / f"{product_id}.events.jsonl"
        if self._request_already_committed(request.request_id):
            raise BatchCreationError("duplicate_request", "这次登记请求已经处理过，没有重复创建批次。")
        if target.exists() or manifest_path.exists() or journal_path.exists():
            raise BatchCreationError("batch_exists", "这个批次已经存在，未覆盖任何文件。")
        validated = self._validate_uploads(request, tuple(uploaded_files))
        manifest, directories = self._dry_run_plan(request, product_id, target)

        request_digest = hashlib.sha256(request.request_id.encode("utf-8")).hexdigest()[:12]
        stage = self.workspace_parent / f".{product_id}.{request_digest}.batch-intake-staging"
        if stage.exists():
            raise BatchCreationError("temporary_exists", "发现同批次未完成的临时区，已停止并保留现场。")
        marker_content = _workspace_marker(request.request_id, product_id)
        published = False
        try:
            lock = (
                self.batch_lock_factory(
                    product_id,
                    lock_root=self.batch_lock_root,
                )
                if self.batch_lock_factory is not None
                else nullcontext()
            )
            with lock:
                try:
                    if self._request_already_committed(request.request_id):
                        raise BatchCreationError("duplicate_request", "这次登记请求已经处理过，没有重复创建批次。")
                    if target.exists() or manifest_path.exists() or journal_path.exists():
                        raise BatchCreationError("batch_exists", "这个批次已经存在，未覆盖任何文件。")
                    assets = self._build_stage(
                        stage,
                        target,
                        manifest,
                        directories,
                        request,
                        validated,
                        product_id,
                        marker_content,
                    )
                    _publish_workspace(stage, target)
                    published = True
                    _verify_published_workspace(target, assets)
                    _atomic_repository_manifest(manifest_path, manifest, request_id=request.request_id)
                    completed_path = self._completed_path(request.request_id)
                    try:
                        completed_path.parent.mkdir(parents=True, exist_ok=True)
                        _atomic_write_json(
                            completed_path,
                            {"request_id": request.request_id, "product_id": product_id},
                            request_id=request.request_id,
                        )
                    except (OSError, BatchCreationError):
                        pass
                except BatchCreationError:
                    if published:
                        _safe_remove_owned_directory(
                            target,
                            marker_content=marker_content,
                            allowed_parent=self.workspace_parent,
                        )
                    else:
                        removed = _safe_remove_owned_directory(
                            stage,
                            marker_content=marker_content,
                            allowed_parent=self.workspace_parent,
                        )
                        if not removed:
                            _safe_remove_exact_empty_directory(stage, allowed_parent=self.workspace_parent)
                    raise
                except Exception:
                    if published:
                        _safe_remove_owned_directory(
                            target,
                            marker_content=marker_content,
                            allowed_parent=self.workspace_parent,
                        )
                    else:
                        removed = _safe_remove_owned_directory(
                            stage,
                            marker_content=marker_content,
                            allowed_parent=self.workspace_parent,
                        )
                        if not removed:
                            _safe_remove_exact_empty_directory(stage, allowed_parent=self.workspace_parent)
                    raise BatchCreationError(
                        "commit_failed",
                        "批次写入未完成，已撤回本次临时文件并停止。",
                    ) from None
        except BatchOperationBusy:
            raise BatchCreationError(
                "batch_busy",
                "本批次有任务正在运行，这次登记已安全停止。",
            ) from None
        except BatchOperationLockUnavailable:
            raise BatchCreationError(
                "lock_unavailable",
                "批次独占保护暂时不可用，这次登记已安全停止。",
            ) from None

        receipt_path = target / "manifests" / "batch_intake_receipt.json"
        return BatchCreationResult(
            request_id=request.request_id,
            product_id=product_id,
            image_count=len(assets),
            facts=request.facts.as_dict(),
            workspace_root=target,
            receipt_path=receipt_path,
            manifest_path=manifest_path,
            assets=assets,
        )
