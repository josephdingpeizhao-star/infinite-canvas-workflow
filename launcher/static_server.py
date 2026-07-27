from __future__ import annotations

import argparse
import mimetypes
import re
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class StaticServerError(RuntimeError):
    """A dist tree that cannot be served safely."""


MIME_OVERRIDES = {
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".wasm": "application/wasm",
}


def content_type_for(path: Path) -> str:
    override = MIME_OVERRIDES.get(path.suffix.casefold())
    if override is not None:
        return override
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def validate_dist_root(root: Path) -> Path:
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise StaticServerError(f"画布 dist 目录不存在：{root}")
    if not (root / "index.html").is_file():
        raise StaticServerError(f"画布 dist 缺少 index.html：{root}")
    return root


def _decode_request_path(raw_path: str) -> str | None:
    if raw_path.startswith("//"):
        return None
    current = raw_path
    try:
        for _ in range(5):
            decoded = urllib.parse.unquote_to_bytes(current).decode("utf-8", errors="strict")
            if decoded == current:
                break
            current = decoded
    except UnicodeError:
        return None
    if re.search(r"%[0-9a-fA-F]{2}", current):
        return None
    if "\x00" in current or "\\" in current or current.startswith("//"):
        return None
    if re.match(r"^/[a-zA-Z]:", current):
        return None
    if any(segment == ".." for segment in current.split("/")):
        return None
    return current


def _handler_for(root: Path) -> type[BaseHTTPRequestHandler]:
    index = root / "index.html"

    class DistRequestHandler(BaseHTTPRequestHandler):
        server_version = "InfiniteCanvasStatic/1.0"
        unsafe_request_target = False

        def parse_request(self) -> bool:
            parts = self.raw_requestline.decode("iso-8859-1", errors="replace").split()
            self.unsafe_request_target = len(parts) >= 2 and parts[1].startswith("//")
            return super().parse_request()

        def do_GET(self) -> None:
            self._serve(include_body=True)

        def do_HEAD(self) -> None:
            self._serve(include_body=False)

        def _serve(self, *, include_body: bool) -> None:
            if self.unsafe_request_target or self.path.startswith("//"):
                self.send_error(404)
                return
            raw_path = urllib.parse.urlsplit(self.path).path
            decoded = _decode_request_path(raw_path)
            if decoded is None:
                self.send_error(404)
                return
            candidate = (root / decoded.lstrip("/")).resolve()
            if candidate != root and root not in candidate.parents:
                self.send_error(404)
                return
            selected = candidate if candidate.is_file() else index
            try:
                payload = selected.read_bytes()
            except OSError:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type_for(selected))
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            if include_body:
                self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            print(f"{self.address_string()} - {format % args}", flush=True)

    return DistRequestHandler


class LoopbackThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True


def create_server(root: Path, *, host: str = "127.0.0.1", port: int = 3000) -> ThreadingHTTPServer:
    if host != "127.0.0.1":
        raise StaticServerError("静态网页服务只允许绑定 127.0.0.1")
    resolved_root = validate_dist_root(root)
    try:
        return LoopbackThreadingHTTPServer((host, port), _handler_for(resolved_root))
    except OSError as error:
        raise StaticServerError(f"静态网页服务无法绑定 {host}:{port}：{error}") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve the Infinite Canvas dist tree on loopback.")
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=3000, type=int)
    args = parser.parse_args(argv)
    try:
        server = create_server(args.root, host=args.host, port=args.port)
    except StaticServerError as error:
        print(str(error), file=sys.stderr, flush=True)
        return 1
    print(f"画布静态网页服务已启动：http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
