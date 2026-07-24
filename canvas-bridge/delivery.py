"""Ledger-authoritative, offline packaging for one accepted batch delivery."""

from __future__ import annotations

import hashlib
import json
import shutil
import string
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from workflow_production_projection import WorkflowProductionArtifact, artifact_from_path


CONFIG_IDS = tuple(
    [f"main_{index:02d}" for index in range(1, 7)]
    + [f"detail_{index:02d}" for index in range(1, 9)]
)
CONFIG_ID_SET = frozenset(CONFIG_IDS)
SOURCES = frozenset({"renders", "repaired"})
ACCEPTANCE_EVENT = "batch_acceptance_closed"
DELIVERY_EVENT = "delivery_packaged"
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


class DeliveryRejected(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class DeliveryResult:
    item_count: int
    source_counts: dict[str, int]
    acceptance_request_id: str
    selection_sha256: str
    zip_sha256: str
    zip_byte_count: int
    manifest_sha256: str
    manifest_markdown_sha256: str
    sidecar_sha256: str


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_path(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(parent.resolve(strict=False))
        return True
    except ValueError:
        return False


def _safe_identifier(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return ""
    if Path(value).name != value or any(char in value for char in ("/", "\\", "\0", "\r", "\n")):
        return ""
    return value


def _validated_context(manifest: Mapping[str, Any]) -> tuple[str, Path]:
    batch_id = _safe_identifier(manifest.get("batch_id"))
    product_id = _safe_identifier(manifest.get("product_id"))
    if not batch_id or batch_id != product_id:
        raise DeliveryRejected("batch_mismatch", "交付门禁未通过：批次清单与批次号不一致。")
    workspace_value = (
        (manifest.get("workspace") or {}).get("root")
        if isinstance(manifest.get("workspace"), Mapping)
        else None
    )
    if not isinstance(workspace_value, str) or not workspace_value:
        raise DeliveryRejected("workspace_invalid", "交付门禁未通过：批次工作区信息无效。")
    workspace = Path(workspace_value)
    if not workspace.is_absolute() or not workspace.is_dir():
        raise DeliveryRejected("workspace_invalid", "交付门禁未通过：批次工作区信息无效。")
    workspace = workspace.resolve()
    try:
        marker = json.loads((workspace / ".canvas_batch").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise DeliveryRejected(
            "workspace_marker_invalid",
            "交付门禁未通过：批次安全标记无效。",
        ) from None
    if (
        not isinstance(marker, dict)
        or marker.get("type") != "canvas-batch-v1"
        or marker.get("product_id") != batch_id
    ):
        raise DeliveryRejected(
            "workspace_marker_invalid",
            "交付门禁未通过：批次安全标记无效。",
        )
    return batch_id, workspace


def _read_events(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    except (OSError, UnicodeError):
        raise DeliveryRejected("journal_invalid", "交付门禁未通过：批次账本无法读取。") from None
    events: list[dict[str, Any]] = []
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            raise DeliveryRejected("journal_invalid", "交付门禁未通过：批次账本无效。") from None
        if not isinstance(event, dict):
            raise DeliveryRejected("journal_invalid", "交付门禁未通过：批次账本无效。")
        events.append(event)
    return events


def _validated_acceptance(
    events: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, str]], str, dict[str, int]]:
    if any(event.get("event") == DELIVERY_EVENT for event in events):
        raise DeliveryRejected(
            "delivery_already_recorded",
            "本批次账本已记录交付完成，不会重复打包。",
        )
    accepted = [event for event in events if event.get("event") == ACCEPTANCE_EVENT]
    if not accepted:
        raise DeliveryRejected("acceptance_missing", "交付门禁未通过：批次尚未正式关账。")
    if len(accepted) != 1:
        raise DeliveryRejected(
            "acceptance_not_unique",
            "交付门禁未通过：关账记录不唯一，需要人工核对。",
        )
    event = accepted[0]
    raw_selections = event.get("selections")
    if (
        type(event.get("selection_count")) is not int
        or event.get("selection_count") != len(CONFIG_IDS)
        or not isinstance(raw_selections, list)
        or len(raw_selections) != len(CONFIG_IDS)
        or not isinstance(event.get("request_id"), str)
        or not event.get("request_id")
        or not isinstance(event.get("ts"), str)
        or not event.get("ts")
    ):
        raise DeliveryRejected(
            "selections_invalid",
            "交付门禁未通过：关账图位不完整或无效。",
        )
    selections: list[dict[str, str]] = []
    for raw in raw_selections:
        if not isinstance(raw, dict) or set(raw) != {"config_id", "source", "sha256"}:
            raise DeliveryRejected(
                "selections_invalid",
                "交付门禁未通过：关账图位不完整或无效。",
            )
        config_id = raw.get("config_id")
        source = raw.get("source")
        sha256 = raw.get("sha256")
        if (
            config_id not in CONFIG_ID_SET
            or source not in SOURCES
            or not isinstance(sha256, str)
            or len(sha256) != 64
            or any(char not in string.hexdigits.lower() for char in sha256)
            or sha256.lower() != sha256
        ):
            raise DeliveryRejected(
                "selections_invalid",
                "交付门禁未通过：关账图位不完整或无效。",
            )
        selections.append(
            {"config_id": config_id, "source": source, "sha256": sha256}
        )
    config_ids = [item["config_id"] for item in selections]
    if len(set(config_ids)) != len(CONFIG_IDS) or set(config_ids) != CONFIG_ID_SET:
        raise DeliveryRejected(
            "selections_invalid",
            "交付门禁未通过：14 个关账图位必须齐全且不能重复。",
        )
    selections.sort(key=lambda item: CONFIG_IDS.index(item["config_id"]))
    canonical = json.dumps(
        selections,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    source_counts = {
        source: sum(item["source"] == source for item in selections)
        for source in ("renders", "repaired")
    }
    return event, selections, _sha256_bytes(canonical), source_counts


def _path_values(value: Any) -> tuple[Path, ...]:
    values = value if isinstance(value, list) else [value]
    return tuple(Path(item) for item in values if isinstance(item, str) and item)


def _resolve_artifacts(
    manifest: Mapping[str, Any],
    workspace: Path,
    selections: list[dict[str, str]],
) -> list[WorkflowProductionArtifact]:
    outputs = manifest.get("outputs")
    if not isinstance(outputs, Mapping):
        raise DeliveryRejected("outputs_invalid", "交付门禁未通过：成图目录信息无效。")
    artifacts: list[WorkflowProductionArtifact] = []
    for selection in selections:
        roots = _path_values(outputs.get(selection["source"]))
        if not roots:
            raise DeliveryRejected(
                "outputs_invalid",
                "交付门禁未通过：成图目录信息无效。",
            )
        matches: dict[Path, Path] = {}
        for root in roots:
            if not root.is_absolute() or not _inside(root, workspace):
                raise DeliveryRejected(
                    "source_outside_workspace",
                    "交付门禁未通过：成图来源超出批次工作区。",
                )
            target = root / f"{selection['config_id']}.png"
            if not _inside(target, workspace):
                raise DeliveryRejected(
                    "source_outside_workspace",
                    "交付门禁未通过：成图来源超出批次工作区。",
                )
            if target.is_file():
                matches[target.resolve()] = target
        if len(matches) != 1:
            raise DeliveryRejected(
                "selection_file_invalid",
                "交付门禁未通过：关账图片在磁盘上不存在或不唯一。",
            )
        path = next(iter(matches.values()))
        try:
            artifact = artifact_from_path(
                str(manifest.get("product_id") or ""),
                path,
                source=selection["source"],
            )
        except (OSError, ValueError):
            raise DeliveryRejected(
                "selection_file_invalid",
                "交付门禁未通过：关账图片实物无效。",
            ) from None
        if artifact.sha256 != selection["sha256"]:
            raise DeliveryRejected(
                "selection_sha_mismatch",
                "交付门禁未通过：关账图片已发生变化，未生成任何交付产物。",
            )
        artifacts.append(artifact)
    return artifacts


def _display_name(config_id: str) -> str:
    ordinal = int(config_id.rsplit("_", 1)[1])
    return f"{'主图' if config_id.startswith('main_') else '详情'} {ordinal}"


def _archive_names(batch_id: str) -> list[str]:
    return [
        f"{batch_id}/delivery_manifest.json",
        f"{batch_id}/delivery_manifest.md",
        *[f"{batch_id}/images/{config_id}.png" for config_id in CONFIG_IDS],
    ]


def _complete_delivery(
    batch_id: str,
    delivery_dir: Path,
    zip_path: Path,
    sidecar_path: Path,
) -> bool:
    try:
        if not delivery_dir.is_dir() or not zip_path.is_file() or not sidecar_path.is_file():
            return False
        top_level = {path.name for path in delivery_dir.iterdir()}
        if top_level != {"images", "delivery_manifest.json", "delivery_manifest.md"}:
            return False
        images_dir = delivery_dir / "images"
        if not images_dir.is_dir():
            return False
        image_files = {path.name for path in images_dir.iterdir() if path.is_file()}
        if image_files != {f"{config_id}.png" for config_id in CONFIG_IDS}:
            return False
        if any(not path.is_file() for path in images_dir.iterdir()):
            return False
        zip_sha256 = _sha256_path(zip_path)
        if sidecar_path.read_text(encoding="utf-8") != f"{zip_sha256}  {zip_path.name}\n":
            return False
        expected_names = _archive_names(batch_id)
        with zipfile.ZipFile(zip_path) as archive:
            if archive.namelist() != expected_names:
                return False
            for name in expected_names:
                if archive.read(name) != (delivery_dir.parent / name).read_bytes():
                    return False
        return True
    except (OSError, UnicodeError, ValueError, zipfile.BadZipFile, KeyError):
        return False


def _manifest_payload(
    batch_id: str,
    acceptance: Mapping[str, Any],
    selections: list[dict[str, str]],
    artifacts: list[WorkflowProductionArtifact],
    packaged_at: str,
) -> dict[str, Any]:
    artifact_by_id = {artifact.config_id: artifact for artifact in artifacts}
    items = []
    for selection in selections:
        artifact = artifact_by_id[selection["config_id"]]
        items.append(
            {
                "config_id": selection["config_id"],
                "display_name_zh": _display_name(selection["config_id"]),
                "source": selection["source"],
                "sha256": artifact.sha256,
                "byte_count": artifact.byte_count,
                "width": artifact.width,
                "height": artifact.height,
                "file": f"images/{selection['config_id']}.png",
            }
        )
    return {
        "schema_version": "delivery_manifest_v1",
        "batch_id": batch_id,
        "acceptance": {
            "request_id": acceptance["request_id"],
            "closed_at": acceptance["ts"],
            "selection_count": len(selections),
        },
        "packaged_at": packaged_at,
        "archive": {
            "file_name": f"{batch_id}.zip",
            "sha256_sidecar": f"../{batch_id}.zip.sha256",
            "compression": "deflate-9",
            "entry_order": _archive_names(batch_id),
        },
        "items": items,
    }


def _markdown_manifest(payload: Mapping[str, Any]) -> str:
    acceptance = payload["acceptance"]
    lines = [
        f"# {payload['batch_id']} 交付清单",
        "",
        f"- 关账请求：`{acceptance['request_id']}`",
        f"- 关账时间：`{acceptance['closed_at']}`",
        f"- 打包时间：`{payload['packaged_at']}`",
        f"- 定稿数量：{acceptance['selection_count']}",
        "",
        "| 中文名 | 配置编号 | 来源 | 尺寸 | 字节数 | SHA-256 |",
        "| --- | --- | --- | ---: | ---: | --- |",
    ]
    for item in payload["items"]:
        lines.append(
            "| {display_name_zh} | `{config_id}` | `{source}` | "
            "{width}×{height} | {byte_count} | `{sha256}` |".format(**item)
        )
    lines.extend(
        [
            "",
            f"ZIP 的 SHA-256 记录在同级 `{payload['archive']['file_name']}.sha256` 文件中。",
            "",
        ]
    )
    return "\n".join(lines)


def _write_zip_entry(archive: zipfile.ZipFile, name: str, data: bytes) -> None:
    info = zipfile.ZipInfo(filename=name, date_time=ZIP_TIMESTAMP)
    info.create_system = 3
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    archive.writestr(
        info,
        data,
        compress_type=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    )


def package_delivery(
    manifest: Mapping[str, Any],
    manifest_path: Path,
    *,
    journal_path: Path,
    request_id: str,
    packaged_at: str,
) -> DeliveryResult:
    """Create one immutable delivery directory, ZIP, and SHA sidecar."""

    if not isinstance(manifest, Mapping):
        raise DeliveryRejected("manifest_invalid", "交付门禁未通过：批次清单无效。")
    if not _safe_identifier(request_id):
        raise DeliveryRejected("request_invalid", "交付门禁未通过：交付请求无效。")
    if (
        not isinstance(packaged_at, str)
        or not packaged_at
        or any(char in packaged_at for char in ("\0", "\r", "\n"))
    ):
        raise DeliveryRejected("request_invalid", "交付门禁未通过：打包时间无效。")
    batch_id, workspace = _validated_context(manifest)
    expected_journal = manifest_path.parent / f"{batch_id}.events.jsonl"
    if journal_path.resolve(strict=False) != expected_journal.resolve(strict=False):
        raise DeliveryRejected("journal_mismatch", "交付门禁未通过：批次账本不匹配。")

    deliveries_root = workspace / "deliveries"
    root_created = not deliveries_root.exists()
    try:
        deliveries_root.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise DeliveryRejected(
            "delivery_root_invalid",
            "交付门禁未通过：交付目录无法使用。",
        ) from None
    if not deliveries_root.is_dir() or not _inside(deliveries_root, workspace):
        raise DeliveryRejected(
            "delivery_root_invalid",
            "交付门禁未通过：交付目录无法使用。",
        )
    lock_path = deliveries_root / f".{batch_id}.delivery.lock"
    try:
        lock_handle = lock_path.open("x", encoding="utf-8")
    except FileExistsError:
        raise DeliveryRejected(
            "delivery_locked",
            "交付任务正在运行或上次未正常收尾，已拒绝重复启动。",
        ) from None
    except OSError:
        raise DeliveryRejected(
            "delivery_lock_failed",
            "交付门禁未通过：无法建立排他锁。",
        ) from None

    try:
        with lock_handle:
            lock_handle.write(request_id)
        events = _read_events(journal_path)
        acceptance, selections, selection_sha256, source_counts = _validated_acceptance(events)
        delivery_dir = deliveries_root / batch_id
        zip_path = deliveries_root / f"{batch_id}.zip"
        sidecar_path = deliveries_root / f"{batch_id}.zip.sha256"
        existing = delivery_dir.exists() or zip_path.exists() or sidecar_path.exists()
        if existing:
            if _complete_delivery(batch_id, delivery_dir, zip_path, sidecar_path):
                raise DeliveryRejected(
                    "delivery_already_exists",
                    "完整交付包已经存在，不会重复打包。",
                )
            raise DeliveryRejected(
                "delivery_residue_exists",
                "发现上次中断留下的不完整交付内容；已保留现场，请人工处理。",
            )
        artifacts = _resolve_artifacts(manifest, workspace, selections)
        payload = _manifest_payload(
            batch_id,
            acceptance,
            selections,
            artifacts,
            packaged_at,
        )
        manifest_text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        markdown_text = _markdown_manifest(payload)
        try:
            images_dir = delivery_dir / "images"
            images_dir.mkdir(parents=True, exist_ok=False)
            artifact_by_id = {artifact.config_id: artifact for artifact in artifacts}
            for config_id in CONFIG_IDS:
                artifact = artifact_by_id[config_id]
                target = images_dir / f"{config_id}.png"
                shutil.copyfile(artifact.path, target)
                if _sha256_path(target) != artifact.sha256:
                    raise DeliveryRejected(
                        "copy_verification_failed",
                        "交付写入校验失败；已停止，未自动清理现场。",
                    )
            json_path = delivery_dir / "delivery_manifest.json"
            markdown_path = delivery_dir / "delivery_manifest.md"
            json_path.write_text(manifest_text, encoding="utf-8")
            markdown_path.write_text(markdown_text, encoding="utf-8")
            archive_sources = {
                f"{batch_id}/delivery_manifest.json": json_path,
                f"{batch_id}/delivery_manifest.md": markdown_path,
                **{
                    f"{batch_id}/images/{config_id}.png": images_dir / f"{config_id}.png"
                    for config_id in CONFIG_IDS
                },
            }
            with zip_path.open("x+b") as raw_zip:
                with zipfile.ZipFile(
                    raw_zip,
                    mode="w",
                    compression=zipfile.ZIP_DEFLATED,
                    compresslevel=9,
                ) as archive:
                    for name in _archive_names(batch_id):
                        _write_zip_entry(archive, name, archive_sources[name].read_bytes())
            zip_sha256 = _sha256_path(zip_path)
            sidecar_path.write_text(
                f"{zip_sha256}  {zip_path.name}\n",
                encoding="utf-8",
            )
        except DeliveryRejected:
            raise
        except (OSError, UnicodeError, ValueError, zipfile.BadZipFile):
            raise DeliveryRejected(
                "delivery_write_failed",
                "交付写入未完成；已保留现场，请人工处理。",
            ) from None
        return DeliveryResult(
            item_count=len(artifacts),
            source_counts=source_counts,
            acceptance_request_id=str(acceptance["request_id"]),
            selection_sha256=selection_sha256,
            zip_sha256=zip_sha256,
            zip_byte_count=zip_path.stat().st_size,
            manifest_sha256=_sha256_path(delivery_dir / "delivery_manifest.json"),
            manifest_markdown_sha256=_sha256_path(delivery_dir / "delivery_manifest.md"),
            sidecar_sha256=_sha256_path(sidecar_path),
        )
    finally:
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass
        if root_created:
            try:
                deliveries_root.rmdir()
            except OSError:
                pass
