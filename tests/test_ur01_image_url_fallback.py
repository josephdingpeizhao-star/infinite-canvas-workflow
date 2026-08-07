from __future__ import annotations

import base64
import copy
from http import client as http_client
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib import error, request


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
TESTS = ROOT / "tests"
for extra in (BRIDGE, TESTS):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from executor_contract import (  # noqa: E402
    ExecutionRequest,
    ExecutorContext,
    ExecutorExecutionError,
    ImageGenerationTask,
)
from final_prompt_integrity_fixtures import build_final_prompt_bundle, write_json  # noqa: E402
from image_production_executor import ImageProductionExecutor  # noqa: E402
from openai_image_executor import (  # noqa: E402
    CLIENT_USER_AGENT,
    DEFAULT_IMAGE_DOWNLOAD_MAX_BYTES,
    HttpResponse,
    OpenAIImageExecutor,
    UrllibTransport,
    _ImageDownloadRedirectError,
    _ImageDownloadRedirectHandler,
    _validate_image_download_url,
)
from render_task_assembler import RenderTaskPlan  # noqa: E402
from workflow_production_service import (  # noqa: E402
    WorkflowProductionService,
    _IMAGE_SERVICE_FAILURE_CODES,
)


IMAGE_BYTES = b"\x89PNG\r\n\x1a\nur01-download"
SAFE_URL = "https://images.example.test/generated/image.png?signature=signed-value"


def provider_response(data0: object | None, *, include_data0: bool = True) -> HttpResponse:
    payload: dict[str, object] = {"created": 123, "data": []}
    if include_data0:
        payload["data"] = [data0]
    return HttpResponse(status=200, headers={"x-request-id": "req_ur01"}, body=json.dumps(payload).encode())


class PostOnlyTransport:
    """Old fake shape: intentionally has no get method."""

    def __init__(self, response: HttpResponse):
        self.response = response
        self.post_calls: list[dict[str, object]] = []

    def post(self, url: str, headers: dict[str, str], body: bytes, timeout: float) -> HttpResponse:
        self.post_calls.append({"url": url, "headers": headers, "body": body, "timeout": timeout})
        return self.response


class DownloadTransport(PostOnlyTransport):
    def __init__(
        self,
        response: HttpResponse,
        download_response: HttpResponse | BaseException,
    ) -> None:
        super().__init__(response)
        self.download_response = download_response
        self.get_calls: list[dict[str, object]] = []

    def get(self, url: str, headers: dict[str, str], timeout: float) -> HttpResponse:
        self.get_calls.append({"url": url, "headers": headers, "timeout": timeout})
        if isinstance(self.download_response, BaseException):
            raise self.download_response
        return self.download_response


class UrlopenDownloadResponse:
    status = 200
    headers = {"Content-Type": "image/png"}

    def __init__(
        self,
        *,
        body: bytes = IMAGE_BYTES,
        read_error: BaseException | None = None,
    ) -> None:
        self.body = body
        self.read_error = read_error
        self.read_sizes: list[int] = []
        self.closed = False

    def __enter__(self) -> UrlopenDownloadResponse:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.closed = True
        return None

    def close(self) -> None:
        self.closed = True

    def read(self, size: int) -> bytes:
        self.read_sizes.append(size)
        if self.read_error is not None:
            raise self.read_error
        return self.body[:size]

    def geturl(self) -> str:
        return SAFE_URL


class RecordingOpener:
    def __init__(self, response: object | None = None) -> None:
        self.calls: list[tuple[request.Request, float]] = []
        self.response = response or UrlopenDownloadResponse()

    def open(self, outbound: request.Request, timeout: float):  # type: ignore[no-untyped-def]
        self.calls.append((outbound, timeout))
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


class RedirectResponse:
    def __init__(self) -> None:
        self.read_calls: list[object] = []
        self.closed = False

    def read(self, *args):  # type: ignore[no-untyped-def]
        self.read_calls.append(args)
        raise AssertionError("redirect response body must not be read")

    def close(self) -> None:
        self.closed = True


class RedirectParent:
    def __init__(self) -> None:
        self.calls: list[tuple[request.Request, float]] = []

    def open(self, outbound: request.Request, timeout: float):  # type: ignore[no-untyped-def]
        self.calls.append((outbound, timeout))
        return "redirected"


