import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class LauncherShortcutScriptTests(unittest.TestCase):
    def test_shortcut_creator_is_gbk_crlf_and_never_falls_back_to_python_console(self):
        path = REPO_ROOT / "launcher" / "创建桌面入口.bat"
        raw = path.read_bytes()
        text = raw.decode("gbk")

        self.assertNotIn(b"\xef\xbb\xbf", raw[:3])
        self.assertEqual(text.splitlines()[0].lower(), "@chcp 936 >nul")
        self.assertNotIn("\n", text.replace("\r\n", ""))
        self.assertIn("pythonw.exe", text.lower())
        self.assertNotIn("python.exe", text.lower())
        self.assertIn("无限画布工作台.lnk", text)
        self.assertIn("停止画布工作台.lnk", text)
        self.assertIn("canvas_launcher.py", text)
        self.assertIn("canvas_stop.py", text)


if __name__ == "__main__":
    unittest.main()
