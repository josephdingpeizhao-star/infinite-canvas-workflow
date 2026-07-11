from __future__ import annotations

import argparse
import copy
import hashlib
import json
import mimetypes
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Iterable


DEFAULT_API_BASE = "https://cloud.comfy.org/api"
DEFAULT_API_KEY_ENV = "COMFY_CLOUD_API_KEY"
DEFAULT_PARTNER_API_KEY_FIELD = "api_key_comfy_org"
MAX_COMFY_INT_SEED = 2_147_483_647
DEFAULT_TEXT_NODE = "2"
DEFAULT_CLEAR_TEXT_NODES = "4,5,6,11,12"
DEFAULT_IMAGE_NODE = "17"
DEFAULT_SAVE_NODE = "20"
DEFAULT_GENERATOR_NODE = "1"
DEFAULT_LOCAL_ENV_FILE = ".env.local"
ROOT = Path(__file__).resolve().parents[1]
PROMPT_INTEGRITY_ALLOWED_STATUSES = {"pass", "needs_review"}


class ScriptError(Exception):
    pass


def int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def output_canvas_type(job: dict[str, Any]) -> str:
    output_type = str(job.get("output_type", "")).lower()
    job_id = str(job.get("job_id", "")).lower()
    if output_type == "detail" or job_id.startswith("detail_"):
        return "detail"
    return "main"


def validate_output_canvas_dimensions(output_type: str, job_id: str, width: int, height: int) -> None:
    if output_type == "main" and width != height:
        raise ScriptError(f"{job_id}: main output canvas must be 1:1; got {width}x{height}")
    if output_type == "detail" and width * 4 != height * 3:
        raise ScriptError(f"{job_id}: detail output canvas must be 3:4; got {width}x{height}")


def dimensions_for_job(job: dict[str, Any], args: argparse.Namespace) -> tuple[int, int]:
    job_id = str(job.get("job_id") or "<unknown>")
    output_type = output_canvas_type(job)
    width = args.width if args.width is not None else int_or_none(job.get("width"))
    height = args.height if args.height is not None else int_or_none(job.get("height"))
    if width is None or height is None:
        raise ScriptError(f"{job_id}: job manifest must provide width and height so workflow template defaults cannot leak into submission")
    validate_output_canvas_dimensions(output_type, job_id, width, height)
    return width, height


def load_local_env(env_path: Path | None = None) -> dict[str, str]:
    """Load repo-local env values without overriding explicit environment variables."""
    target = env_path or Path(__file__).resolve().parents[1] / DEFAULT_LOCAL_ENV_FILE
    if not target.exists():
        return {}

    loaded: dict[str, str] = {}
    for raw_line in target.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if not key:
            continue
        if key not in os.environ:
            os.environ[key] = value
        loaded[key] = os.environ.get(key, value)
    return loaded