class FakeCanvasClient:
    def __init__(self, batch_id: str) -> None:
        self.state = {
            "nodes": [
                {
                    "id": "machine",
                    "type": "workflow",
                    "metadata": {
                        "content": "# workflow-production\n# request-id: req-ur01\nrun: next",
                        "workflowProduction": {
                            "status": "queued",
                            "requestId": "req-ur01",
                            "batchId": batch_id,
                            "requestedAt": 1_000,
                            "producedCount": 0,
                        },
                    },
                },
                {
                    "id": "card",
                    "type": "batch-info",
                    "metadata": {
                        "batchIntake": {
                            "status": "completed",
                            "receipt": {"batchId": batch_id, "imageCount": 14},
                        }
                    },
                },
                {
                    "id": "original",
                    "type": "image",
                    "metadata": {
                        "content": "blob:original",
                        "storageKey": "image:original",
                    },
                },
            ],
            "connections": [
                {"id": "card-machine", "fromNodeId": "card", "toNodeId": "machine"},
                {"id": "image-machine", "fromNodeId": "original", "toNodeId": "machine"},
            ],
        }

    def call_tool(self, name: str) -> dict[str, object]:
        if name != "canvas_get_state":
            raise AssertionError(name)
        return self.state

    def apply_ops(self, ops: list[dict[str, object]]) -> int:
        nodes = self.state["nodes"]
        for op in ops:
            if op.get("type") != "update_node":
                continue
            node = next(item for item in nodes if item["id"] == op["id"])
            node["metadata"] = {**node.get("metadata", {}), **op.get("metadata", {})}
        return len(ops)


