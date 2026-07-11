from __future__ import annotations

import argparse
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from submit_comfy_cloud_jobs import (
    DEFAULT_API_BASE,
    DEFAULT_API_KEY_ENV,
    DEFAULT_CLEAR_TEXT_NODES,
    DEFAULT_GENERATOR_NODE,
    DEFAULT_IMAGE_NODE,
    DEFAULT_PARTNER_API_KEY_FIELD,
    DEFAULT_SAVE_NODE,
    DEFAULT_TEXT_NODE,
    ScriptError,
    api_url,
    download_view_file,
    ensure_prompt_integrity_gate,
    ensure_api_workflow,
    http_json,
    iter_output_files,
    load_local_env,
    load_json,
    parse_csv,
    patch_workflow,
    select_jobs,
    upload_image,
    workflow_image_value,
    write_json,
)


SUCCESS_STATUSES = {"completed", "success"}
TERMINAL_STATUSES = {*SUCCESS_STATUSES, "error", "failed", "cancelled"}
RECOMMENDED_SMALL_BATCH_CONCURRENCY = 3
DEFAULT_MAIN_CANVAS = (1440, 1440)
DEFAULT_DETAIL_CANVAS = (1440, 1920)
CONCURRENCY_SAFETY_NOTES = [
    "Default concurrency remains 1 to preserve the legacy serial execution path.",
    "For small Comfy Cloud batches, start with --concurrency 3 or 4 after confirming account quota and workflow-template stability.",
    "Job seeds remain derived from job_id, so parallel execution does not change deterministic seed assignment.",
    "Records are sorted by selection_number before every manifest write to keep manifest order stable.",
    "Downloaded files use the job_id-derived local prefix, avoiding cross-job filename collisions.",
]


def emit(event: str, **payload: Any) -> None:
    print(json.dumps({"event": event, **payload}, ensure_ascii=False), flush=True)


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


def robust_http_json(
    method: str,
    url: str,
    api_key: str,
    payload: Any | None = None,
    *,
    attempts: int,
    delay_seconds: float,
) -> Any:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return http_json(method, url, api_key, payload)
        except ScriptError as exc:
            last_error = exc
            if attempt == attempts:
                break
            emit(
                "http_retry",
                method=method,
                url=url,
                attempt=attempt,
                attempts=attempts,
                message=str(exc),
            )
            time.sleep(delay_seconds)
    raise ScriptError(str(last_error))


def upload_image_robust(
    *,
    args: argparse.Namespace,
    api_key: str,
    image_path: Path,
) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(1, args.upload_attempts + 1):
        try:
            return upload_image(
                api_base=args.api_base,
                api_key=api_key,
                image_path=image_path,
                subfolder=args.upload_subfolder,
                upload_type=args.upload_type,
            )
        except ScriptError as exc:
            last_error = exc
            if attempt == args.upload_attempts:
                break
            emit(
                "upload_retry",
                image_path=str(image_path),
                attempt=attempt,
                attempts=args.upload_attempts,
                message=str(exc),
            )
            time.sleep(10.0)
    raise ScriptError(str(last_error))


def download_view_file_robust(
    *,
    args: argparse.Namespace,
    api_key: str,
    file_info: dict[str, str],
    output_dir: Path,
    local_prefix: str,
    index: int,
) -> Path:
    last_error: Exception | None = None
    for attempt in range(1, args.download_attempts + 1):
        try:
            return download_view_file(
                api_base=args.api_base,
                api_key=api_key,
                file_info=file_info,
                output_dir=output_dir,
                local_prefix=local_prefix,
                index=index,
            )
        except ScriptError as exc:
            last_error = exc
            if attempt == args.download_attempts:
                break
            emit(
                "download_retry",
                local_prefix=local_prefix,
                index=index,
                attempt=attempt,
                attempts=args.download_attempts,
                message=str(exc),
            )
            time.sleep(10.0)
    raise ScriptError(str(last_error))


def wait_for_job_robust(
    *,
    api_base: str,
    api_key: str,
    cloud_job_id: str,
    poll_interval: float,
    timeout_seconds: int,
    status_attempts: int,
) -> dict[str, Any]:
    deadline = time.time() + timeout_seconds
    latest: dict[str, Any] = {}
    status_url = api_url(api_base, f"job/{cloud_job_id}/status")

    while time.time() < deadline:
        latest = robust_http_json(
            "GET",
            status_url,
            api_key,
            attempts=status_attempts,
            delay_seconds=min(10.0, poll_interval),
        )
        status = str(latest.get("status", "")).lower()
        emit("job_status", cloud_job_id=cloud_job_id, status=status, latest=latest)
        if status in TERMINAL_STATUSES:
            return latest
        time.sleep(poll_interval)

    raise ScriptError(f"timed out waiting for job {cloud_job_id}; latest status: {latest}")