def load_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as exc:
        raise ScriptError(f"file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ScriptError(f"invalid JSON in {path}: {exc}") from exc


def prompt_integrity_report_candidates(job_manifest_path: Path, job_manifest: dict[str, Any]) -> list[Path]:
    candidates: list[Path] = []
    gate = job_manifest.get("prompt_integrity_gate")
    if isinstance(gate, dict):
        for key in ("report_path", "repo_report_path"):
            value = gate.get(key)
            if isinstance(value, str) and value:
                candidates.append(Path(value))

    candidates.append(job_manifest_path.parent.parent / "qc_reports" / "final_prompt_integrity_report.json")
    product_id = job_manifest.get("product_id")
    if isinstance(product_id, str) and product_id:
        candidates.append(ROOT / "reports" / f"{product_id}_final_prompt_integrity_report.json")

    unique: list[Path] = []
    seen = set()
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def ensure_prompt_integrity_gate(job_manifest_path: Path, job_manifest: dict[str, Any]) -> dict[str, Any]:
    checked_candidates = prompt_integrity_report_candidates(job_manifest_path, job_manifest)
    for candidate in checked_candidates:
        if not candidate.is_file():
            continue
        report = load_json(candidate)
        status = str(report.get("status", ""))
        render_blocked = report.get("render_blocked") is True
        if render_blocked or status == "fail":
            raise ScriptError(
                "prompt integrity gate failed; rendering is blocked. "
                f"Report: {candidate}"
            )
        if status not in PROMPT_INTEGRITY_ALLOWED_STATUSES:
            raise ScriptError(
                f"prompt integrity gate status is not renderable: {status or 'missing'}; report: {candidate}"
            )
        expected_product_id = job_manifest.get("product_id")
        if expected_product_id and report.get("product_id") != expected_product_id:
            raise ScriptError(
                "prompt integrity gate product_id does not match the job manifest. "
                f"Report: {candidate}"
            )
        return {
            "status": status,
            "report": str(candidate),
            "render_blocked": render_blocked,
            "warning_count": report.get("warning_count"),
            "blocking_issue_count": report.get("blocking_issue_count"),
        }

    raise ScriptError(
        "missing prompt integrity gate report. Run "
        "python scripts/validate_final_prompt_integrity.py --batch-manifest <manifest> "
        "after final prompt compilation and before rendering. Checked: "
        + ", ".join(str(path) for path in checked_candidates)
    )


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def emit(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def parse_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def api_url(api_base: str, path: str) -> str:
    return f"{api_base.rstrip('/')}/{path.lstrip('/')}"


def sanitize_filename_part(value: str) -> str:
    safe = []
    for char in value:
        if char.isalnum() or char in ("-", "_"):
            safe.append(char)
        else:
            safe.append("_")
    return "".join(safe).strip("_") or "comfy_job"


def workflow_image_value(upload: dict[str, Any]) -> str:
    name = str(upload["name"])
    subfolder = str(upload.get("subfolder", "")).strip("/")
    if subfolder and not name.startswith(f"{subfolder}/"):
        return f"{subfolder}/{name}"
    return name


def deterministic_seed(value: str) -> int:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % (MAX_COMFY_INT_SEED + 1)


def ensure_api_workflow(workflow: Any, required_node_ids: Iterable[str]) -> dict[str, Any]:
    if not isinstance(workflow, dict):
        raise ScriptError("workflow template must be a JSON object")
    if isinstance(workflow.get("nodes"), list):
        raise ScriptError(
            "workflow template looks like a UI canvas export. Export ComfyUI as API format instead."
        )
    missing = [node_id for node_id in required_node_ids if node_id not in workflow]
    if missing:
        raise ScriptError(f"workflow template missing required node ids: {', '.join(missing)}")
    for node_id, node in workflow.items():
        if not isinstance(node, dict) or "class_type" not in node or "inputs" not in node:
            raise ScriptError(f"node {node_id} is not an API-format node")
    return workflow


def build_prompt_text(final_prompt_doc: dict[str, Any]) -> str:
    final_prompt = str(final_prompt_doc.get("final_prompt", "")).strip()
    if not final_prompt:
        raise ScriptError("final prompt artifact is missing final_prompt")

    negative_prompt = str(final_prompt_doc.get("negative_prompt", "")).strip()
    if negative_prompt:
        return f"{final_prompt}\n\nNegative prompt constraints:\n{negative_prompt}"
    return final_prompt


def patch_workflow(
    template: dict[str, Any],
    *,
    job: dict[str, Any],
    final_prompt_doc: dict[str, Any],
    uploaded_image_name: str,
    text_node: str,
    clear_text_nodes: list[str],
    image_node: str,
    save_node: str,
    generator_node: str,
    width: int | None,
    height: int | None,
    size: str | None,
    quality: str | None,
) -> dict[str, Any]:
    workflow = copy.deepcopy(template)
    prompt_text = build_prompt_text(final_prompt_doc)

    workflow[text_node]["inputs"]["text"] = prompt_text
    for node_id in clear_text_nodes:
        if node_id in workflow and "text" in workflow[node_id].get("inputs", {}):
            workflow[node_id]["inputs"]["text"] = ""

    workflow[image_node]["inputs"]["image"] = uploaded_image_name

    save_inputs = workflow[save_node]["inputs"]
    if "filename_prefix" in save_inputs:
        save_inputs["filename_prefix"] = sanitize_filename_part(job["job_id"])

    generator_inputs = workflow[generator_node]["inputs"]
    if "seed" in generator_inputs:
        generator_inputs["seed"] = deterministic_seed(job["job_id"])
    if width is not None and "custom_width" in generator_inputs:
        generator_inputs["custom_width"] = width
    if height is not None and "custom_height" in generator_inputs:
        generator_inputs["custom_height"] = height
    if size is not None and "size" in generator_inputs:
        generator_inputs["size"] = size
    if quality is not None and "quality" in generator_inputs:
        generator_inputs["quality"] = quality

    return workflow


def http_json(method: str, url: str, api_key: str, payload: Any | None = None) -> Any:
    data = None
    headers = {"X-API-Key": api_key}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise ScriptError(f"HTTP {exc.code} from {url}: {body}") from exc
    except urllib.error.URLError as exc:
        raise ScriptError(f"request failed for {url}: {exc}") from exc

    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ScriptError(f"non-JSON response from {url}: {raw[:200]!r}") from exc


def encode_multipart_form(fields: dict[str, str], file_field: str, file_path: Path) -> tuple[bytes, str]:
    boundary = f"----codex-comfy-{uuid.uuid4().hex}"
    chunks: list[bytes] = []

    def add_line(line: str) -> None:
        chunks.append(line.encode("utf-8"))
        chunks.append(b"\r\n")

    for name, value in fields.items():
        add_line(f"--{boundary}")
        add_line(f'Content-Disposition: form-data; name="{name}"')
        add_line("")
        add_line(value)

    mime_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    add_line(f"--{boundary}")
    add_line(
        f'Content-Disposition: form-data; name="{file_field}"; filename="{file_path.name}"'
    )
    add_line(f"Content-Type: {mime_type}")
    add_line("")
    chunks.append(file_path.read_bytes())
    chunks.append(b"\r\n")
    add_line(f"--{boundary}--")
    return b"".join(chunks), boundary


def upload_image(
    *,
    api_base: str,
    api_key: str,
    image_path: Path,
    subfolder: str,
    upload_type: str,
) -> dict[str, Any]:
    if not image_path.exists():
        raise ScriptError(f"image not found: {image_path}")

    body, boundary = encode_multipart_form(
        {
            "overwrite": "false",
            "subfolder": subfolder,
            "type": upload_type,
        },
        "image",
        image_path,
    )
    headers = {
        "X-API-Key": api_key,
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    }
    request = urllib.request.Request(
        api_url(api_base, "upload/image"),
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise ScriptError(f"image upload failed with HTTP {exc.code}: {error_body}") from exc
    except urllib.error.URLError as exc:
        raise ScriptError(f"image upload failed: {exc}") from exc

    return json.loads(raw.decode("utf-8"))


def wait_for_job(
    *,
    api_base: str,
    api_key: str,
    job_id: str,
    poll_interval: float,
    timeout_seconds: int,
) -> dict[str, Any]:
    deadline = time.time() + timeout_seconds
    terminal_statuses = {"completed", "error", "failed", "cancelled"}
    latest: dict[str, Any] = {}

    while time.time() < deadline:
        latest = http_json("GET", api_url(api_base, f"job/{job_id}/status"), api_key)
        status = str(latest.get("status", "")).lower()
        if status in terminal_statuses:
            return latest
        time.sleep(poll_interval)

    raise ScriptError(f"timed out waiting for job {job_id}; latest status: {latest}")


def iter_output_files(value: Any) -> Iterable[dict[str, str]]:
    if isinstance(value, dict):
        filename = value.get("filename") or value.get("name")
        if isinstance(filename, str):
            yield {
                "filename": filename,
                "subfolder": str(value.get("subfolder", "")),
                "type": str(value.get("type", "output")),
            }
        for child in value.values():
            yield from iter_output_files(child)
    elif isinstance(value, list):
        for item in value:
            yield from iter_output_files(item)


def download_view_file(
    *,
    api_base: str,
    api_key: str,
    file_info: dict[str, str],
    output_dir: Path,
    local_prefix: str,
    index: int,
) -> Path:
    query = {
        "filename": file_info["filename"],
        "type": file_info.get("type", "output"),
    }
    if file_info.get("subfolder"):
        query["subfolder"] = file_info["subfolder"]

    url = f"{api_url(api_base, 'view')}?{urllib.parse.urlencode(query)}"
    request = urllib.request.Request(url, headers={"X-API-Key": api_key}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            data = response.read()
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise ScriptError(f"download failed with HTTP {exc.code}: {error_body}") from exc
    except urllib.error.URLError as exc:
        raise ScriptError(f"download failed: {exc}") from exc

    suffix = Path(file_info["filename"]).suffix or ".png"
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / f"{sanitize_filename_part(local_prefix)}_{index:02d}{suffix}"
    target.write_bytes(data)
    return target


def select_jobs(job_manifest: dict[str, Any], job_ids: list[str] | None, limit: int | None) -> list[dict[str, Any]]:
    jobs = list(job_manifest.get("jobs", []))
    if job_ids:
        requested = set(job_ids)
        jobs = [job for job in jobs if job.get("job_id") in requested]
        missing = sorted(requested - {job.get("job_id") for job in jobs})
        if missing:
            raise ScriptError(f"job ids not found in manifest: {', '.join(missing)}")
    if limit is not None:
        jobs = jobs[:limit]
    if not jobs:
        raise ScriptError("no jobs selected")
    return jobs


def build_submission_manifest_path(job_manifest_path: Path, submit: bool) -> Path | None:
    if not submit:
        return None
    return job_manifest_path.with_name("comfy_cloud_submission_manifest.json")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Submit Codex final prompt jobs to Comfy Cloud using a ComfyUI API workflow template."
    )
    parser.add_argument("--workflow-template", required=True, help="Path to a ComfyUI API-format workflow JSON.")
    parser.add_argument("--job-manifest", required=True, help="Path to artifacts/comfyui_jobs/comfyui_job_manifest.json.")
    parser.add_argument("--api-base", default=DEFAULT_API_BASE, help="Comfy Cloud API base URL.")
    parser.add_argument("--api-key", default=None, help="Comfy Cloud API key. Prefer the environment variable.")
    parser.add_argument("--api-key-env", default=DEFAULT_API_KEY_ENV, help="Environment variable containing the API key.")
    parser.add_argument(
        "--partner-api-key-field",
        default=DEFAULT_PARTNER_API_KEY_FIELD,
        help="extra_data field used by Comfy partner nodes for account API authorization.",
    )
    parser.add_argument(
        "--no-partner-api-key",
        action="store_true",
        help="Do not include the Comfy account API key in prompt extra_data.",
    )
    parser.add_argument("--submit", action="store_true", help="Actually upload inputs and submit jobs. Omit for dry-run.")
    parser.add_argument("--wait", action="store_true", help="Wait for submitted jobs to reach a terminal status.")
    parser.add_argument("--download", action="store_true", help="After waiting, download output files into each job output_target_dir.")
    parser.add_argument("--job-id", action="append", help="Submit only this job id. Can be repeated.")
    parser.add_argument("--limit", type=int, default=None, help="Submit only the first N selected jobs.")
    parser.add_argument("--output-manifest", default=None, help="Where to write submission results. Defaults only when --submit is used.")
    parser.add_argument("--text-node", default=DEFAULT_TEXT_NODE, help="Text node id to receive the compiled final prompt.")
    parser.add_argument("--clear-text-nodes", default=DEFAULT_CLEAR_TEXT_NODES, help="Comma-separated text node ids to clear.")
    parser.add_argument("--image-node", default=DEFAULT_IMAGE_NODE, help="LoadImage node id for the product reference.")
    parser.add_argument("--save-node", default=DEFAULT_SAVE_NODE, help="SaveImage node id for filename_prefix.")
    parser.add_argument("--generator-node", default=DEFAULT_GENERATOR_NODE, help="Generator node id.")
    parser.add_argument("--width", type=int, default=None, help="Override manifest width for every selected job when the generator node supports custom_width.")
    parser.add_argument("--height", type=int, default=None, help="Override manifest height for every selected job when the generator node supports custom_height.")
    parser.add_argument("--size", default=None, help="Override size when the generator node supports it.")
    parser.add_argument("--quality", default=None, help="Override quality when the generator node supports it.")
    parser.add_argument("--upload-subfolder", default="", help="Client-side upload subfolder metadata.")
    parser.add_argument("--upload-type", default="input", help="Comfy upload type for product references.")
    parser.add_argument("--poll-interval", type=float, default=5.0, help="Seconds between job status checks.")
    parser.add_argument("--timeout-seconds", type=int, default=1800, help="Maximum wait time per job.")
    args = parser.parse_args()
    if (args.width is None) != (args.height is None):
        raise ScriptError("--width and --height must be supplied together when overriding manifest dimensions")

    workflow_template_path = Path(args.workflow_template)
    job_manifest_path = Path(args.job_manifest)
    template = ensure_api_workflow(
        load_json(workflow_template_path),
        [
            args.text_node,
            args.image_node,
            args.save_node,
            args.generator_node,
        ],
    )
    job_manifest = load_json(job_manifest_path)
    prompt_integrity_gate = ensure_prompt_integrity_gate(job_manifest_path, job_manifest)
    jobs = select_jobs(job_manifest, args.job_id, args.limit)

    load_local_env()
    api_key = args.api_key or os.environ.get(args.api_key_env)
    if args.submit and not api_key:
        raise ScriptError(
            f"missing API key. Set {args.api_key_env} or pass --api-key before using --submit."
        )

    clear_text_nodes = parse_csv(args.clear_text_nodes)
    records: list[dict[str, Any]] = []

    for number, job in enumerate(jobs, start=1):
        final_prompt_path = Path(job["final_prompt_path"])
        reference_path = Path(job["required_product_reference"])
        output_target_dir = Path(job["output_target_dir"])
        final_prompt_doc = load_json(final_prompt_path)
        output_width, output_height = dimensions_for_job(job, args)

        if args.submit:
            upload = upload_image(
                api_base=args.api_base,
                api_key=api_key or "",
                image_path=reference_path,
                subfolder=args.upload_subfolder,
                upload_type=args.upload_type,
            )
            uploaded_image_name = workflow_image_value(upload)
        else:
            upload = {
                "dry_run": True,
                "name": f"DRY_RUN_UPLOAD::{reference_path.name}",
                "source_path": str(reference_path),
            }
            uploaded_image_name = upload["name"]

        workflow = patch_workflow(
            template,
            job=job,
            final_prompt_doc=final_prompt_doc,
            uploaded_image_name=uploaded_image_name,
            text_node=args.text_node,
            clear_text_nodes=clear_text_nodes,
            image_node=args.image_node,
            save_node=args.save_node,
            generator_node=args.generator_node,
            width=output_width,
            height=output_height,
            size=args.size,
            quality=args.quality,
        )

        extra_data = {
            "codex_product_id": job_manifest.get("product_id"),
            "codex_job_id": job.get("job_id"),
            "codex_output_type": job.get("output_type"),
            "final_prompt_path": str(final_prompt_path),
            "required_product_reference": str(reference_path),
            "style_reference": job.get("style_reference", ""),
        }
        if args.submit and api_key and not args.no_partner_api_key and args.partner_api_key_field:
            extra_data[args.partner_api_key_field] = api_key

        payload = {
            "prompt": workflow,
            "number": number,
            "front": False,
            "extra_data": extra_data,
        }

        record: dict[str, Any] = {
            "job_id": job.get("job_id"),
            "output_type": job.get("output_type"),
            "final_prompt_path": str(final_prompt_path),
            "required_product_reference": str(reference_path),
            "style_reference": job.get("style_reference", ""),
            "output_target_dir": str(output_target_dir),
            "width": output_width,
            "height": output_height,
            "upload": upload,
            "patched_nodes": {
                "compiled_prompt_text_node": args.text_node,
                "cleared_text_nodes": clear_text_nodes,
                "load_image_node": args.image_node,
                "save_image_node": args.save_node,
                "generator_node": args.generator_node,
            },
            "submit": "skipped_dry_run",
            "request_preview": {
                "endpoint": api_url(args.api_base, "prompt"),
                "number": payload["number"],
                "front": payload["front"],
                "prompt_node_count": len(workflow),
                "image_value": uploaded_image_name,
                "filename_prefix": workflow[args.save_node]["inputs"].get("filename_prefix"),
                "seed": workflow[args.generator_node]["inputs"].get("seed"),
                "partner_api_key_field": (
                    args.partner_api_key_field
                    if args.submit and api_key and not args.no_partner_api_key and args.partner_api_key_field
                    else None
                ),
            },
        }

        if args.submit:
            response = http_json("POST", api_url(args.api_base, "prompt"), api_key or "", payload)
            cloud_job_id = response.get("prompt_id")
            record["submit"] = "submitted"
            record["submit_response"] = response
            record["cloud_job_id"] = cloud_job_id

            if args.wait and cloud_job_id:
                status = wait_for_job(
                    api_base=args.api_base,
                    api_key=api_key or "",
                    job_id=cloud_job_id,
                    poll_interval=args.poll_interval,
                    timeout_seconds=args.timeout_seconds,
                )
                record["final_status"] = status

                status_value = str(status.get("status", "")).lower()
                if args.download and status_value == "completed":
                    details = http_json("GET", api_url(args.api_base, f"jobs/{cloud_job_id}"), api_key or "")
                    output_files = list(iter_output_files(details.get("outputs", {})))
                    record["output_files"] = output_files
                    downloaded = []
                    for index, file_info in enumerate(output_files, start=1):
                        target = download_view_file(
                            api_base=args.api_base,
                            api_key=api_key or "",
                            file_info=file_info,
                            output_dir=output_target_dir,
                            local_prefix=str(job.get("job_id")),
                            index=index,
                        )
                        downloaded.append(str(target))
                    record["downloaded_files"] = downloaded

        records.append(record)

    output_manifest = Path(args.output_manifest) if args.output_manifest else build_submission_manifest_path(job_manifest_path, args.submit)
    result = {
        "status": "dry_run" if not args.submit else "submitted",
        "api_base": args.api_base,
        "workflow_template": str(workflow_template_path),
        "job_manifest": str(job_manifest_path),
        "prompt_integrity_gate": prompt_integrity_gate,
        "selected_job_count": len(records),
        "records": records,
    }

    if output_manifest is not None:
        write_json(output_manifest, result)
        result["submission_manifest_path"] = str(output_manifest)

    emit(result)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ScriptError as exc:
        emit({"status": "error", "message": str(exc)})
        raise SystemExit(2)