class UR01ImageUrlFallbackTest(unittest.TestCase):
    def _executor(
        self,
        transport: object,
        *,
        download_max: object | None = None,
        include_download_max: bool = False,
    ) -> OpenAIImageExecutor:
        environment: dict[str, object] = {"OPENAI_API_KEY": "server-secret"}
        if include_download_max:
            environment["OPENAI_IMAGE_DOWNLOAD_MAX_BYTES"] = download_max
        return OpenAIImageExecutor(
            ExecutorContext(manifest={}, environment=environment),  # type: ignore[arg-type]
            transport=transport,  # type: ignore[arg-type]
        )

    @staticmethod
    def _request(output: Path) -> ExecutionRequest:
        return ExecutionRequest(
            step="renders",
            payload=ImageGenerationTask(prompt="product", output_path=output),
        )

    def _assert_url_rejected(self, url: object) -> ExecutorExecutionError:
        transport = DownloadTransport(
            provider_response({"url": url}),
            HttpResponse(status=200, headers={}, body=IMAGE_BYTES, final_url=SAFE_URL),
        )
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "out.png"
            with self.assertRaises(ExecutorExecutionError) as caught:
                self._executor(transport).execute(self._request(output))
            self.assertFalse(output.exists())
        self.assertEqual([], transport.get_calls)
        self.assertEqual("render_response_invalid", caught.exception.code)
        self.assertEqual("图片服务返回的图片地址无法使用", str(caught.exception))
        self.assertEqual(("created", "data"), caught.exception.response_top_keys)
        self.assertEqual(("url",), caught.exception.response_data0_keys)
        return caught.exception

    @staticmethod
    def _route(_manifest_path: Path) -> dict[str, object]:
        return {
            "current_stage": "needs_generated_images_before_qc",
            "next_required_skill": None,
            "blocked_reasons": ["QC is post-generation only"],
            "available_artifacts": ["final_prompts"],
            "outputs": {
                "renders": {"file_count": 0},
                "repaired": {"file_count": 0},
            },
            "inputs": {"style_reference_images": {"file_count": 1}},
        }

    @staticmethod
    def _render_plan(renders_dir: Path) -> RenderTaskPlan:
        task = ImageGenerationTask(
            prompt="safe fixture prompt",
            output_path=renders_dir / "main_01.png",
        )
        return RenderTaskPlan(tasks=(task,), planned=("main_01",), skipped=())

    def _run_url_download_service_failure(
        self,
        *,
        url: str = SAFE_URL,
        status: int = 404,
    ):  # type: ignore[no-untyped-def]
        with tempfile.TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            repo = temp_root / "repo"
            (repo / "manifests").mkdir(parents=True)
            shutil.copytree(ROOT / "categories", repo / "categories")
            bundle = build_final_prompt_bundle(temp_root / "fixture")
            batch_id = str(bundle.manifest["product_id"])
            manifest_path = repo / "manifests" / f"{batch_id}.batch_manifest.json"
            write_json(manifest_path, bundle.manifest)
            (bundle.root / "workspace" / ".canvas_batch").write_text(
                json.dumps({"type": "canvas-batch-v1", "product_id": batch_id}),
                encoding="utf-8",
            )
            client = FakeCanvasClient(batch_id)
            environment = {
                "RENDER_ALLOW_REAL_EXECUTION": "1",
                "OPENAI_API_KEY": "server-secret",
            }
            transport = DownloadTransport(
                provider_response({"url": url}),
                HttpResponse(
                    status=status,
                    headers={},
                    body=b"not found",
                    final_url=url,
                ),
            )

            def build_executor(step, manifest, path, _on_output):  # type: ignore[no-untyped-def]
                self.assertEqual("renders", step)
                context = ExecutorContext(
                    manifest=manifest,
                    manifest_path=path,
                    environment=environment,
                )
                image_executor = OpenAIImageExecutor(context, transport=transport)
                return ImageProductionExecutor(
                    context,
                    image_executor_factory=lambda _context: image_executor,
                    task_assembler=lambda _manifest, _index: self._render_plan(
                        bundle.renders_dir
                    ),
                )

            service = WorkflowProductionService(
                repo,
                client=client,
                executor_builder=build_executor,
                route_reader=self._route,
                integrity_reader=lambda _route: {
                    "found": True,
                    "status": "pass",
                    "render_blocked": False,
                },
                artifact_reader=lambda _manifest: (),
                render_artifact_reader=lambda _manifest: (),
                repaired_artifact_reader=lambda _manifest: (),
                clock_ms=lambda: 1_100,
                environment=environment,
                batch_lock_root=temp_root / "locks",
            )
            service.poll_once()
            machine = client.state["nodes"][0]
            production = machine["metadata"]["workflowProduction"]
            events = [
                json.loads(line)
                for line in (repo / "manifests" / f"{batch_id}.events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            failed_event = next(event for event in events if event["event"] == "step_failed")
            return (
                copy.deepcopy(production),
                copy.deepcopy(failed_event),
                copy.deepcopy(transport.get_calls),
            )

    # T1: inline bytes retain priority and remain compatible with post-only fakes.
    def test_inline_base64_uses_old_path_without_get(self) -> None:
        transport = PostOnlyTransport(
            provider_response({"b64_json": base64.b64encode(IMAGE_BYTES).decode("ascii")})
        )
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "out.png"
            result = self._executor(transport).execute(self._request(output))
            self.assertEqual(IMAGE_BYTES, output.read_bytes())
        self.assertFalse(hasattr(transport, "get"))
        self.assertEqual(1, len(transport.post_calls))
        self.assertEqual("inline", result.metadata["retrieval"])

    def test_invalid_present_base64_does_not_fall_back_to_url(self) -> None:
        transport = DownloadTransport(
            provider_response({"b64_json": "%%%", "url": SAFE_URL}),
            HttpResponse(status=200, headers={}, body=IMAGE_BYTES, final_url=SAFE_URL),
        )
        with tempfile.TemporaryDirectory() as temp, self.assertRaises(ExecutorExecutionError) as caught:
            self._executor(transport).execute(self._request(Path(temp) / "out.png"))
        self.assertEqual("render_response_invalid", caught.exception.code)
        self.assertEqual([], transport.get_calls)

    # T2 + N1: URL retrieval writes exact bytes and sends only the fixed user agent.
    def test_https_url_downloads_once_without_credentials_and_marks_metadata(self) -> None:
        transport = DownloadTransport(
            provider_response({"url": SAFE_URL}),
            HttpResponse(status=200, headers={}, body=IMAGE_BYTES, final_url=SAFE_URL),
        )
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "out.png"
            result = self._executor(transport).execute(self._request(output))
            self.assertEqual(IMAGE_BYTES, output.read_bytes())
        self.assertEqual(1, len(transport.post_calls))
        self.assertEqual(1, len(transport.get_calls))
        self.assertEqual(SAFE_URL, transport.get_calls[0]["url"])
        self.assertEqual({"User-Agent": CLIENT_USER_AGENT}, transport.get_calls[0]["headers"])
        self.assertEqual(180.0, transport.get_calls[0]["timeout"])
        self.assertEqual("url", result.metadata["retrieval"])
        self.assertNotIn("url", result.metadata)
        self.assertNotIn(SAFE_URL, repr(result.metadata))

    # T3: no inline bytes and no URL retains the existing ER-02 shape failure.
    def test_missing_both_keeps_response_invalid_and_shape(self) -> None:
        transport = PostOnlyTransport(provider_response({"revised_prompt": "safe"}))
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "out.png"
            with self.assertRaises(ExecutorExecutionError) as caught:
                self._executor(transport).execute(self._request(output))
            self.assertFalse(output.exists())
        self.assertEqual("render_response_invalid", caught.exception.code)
        self.assertEqual(("created", "data"), caught.exception.response_top_keys)
        self.assertEqual(("revised_prompt",), caught.exception.response_data0_keys)

    # T4: download HTTP failures stop after one generation and never create output.
    def test_download_non_2xx_stops_without_output(self) -> None:
        for status in (404, 500):
            with self.subTest(status=status):
                transport = DownloadTransport(
                    provider_response({"url": SAFE_URL}),
                    HttpResponse(status=status, headers={}, body=b"provider body", final_url=SAFE_URL),
                )
                with tempfile.TemporaryDirectory() as temp:
                    output = Path(temp) / "out.png"
                    with self.assertRaises(ExecutorExecutionError) as caught:
                        self._executor(transport).execute(self._request(output))
                    self.assertFalse(output.exists())
                self.assertEqual("render_image_download_failed", caught.exception.code)
                self.assertEqual(status, caught.exception.http_status)
                self.assertEqual(1, len(transport.post_calls))
                self.assertEqual(1, len(transport.get_calls))

    # S1.
    def test_rejects_plain_http_url(self) -> None:
        self._assert_url_rejected("http://images.example.test/image.png")

    # S2.
    def test_rejects_file_and_data_urls(self) -> None:
        for url in ("file:///tmp/image.png", "data:image/png;base64,AAAA"):
            with self.subTest(url=url):
                self._assert_url_rejected(url)

    # S3.
    def test_rejects_url_with_username_or_password(self) -> None:
        self._assert_url_rejected("https://user:password@images.example.test/image.png")

    # S4.
    def test_rejects_private_and_loopback_ipv4(self) -> None:
        for url in ("https://10.2.3.4/image.png", "https://127.0.0.1/image.png"):
            with self.subTest(url=url):
                self._assert_url_rejected(url)

    def test_rejects_legacy_ipv4_loopback_spellings(self) -> None:
        for url in (
            "https://127.1/image.png",
            "https://2130706433/image.png",
            "https://0177.0.0.1/image.png",
            "https://0x7f000001/image.png",
            "https://127.1./image.png",
            "https://2130706433./image.png",
        ):
            with self.subTest(url=url):
                self._assert_url_rejected(url)

    # S5.
    def test_rejects_loopback_and_link_local_ipv6(self) -> None:
        for url in ("https://[::1]/image.png", "https://[fe80::1]/image.png"):
            with self.subTest(url=url):
                self._assert_url_rejected(url)

    def test_rejects_reserved_nat64_and_site_local_ipv6(self) -> None:
        for url in (
            "https://[64:ff9b::127.0.0.1]/image.png",
            "https://[fec0::1]/image.png",
        ):
            with self.subTest(url=url):
                self._assert_url_rejected(url)

    def test_public_numeric_ip_urls_reach_download_transport(self) -> None:
        for url in (
            "https://8.8.8.8/image.png",
            "https://[2606:4700:4700::1111]/image.png",
        ):
            with self.subTest(url=url):
                transport = DownloadTransport(
                    provider_response({"url": url}),
                    HttpResponse(
                        status=200,
                        headers={},
                        body=IMAGE_BYTES,
                        final_url=url,
                    ),
                )
                with tempfile.TemporaryDirectory() as temp:
                    output = Path(temp) / "out.png"
                    self._executor(transport).execute(self._request(output))
                    self.assertEqual(IMAGE_BYTES, output.read_bytes())
                self.assertEqual(1, len(transport.get_calls))

    # S6.
    def test_rejects_empty_non_string_and_overlong_urls(self) -> None:
        for url in ("", None, 123, "https://images.example.test/" + "x" * 2049):
            with self.subTest(kind=type(url).__name__, length=len(url) if isinstance(url, str) else None):
                self._assert_url_rejected(url)

    # S7 plus the explicit fragment rule.
    def test_rejects_missing_hostname_and_fragment(self) -> None:
        for url in (
            "https:///image.png",
            "https://images.example.test/image.png#section",
            "https://images.example.test/image.png#",
            "https://images.example.test/image.png?#",
        ):
            with self.subTest(url=url):
                self._assert_url_rejected(url)

    def test_percent_encoded_fragment_marker_is_allowed(self) -> None:
        url = "https://images.example.test/image%23copy.png?signature=value%23part"
        transport = DownloadTransport(
            provider_response({"url": url}),
            HttpResponse(status=200, headers={}, body=IMAGE_BYTES, final_url=url),
        )
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "out.png"
            self._executor(transport).execute(self._request(output))
            self.assertEqual(IMAGE_BYTES, output.read_bytes())
        self.assertEqual(1, len(transport.get_calls))

    def test_rejects_literal_control_characters_before_parsing(self) -> None:
        for character in ("\r", "\n", "\t", "\x7f"):
            with self.subTest(codepoint=ord(character)):
                url = f"https://images.example.test/image{character}.png"
                with self.assertRaises(ValueError) as unsafe:
                    _validate_image_download_url(url)
                self.assertEqual("unsafe_url", str(unsafe.exception))
                self._assert_url_rejected(url)

        encoded_url = (
            "https://images.example.test/image%0D%0A%09%7F.png"
            "?signature=value%0D%0A%09%7F"
        )
        transport = DownloadTransport(
            provider_response({"url": encoded_url}),
            HttpResponse(
                status=200,
                headers={},
                body=IMAGE_BYTES,
                final_url=encoded_url,
            ),
        )
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "out.png"
            self._executor(transport).execute(self._request(output))
            self.assertEqual(IMAGE_BYTES, output.read_bytes())
        self.assertEqual(1, len(transport.get_calls))

    # S8.
    def test_rejects_body_over_configured_limit_without_truncating_or_writing(self) -> None:
        limit = 1_000_000
        transport = DownloadTransport(
            provider_response({"url": SAFE_URL}),
            HttpResponse(
                status=200,
                headers={},
                body=b"x" * (limit + 1),
                final_url=SAFE_URL,
            ),
        )
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "out.png"
            with self.assertRaises(ExecutorExecutionError) as caught:
                self._executor(
                    transport,
                    download_max=str(limit),
                    include_download_max=True,
                ).execute(self._request(output))
            self.assertFalse(output.exists())
            self.assertEqual([], list(output.parent.glob(".out.png.*.tmp")))
        self.assertEqual("render_image_download_failed", caught.exception.code)
        self.assertEqual(f"图片下载响应超过 {limit} 字节上限", str(caught.exception))

    # S9.
    def test_rejects_empty_2xx_body(self) -> None:
        transport = DownloadTransport(
            provider_response({"url": SAFE_URL}),
            HttpResponse(status=204, headers={}, body=b"", final_url=SAFE_URL),
        )
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "out.png"
            with self.assertRaises(ExecutorExecutionError) as caught:
                self._executor(transport).execute(self._request(output))
            self.assertFalse(output.exists())
        self.assertEqual("render_image_download_failed", caught.exception.code)
        self.assertEqual("图片下载响应为空", str(caught.exception))

    # S10: both the executor's final-URL check and the production redirect hook reject downgrade.
    def test_rejects_https_to_http_redirect_downgrade(self) -> None:
        transport = DownloadTransport(
            provider_response({"url": SAFE_URL}),
            HttpResponse(
                status=200,
                headers={},
                body=IMAGE_BYTES,
                final_url="http://images.example.test/image.png",
            ),
        )
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "out.png"
            with self.assertRaises(ExecutorExecutionError) as caught:
                self._executor(transport).execute(self._request(output))
            self.assertFalse(output.exists())
        self.assertEqual("render_image_download_failed", caught.exception.code)

        handler = _ImageDownloadRedirectHandler()
        outbound = request.Request(SAFE_URL, method="GET")
        with self.assertRaises(_ImageDownloadRedirectError):
            handler.redirect_request(
                outbound,
                None,
                302,
                "Found",
                {},
                "http://images.example.test/image.png",
            )
        self.assertEqual(3, handler.max_redirections)

    def test_urllib_get_filters_all_headers_except_user_agent(self) -> None:
        opener = RecordingOpener()
        handlers: list[object] = []

        def build_opener(*values):  # type: ignore[no-untyped-def]
            handlers.extend(values)
            return opener

        with mock.patch("openai_image_executor.request.build_opener", side_effect=build_opener):
            response = UrllibTransport().get(
                SAFE_URL,
                {
                    "Authorization": "Bearer server-secret",
                    "Proxy-Authorization": "Basic proxy-secret",
                    "Cookie": "session=secret-cookie",
                    "X-Api-Key": "secret-custom-key",
                    "User-Agent": "Caller-Agent/2.0",
                },
                45.0,
            )
        outbound, timeout = opener.calls[0]
        self.assertEqual(IMAGE_BYTES, response.body)
        self.assertEqual(45.0, timeout)
        self.assertEqual(
            [("User-agent", "Caller-Agent/2.0")],
            list(outbound.header_items()),
        )
        for forbidden_header in (
            "authorization",
            "proxy-authorization",
            "cookie",
            "x-api-key",
        ):
            self.assertNotIn(
                forbidden_header,
                {name.lower() for name, _value in outbound.header_items()},
            )
        proxy_handler = next(
            value for value in handlers if isinstance(value, request.ProxyHandler)
        )
        self.assertEqual({}, proxy_handler.proxies)

    def test_urllib_get_bounds_success_and_http_error_reads(self) -> None:
        limit = 1_000_000
        success_stream = UrlopenDownloadResponse(body=b"x" * (limit + 2))
        with mock.patch(
            "openai_image_executor.request.build_opener",
            return_value=RecordingOpener(success_stream),
        ):
            response = UrllibTransport(image_download_max_bytes=limit).get(
                SAFE_URL,
                {"User-Agent": CLIENT_USER_AGENT},
                45.0,
            )
        self.assertEqual([limit + 1], success_stream.read_sizes)
        self.assertEqual(limit + 1, len(response.body))
        self.assertTrue(success_stream.closed)

        error_stream = UrlopenDownloadResponse(body=b"y" * (limit + 2))
        failure = error.HTTPError(
            SAFE_URL,
            500,
            "failure",
            {},
            error_stream,
        )
        with mock.patch(
            "openai_image_executor.request.build_opener",
            return_value=RecordingOpener(failure),
        ):
            response = UrllibTransport(image_download_max_bytes=limit).get(
                SAFE_URL,
                {"User-Agent": CLIENT_USER_AGENT},
                45.0,
            )
        self.assertEqual(500, response.status)
        self.assertEqual([limit + 1], error_stream.read_sizes)
        self.assertEqual(limit + 1, len(response.body))
        self.assertTrue(error_stream.closed)

    def test_redirect_handler_closes_30x_without_reading_body(self) -> None:
        handler = _ImageDownloadRedirectHandler()
        parent = RedirectParent()
        handler.add_parent(parent)
        outbound = request.Request(
            SAFE_URL,
            headers={"User-Agent": CLIENT_USER_AGENT},
            method="GET",
        )
        self.assertEqual(
            [("User-agent", CLIENT_USER_AGENT)],
            list(outbound.header_items()),
        )
        outbound.timeout = 45.0
        stream = RedirectResponse()

        result = handler.http_error_302(
            outbound,
            stream,
            302,
            "Found",
            {"location": "https://cdn.example.test/final.png?signature=kept"},
        )

        self.assertEqual("redirected", result)
        self.assertEqual([], stream.read_calls)
        self.assertTrue(stream.closed)
        self.assertEqual(1, len(parent.calls))
        redirected, timeout = parent.calls[0]
        self.assertEqual(45.0, timeout)
        self.assertEqual(
            "https://cdn.example.test/final.png?signature=kept",
            redirected.full_url,
        )
        self.assertEqual(
            [("User-agent", CLIENT_USER_AGENT)],
            list(redirected.header_items()),
        )
        for forbidden_header in (
            "authorization",
            "proxy-authorization",
            "cookie",
            "x-api-key",
        ):
            self.assertNotIn(
                forbidden_header,
                {name.lower() for name, _value in redirected.header_items()},
            )

    def test_redirect_handler_allows_three_total_hops_and_rejects_fourth(self) -> None:
        handler = _ImageDownloadRedirectHandler()
        parent = RedirectParent()
        handler.add_parent(parent)
        outbound = request.Request(SAFE_URL, method="GET")
        outbound.timeout = 45.0

        for hop, target in enumerate(
            (
                "https://a.example.test/image.png",
                "https://b.example.test/image.png",
                "https://a.example.test/image.png",
            ),
            start=1,
        ):
            stream = RedirectResponse()
            self.assertEqual(
                "redirected",
                handler.http_error_302(
                    outbound,
                    stream,
                    302,
                    "Found",
                    {"location": target},
                ),
            )
            self.assertEqual([], stream.read_calls)
            self.assertTrue(stream.closed)
            outbound = parent.calls[-1][0]
            self.assertEqual(hop, outbound._image_download_redirect_count)

        fourth_stream = RedirectResponse()
        with self.assertRaises(_ImageDownloadRedirectError):
            handler.http_error_302(
                outbound,
                fourth_stream,
                302,
                "Found",
                {"location": "https://b.example.test/image.png"},
            )
        self.assertEqual([], fourth_stream.read_calls)
        self.assertTrue(fourth_stream.closed)
        self.assertEqual(3, len(parent.calls))

    def test_download_stream_io_failures_use_existing_safe_codes(self) -> None:
        cases = (
            (
                "timeout",
                TimeoutError("secret timeout detail"),
                "render_timeout",
                "图片服务连续 45 秒未返回新数据，已停止等待",
            ),
            (
                "connection_reset",
                ConnectionResetError("secret reset detail"),
                "render_network_error",
                "无法连接图片下载服务",
            ),
            (
                "remote_disconnected",
                http_client.RemoteDisconnected("secret disconnect detail"),
                "render_network_error",
                "无法连接图片下载服务",
            ),
            (
                "incomplete_read",
                http_client.IncompleteRead(b"partial-secret", 99),
                "render_network_error",
                "无法连接图片下载服务",
            ),
        )
        for name, stream_error, expected_code, expected_message in cases:
            with self.subTest(name=name):
                stream = UrlopenDownloadResponse(read_error=stream_error)
                with mock.patch(
                    "openai_image_executor.request.build_opener",
                    return_value=RecordingOpener(stream),
                ), self.assertRaises(ExecutorExecutionError) as caught:
                    UrllibTransport().get(
                        SAFE_URL,
                        {"User-Agent": CLIENT_USER_AGENT},
                        45.0,
                    )
                self.assertEqual(expected_code, caught.exception.code)
                self.assertEqual(expected_message, str(caught.exception))
                self.assertNotIn("secret", str(caught.exception))

    def test_valid_url_404_crosses_real_wrapper_service_journal_and_card(self) -> None:
        production, event, get_calls = self._run_url_download_service_failure()

        self.assertEqual(1, len(get_calls))
        self.assertEqual("render_image_download_failed", event["failure_code"])
        self.assertEqual("image_service", production["failureSource"])
        self.assertIn("图片服务已返回图片链接，但图片未能取回（HTTP 404）", production["errorMessage"])
        self.assertIn("响应字段：created、data", production["errorMessage"])
        self.assertIn("data[0] 字段：url", production["errorMessage"])
        self.assertIn("渲染失败：图片未能取回 HTTP 404", event["detail"])
        self.assertIn("响应字段：created、data", event["detail"])
        self.assertIn("data[0] 字段：url", event["detail"])

    # N2.
    def test_failure_and_metadata_never_expose_url_host_or_query(self) -> None:
        sensitive_url = (
            "https://private-download.example.test/image.png"
            "?signature=very-secret-signature&api_key=sk-private-shape"
        )
        failure_transport = DownloadTransport(
            provider_response({"url": sensitive_url}),
            HttpResponse(status=500, headers={}, body=b"failure", final_url=sensitive_url),
        )
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "out.png"
            with self.assertRaises(ExecutorExecutionError) as caught:
                self._executor(failure_transport).execute(self._request(output))
            self.assertFalse(output.exists())

        production, event, service_get_calls = self._run_url_download_service_failure(
            url=sensitive_url,
            status=500,
        )

        success_transport = DownloadTransport(
            provider_response({"url": sensitive_url}),
            HttpResponse(
                status=200,
                headers={},
                body=IMAGE_BYTES,
                final_url=sensitive_url,
            ),
        )
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "out.png"
            result = self._executor(success_transport).execute(self._request(output))
            self.assertEqual(IMAGE_BYTES, output.read_bytes())

        self.assertEqual("render_image_download_failed", caught.exception.code)
        self.assertEqual(1, len(failure_transport.get_calls))
        self.assertEqual(1, len(service_get_calls))
        self.assertEqual(1, len(success_transport.get_calls))
        self.assertEqual("url", result.metadata["retrieval"])
        self.assertNotIn("url", result.metadata)

        surfaced_values = (
            str(caught.exception),
            repr(vars(caught.exception)),
            str(production["errorMessage"]),
            str(event["detail"]),
            repr(result.metadata),
            json.dumps(result.metadata, ensure_ascii=False, sort_keys=True),
        )
        sensitive_fragments = (
            sensitive_url,
            "private-download.example.test",
            "signature=very-secret-signature",
            "very-secret-signature",
            "api_key=sk-private-shape",
            "sk-private-shape",
        )
        for surfaced in surfaced_values:
            for sensitive in sensitive_fragments:
                self.assertNotIn(sensitive, surfaced)

    # C1.
    def test_download_limit_rejects_invalid_values_with_family_message(self) -> None:
        expected = (
            "OPENAI_IMAGE_DOWNLOAD_MAX_BYTES 必须是 1000000 到 100000000 的整数"
        )
        for value in ("", "abc", "999999", "100000001", "1000000.0", "01000000", True):
            with self.subTest(value=value):
                with self.assertRaises(ExecutorExecutionError) as caught:
                    self._executor(
                        PostOnlyTransport(provider_response({"b64_json": "AAAA"})),
                        download_max=value,
                        include_download_max=True,
                    )
                self.assertEqual(expected, str(caught.exception))

    # C2.
    def test_download_limit_defaults_to_twenty_million_bytes(self) -> None:
        executor = self._executor(PostOnlyTransport(provider_response({"b64_json": "AAAA"})))
        self.assertEqual(DEFAULT_IMAGE_DOWNLOAD_MAX_BYTES, executor.image_download_max_bytes)
        self.assertEqual(20_000_000, executor.image_download_max_bytes)

    def test_default_production_transport_receives_configured_download_limit(self) -> None:
        executor = OpenAIImageExecutor(
            ExecutorContext(
                manifest={},
                environment={
                    "OPENAI_API_KEY": "server-secret",
                    "OPENAI_IMAGE_DOWNLOAD_MAX_BYTES": "1000000",
                },
            )
        )
        self.assertIsInstance(executor.transport, UrllibTransport)
        self.assertEqual(1_000_000, executor.transport.image_download_max_bytes)

    # R3 boundary law: unusable source and failed retrieval must never share a code.
    def test_r3_boundary_keeps_invalid_source_distinct_from_valid_url_404(self) -> None:
        cases = (
            (
                "invalid_source",
                DownloadTransport(
                    provider_response({"url": "hidden"}),
                    HttpResponse(status=200, headers={}, body=IMAGE_BYTES),
                ),
                "render_response_invalid",
            ),
            (
                "valid_source_download_failure",
                DownloadTransport(
                    provider_response({"url": SAFE_URL}),
                    HttpResponse(
                        status=404,
                        headers={},
                        body=b"not found",
                        final_url=SAFE_URL,
                    ),
                ),
                "render_image_download_failed",
            ),
        )

        for name, transport, expected_code in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                with self.assertRaises(ExecutorExecutionError) as caught:
                    self._executor(transport).execute(
                        self._request(Path(temp) / "out.png")
                    )

                failure = caught.exception
                self.assertEqual(expected_code, failure.code)
                self.assertEqual(("created", "data"), failure.response_top_keys)
                self.assertEqual(("url",), failure.response_data0_keys)
                self.assertIn(failure.code, _IMAGE_SERVICE_FAILURE_CODES)
                event, card, code = (
                    WorkflowProductionService._structured_render_failure_messages(failure)
                )
                self.assertEqual(expected_code, code)
                self.assertIn("响应字段：created、data", event)
                self.assertIn("data[0] 字段：url", event)
                self.assertIn("响应字段：created、data", card)
                self.assertIn("data[0] 字段：url", card)

                failure_source = (
                    "image_service"
                    if code in _IMAGE_SERVICE_FAILURE_CODES
                    else None
                )
                self.assertEqual("image_service", failure_source)

    def test_new_failure_code_has_image_service_attribution_and_fixed_message(self) -> None:
        failure = ExecutorExecutionError("must not be displayed")
        failure.code = "render_image_download_failed"
        failure.http_status = 404
        failure.successful_count = 0
        failure.planned_count = 4
        failure.skipped_count = 0

        event, card, code = WorkflowProductionService._structured_render_failure_messages(failure)

        self.assertEqual("render_image_download_failed", code)
        self.assertIn(code, _IMAGE_SERVICE_FAILURE_CODES)
        self.assertEqual(
            "渲染失败：图片未能取回 HTTP 404；成功 0/计划 4/跳过 0",
            event,
        )
        self.assertEqual(
            "图片服务已返回图片链接，但图片未能取回（HTTP 404）。"
            "本轮成功 0 张、计划 4 张、跳过 0 张。"
            "机器已停下，未自动重试，已完成的成果都保留了。",
            card,
        )

    # The success boundary is strictly below 300; HTTP 300 is a redirect, not an image.
    def test_download_status_300_is_not_success_and_never_writes_output(self) -> None:
        transport = DownloadTransport(
            provider_response({"url": SAFE_URL}),
            HttpResponse(
                status=300,
                headers={},
                body=IMAGE_BYTES,
                final_url=SAFE_URL,
            ),
        )
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "out.png"
            with self.assertRaises(ExecutorExecutionError) as caught:
                self._executor(transport).execute(self._request(output))
            self.assertFalse(output.exists())

        self.assertEqual("render_image_download_failed", caught.exception.code)
        self.assertEqual(300, caught.exception.http_status)

    # 100.64/10 misses the other seven IP guards; only not-is_global closes this SSRF gap.
    def test_non_global_cgnat_address_is_rejected_while_public_ip_is_allowed(self) -> None:
        with self.assertRaises(ValueError):
            _validate_image_download_url("https://100.64.0.1/a.png")

        public_url = "https://8.8.8.8/a.png"
        self.assertEqual(public_url, _validate_image_download_url(public_url))


if __name__ == "__main__":
    unittest.main()
