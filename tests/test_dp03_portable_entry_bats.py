import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
BAT_PATHS = {
    "start": REPO_ROOT / "启动工作台.bat",
    "stop": REPO_ROOT / "停止工作台.bat",
}


class PortableEntryBatTests(unittest.TestCase):
    def test_entry_bats_are_gbk_without_bom_and_use_only_crlf(self):
        for name, path in BAT_PATHS.items():
            with self.subTest(entry=name):
                raw = path.read_bytes()
                text = raw.decode("gbk")

                self.assertNotIn(b"\xef\xbb\xbf", raw[:3])
                self.assertEqual(text.splitlines()[0].lower(), "@chcp 936 >nul")
                self.assertNotIn("\n", text.replace("\r\n", ""))

    def test_entry_bats_use_only_pythonw(self):
        for name, path in BAT_PATHS.items():
            with self.subTest(entry=name):
                text = path.read_bytes().decode("gbk").lower()

                self.assertIn("pythonw.exe", text)
                self.assertNotIn("python.exe", text)

    def test_entry_bats_route_to_the_matching_launcher(self):
        start_text = BAT_PATHS["start"].read_bytes().decode("gbk").lower()
        stop_text = BAT_PATHS["stop"].read_bytes().decode("gbk").lower()

        self.assertIn(r"launcher\canvas_launcher.py", start_text)
        self.assertNotIn("canvas_stop.py", start_text)
        self.assertIn(r"launcher\canvas_stop.py", stop_text)
        self.assertNotIn("canvas_launcher.py", stop_text)

    def test_entry_bats_are_relative_and_contain_no_credential_terms(self):
        for name, path in BAT_PATHS.items():
            with self.subTest(entry=name):
                text = path.read_bytes().decode("gbk")
                lowered = text.lower()

                self.assertIn(r"%~dp0", lowered)
                self.assertIsNone(re.search(r"[a-z]:\\", text, re.IGNORECASE))
                for term in ("openai", "credentials", "key"):
                    self.assertNotIn(term, lowered)

    def test_gitattributes_has_exact_bat_crlf_rule(self):
        path = REPO_ROOT / ".gitattributes"
        self.assertEqual(path.read_text(encoding="ascii").splitlines(), ["*.bat eol=crlf"])


if __name__ == "__main__":
    unittest.main()