def dimensions_for_job(job: dict[str, Any], args: argparse.Namespace) -> tuple[int | None, int | None]:
    output_type = output_canvas_type(job)
    job_id = str(job.get("job_id") or "<unknown>")
    width = int_or_none(job.get("width"))
    height = int_or_none(job.get("height"))
    if width is None and height is None:
        if output_type == "detail":
            width, height = args.detail_width, args.detail_height
        else:
            width, height = args.main_width, args.main_height
    elif width is None or height is None:
        raise ScriptError(f"{job_id}: job manifest must provide both width and height")
    validate_output_canvas_dimensions(output_type, job_id, width, height)
    return width, height


def write_running_manifest(path: Path, result: dict[str, Any]) -> None:
    result["updated_at_epoch"] = time.time()
    write_json(path, result)


def run_job(
    *,
    args: argparse.Namespace,
    template: dict[str, Any],
    job_manifest: dict[str, Any],
    job: dict[str, Any],
    number: int,
    clear_text_nodes: list[str],
    api_key: str,
) -> dict[str, Any]:
    job_id = str(job.get("job_id"))
    final_prompt_path = Path(job["final_prompt_path"])
    reference_path = Path(job["required_product_reference"])
    output_target_dir = Path(job["output_target_dir"])
    width, height = dimensions_for_job(job, args)

    emit("job_start", job_id=job_id, output_type=job.get("output_type"), width=width, height=height)

    final_prompt_doc = load_json(final_prompt_path)
    upload = upload_image_robust(
        args=args,
        api_key=api_key,
        image_path=reference_path,
    )
    uploaded_image_name = workflow_image_value(upload)
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
        width=width,
        height=height,
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
    if not args.no_partner_api_key and args.partner_api_key_field:
        extra_data[args.partner_api_key_field] = api_key

    payload = {
        "prompt": workflow,
        "number": number,
        "front": False,
        "extra_data": extra_data,
    }

    record: dict[str, Any] = {
        "job_id": job_id,
        "output_type": job.get("output_type"),
        "final_prompt_path": str(final_prompt_path),
        "required_product_reference": str(reference_path),
        "output_target_dir": str(output_target_dir),
        "width": width,
        "height": height,
        "quality": args.quality,
        "upload": upload,
        "submit": "pending",
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
                if not args.no_partner_api_key and args.partner_api_key_field
                else None
            ),
        },
    }

    response = robust_http_json(
        "POST",
        api_url(args.api_base, "prompt"),
        api_key,
        payload,
        attempts=args.submit_attempts,
        delay_seconds=10.0,
    )
    cloud_job_id = response.get("prompt_id")
    record["submit"] = "submitted"
    record["submit_response"] = response
    record["cloud_job_id"] = cloud_job_id
    emit("job_submitted", job_id=job_id, cloud_job_id=cloud_job_id)

    if not cloud_job_id:
        raise ScriptError(f"{job_id}: submit response did not include prompt_id")

    status = wait_for_job_robust(
        api_base=args.api_base,
        api_key=api_key,
        cloud_job_id=str(cloud_job_id),
        poll_interval=args.poll_interval,
        timeout_seconds=args.timeout_seconds,
        status_attempts=args.status_attempts,
    )
    record["final_status"] = status
    status_value = str(status.get("status", "")).lower()
    if status_value not in SUCCESS_STATUSES:
        record["downloaded_files"] = []
        record["result"] = "failed"
        emit("job_failed", job_id=job_id, cloud_job_id=cloud_job_id, final_status=status)
        return record

    details = robust_http_json(
        "GET",
        api_url(args.api_base, f"jobs/{cloud_job_id}"),
        api_key,
        attempts=args.download_attempts,
        delay_seconds=10.0,
    )
    output_files = list(iter_output_files(details.get("outputs", {})))
    record["output_files"] = output_files
    downloaded: list[str] = []
    for index, file_info in enumerate(output_files, start=1):
        target = download_view_file_robust(
            args=args,
            api_key=api_key,
            file_info=file_info,
            output_dir=output_target_dir,
            local_prefix=job_id,
            index=index,
        )
        downloaded.append(str(target))

    record["downloaded_files"] = downloaded
    record["result"] = "completed" if downloaded else "completed_no_downloads"
    emit("job_downloaded", job_id=job_id, cloud_job_id=cloud_job_id, downloaded_files=downloaded)
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Comfy Cloud jobs with rolling manifests.")
    parser.add_argument("--workflow-template", required=True)
    parser.add_argument("--job-manifest", required=True)
    parser.add_argument("--output-manifest", required=True)
    parser.add_argument("--api-base", default=DEFAULT_API_BASE)
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--api-key-env", default=DEFAULT_API_KEY_ENV)
    parser.add_argument("--partner-api-key-field", default=DEFAULT_PARTNER_API_KEY_FIELD)
    parser.add_argument("--no-partner-api-key", action="store_true")
    parser.add_argument("--job-id", action="append")
    parser.add_argument("--text-node", default=DEFAULT_TEXT_NODE)
    parser.add_argument("--clear-text-nodes", default=DEFAULT_CLEAR_TEXT_NODES)
    parser.add_argument("--image-node", default=DEFAULT_IMAGE_NODE)
    parser.add_argument("--save-node", default=DEFAULT_SAVE_NODE)
    parser.add_argument("--generator-node", default=DEFAULT_GENERATOR_NODE)
    parser.add_argument("--main-width", type=int, default=DEFAULT_MAIN_CANVAS[0])
    parser.add_argument("--main-height", type=int, default=DEFAULT_MAIN_CANVAS[1])
    parser.add_argument("--detail-width", type=int, default=DEFAULT_DETAIL_CANVAS[0])
    parser.add_argument("--detail-height", type=int, default=DEFAULT_DETAIL_CANVAS[1])
    parser.add_argument("--size", default=None)
    parser.add_argument("--quality", default="low")
    parser.add_argument("--upload-subfolder", default="")
    parser.add_argument("--upload-type", default="input")
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--upload-attempts", type=int, default=4)
    parser.add_argument("--submit-attempts", type=int, default=1)
    parser.add_argument("--status-attempts", type=int, default=4)
    parser.add_argument("--download-attempts", type=int, default=4)
    parser.add_argument("--concurrency", type=int, default=1, help="Maximum jobs to submit and poll in parallel. Default 1 preserves serial behavior.")
    parser.add_argument(
        "--recommended-concurrency",
        type=int,
        default=RECOMMENDED_SMALL_BATCH_CONCURRENCY,
        help="Documented starting point for small Comfy Cloud batches; does not change --concurrency.",
    )
    args = parser.parse_args()
    if args.concurrency < 1:
        raise ScriptError("--concurrency must be >= 1")
    if args.recommended_concurrency < 1:
        raise ScriptError("--recommended-concurrency must be >= 1")

    load_local_env()
    api_key = args.api_key or os.environ.get(args.api_key_env)
    if not api_key:
        raise ScriptError(f"missing API key. Set {args.api_key_env} or pass --api-key.")

    workflow_template_path = Path(args.workflow_template)
    job_manifest_path = Path(args.job_manifest)
    output_manifest_path = Path(args.output_manifest)
    template = ensure_api_workflow(
        load_json(workflow_template_path),
        [args.text_node, args.image_node, args.save_node, args.generator_node],
    )
    job_manifest = load_json(job_manifest_path)
    prompt_integrity_gate = ensure_prompt_integrity_gate(job_manifest_path, job_manifest)
    jobs = select_jobs(job_manifest, args.job_id, None)
    clear_text_nodes = parse_csv(args.clear_text_nodes)

    result: dict[str, Any] = {
        "status": "running",
        "api_base": args.api_base,
        "workflow_template": str(workflow_template_path),
        "job_manifest": str(job_manifest_path),
        "prompt_integrity_gate": prompt_integrity_gate,
        "selected_job_count": len(jobs),
        "requested_concurrency": args.concurrency,
        "effective_concurrency": min(args.concurrency, len(jobs)),
        "recommended_concurrency": args.recommended_concurrency,
        "concurrency_safety_notes": CONCURRENCY_SAFETY_NOTES,
        "records": [],
    }
    write_running_manifest(output_manifest_path, result)

    failures = 0
    job_items = list(enumerate(jobs, start=1))

    def execute_job(number: int, job: dict[str, Any]) -> tuple[int, dict[str, Any], bool]:
        try:
            record = run_job(
                args=args,
                template=template,
                job_manifest=job_manifest,
                job=job,
                number=number,
                clear_text_nodes=clear_text_nodes,
                api_key=api_key,
            )
        except Exception as exc:
            record = {
                "job_id": job.get("job_id"),
                "output_type": job.get("output_type"),
                "result": "exception",
                "error": str(exc),
            }
            emit("job_exception", job_id=job.get("job_id"), message=str(exc))
        record["selection_number"] = number
        return number, record, record.get("result") != "completed"

    def update_result(processed_count: int, record: dict[str, Any], failed: bool) -> None:
        nonlocal failures
        if failed:
            failures += 1
        result["records"].append(record)
        result["records"].sort(key=lambda item: item.get("selection_number", 999999))
        completed = sum(1 for item in result["records"] if item.get("result") == "completed")
        result["completed_count"] = completed
        result["failure_count"] = failures
        result["status"] = "running" if processed_count < len(jobs) else ("completed" if failures == 0 else "completed_with_failures")
        write_running_manifest(output_manifest_path, result)

    if args.concurrency == 1 or len(job_items) <= 1:
        for processed_count, (number, job) in enumerate(job_items, start=1):
            _, record, failed = execute_job(number, job)
            update_result(processed_count, record, failed)
    else:
        worker_count = min(args.concurrency, len(job_items))
        emit("batch_parallel_start", selected_job_count=len(jobs), concurrency=worker_count)
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {executor.submit(execute_job, number, job): number for number, job in job_items}
            for processed_count, future in enumerate(as_completed(futures), start=1):
                _, record, failed = future.result()
                update_result(processed_count, record, failed)

    emit(
        "batch_done",
        selected_job_count=len(jobs),
        completed_count=result.get("completed_count", 0),
        failure_count=failures,
        output_manifest=str(output_manifest_path),
    )
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ScriptError as exc:
        emit("fatal_error", message=str(exc))
        raise SystemExit(2)
