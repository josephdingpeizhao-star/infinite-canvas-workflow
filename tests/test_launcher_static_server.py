import http.client
import tempfile
import threading
import unittest
from pathlib import Path

from launcher.static_server import (
    StaticServerError,
    content_type_for,
    create_server,
    validate_dist_root,
)


class LauncherStaticServerTests(unittest.TestCase):
    def test_required_mime_types_are_explicit(self):
        self.assertEqual(content_type_for(Path("app.js")), "text/javascript; charset=utf-8")
        self.assertEqual(content_type_for(Path("app.css")), "text/css; charset=utf-8")
        self.assertEqual(content_type_for(Path("icon.svg")), "image/svg+xml")
        self.assertEqual(content_type_for(Path("module.wasm")), "application/wasm")
        self.assertEqual(content_type_for(Path("data.json")), "application/json; charset=utf-8")

    def test_spa_fallback_and_asset_delivery(self):
        with self.running_server() as (host, port):
            status, body, content_type = self.request(host, port, "/canvas/deep/link")
            self.assertEqual(status, 200)
            self.assertEqual(body, b"<h1>canvas</h1>")
            self.assertEqual(content_type, "text/html; charset=utf-8")

            status, body, content_type = self.request(host, port, "/assets/app.js")
            self.assertEqual(status, 200)
            self.assertEqual(body, b"console.log('ok')")
            self.assertEqual(content_type, "text/javascript; charset=utf-8")

    def test_traversal_absolute_and_encoded_variants_are_rejected(self):
        blocked = (
            "/../secret.txt",
            "/%2e%2e/secret.txt",
            "/%252e%252e/secret.txt",
            "/C:%5CWindows%5Cwin.ini",
            "//server/share/file.txt",
            "/%2Fetc/passwd",
        )
        with self.running_server() as (host, port):
            for path in blocked:
                with self.subTest(path=path):
                    status, _, _ = self.request(host, port, path)
                    self.assertEqual(status, 404)

    def test_server_refuses_non_loopback_binding(self):
        with tempfile.TemporaryDirectory() as raw:
            root = self.make_dist(Path(raw))
            with self.assertRaisesRegex(StaticServerError, "127.0.0.1"):
                create_server(root, host="0.0.0.0", port=0)

    def test_missing_dist_or_index_fails_before_binding(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "missing"
            with self.assertRaisesRegex(StaticServerError, "dist"):
                validate_dist_root(root)
            root.mkdir()
            with self.assertRaisesRegex(StaticServerError, "index.html"):
                validate_dist_root(root)

    @staticmethod
    def make_dist(root: Path) -> Path:
        dist = root / "dist"
        (dist / "assets").mkdir(parents=True)
        (dist / "index.html").write_text("<h1>canvas</h1>", encoding="utf-8")
        (dist / "assets" / "app.js").write_text("console.log('ok')", encoding="utf-8")
        return dist

    def running_server(self):
        test_case = self

        class ServerContext:
            def __enter__(self):
                self.temp = tempfile.TemporaryDirectory()
                root = test_case.make_dist(Path(self.temp.name))
                self.server = create_server(root, host="127.0.0.1", port=0)
                self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
                self.thread.start()
                return self.server.server_address

            def __exit__(self, exc_type, exc, traceback):
                self.server.shutdown()
                self.server.server_close()
                self.thread.join(timeout=2)
                self.temp.cleanup()

        return ServerContext()

    @staticmethod
    def request(host: str, port: int, path: str):
        connection = http.client.HTTPConnection(host, port, timeout=2)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            return response.status, response.read(), response.getheader("Content-Type")
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
