from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT / "canvas-bridge", ROOT / "tests"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from executor_contract import ExecutionRequest, ExecutionResult  # noqa: E402
from qc_repair_cli import run_cli  # noqa: E402
from qc_repair_fixtures import build_qc_repair_fixture  # noqa: E402


def write_png(path: Path, width: int, height: int) -> None:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (width, height), color=(240, 240, 240)).save(path, format="PNG")


class RecordingImageExecutor:
    name = "openai-image"

    def __init__(self) -> None:
        self.calls: list[ExecutionRequest] = []

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        self.calls.append(request)
        config_id = request.payload.output_path.stem
        dimensions = (10, 10) if config_id.startswith("main_") else (9, 12)
        write_png(request.payload.output_path, *dimensions)
        return ExecutionResult(
            detail="generated",
            outputs=(request.payload.output_path,),
            provider=self.name,
        )


def ready_route(_manifest_path: Path) -> dict:
    return {
        "current_stage": "ready",
        "next_required_skill": None,
        "blocked_reasons": [],
        "available_artifacts": ["qc_reports"],
        "outputs": {"renders": {"file_count": 14}, "repaired": {"file_count": 0}},
    }


class QcRepairCliTest(unittest.TestCase):
    def test_cli_gate_rejection_makes_zero_executor_calls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = build_qc_repair_fixture(Path(tmp))
            recorder = RecordingImageExecutor()
            output = io.StringIO()

            code = run_cli(
                ["--batch-manifest", str(fixture.bundle.manifest_path), "--command", "run: repair"],
                environment={},
                repo_reports_dir=fixture.repo_reports_dir,
                route_reader=ready_route,
                image_executor_factory=lambda _context: recorder,
                output=output,
            )

            self.assertEqual(1, code)
            self.assertEqual([], recorder.calls)
            self.assertNotIn("final_prompt", output.getvalue())

    def test_cli_executes_repair_without_qc_or_sensitive_log_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = build_qc_repair_fixture(Path(tmp))
            recorder = RecordingImageExecutor()
            output = io.StringIO()
            secret = "SENSITIVE_" + uuid.uuid4().hex
            relay = "https://" + uuid.uuid4().hex + ".invalid/v1"
            original_prompt = json.loads(
                fixture.bundle.prompt_path("main_01").read_text(encoding="utf-8")
            )["final_prompt"]

            code = run_cli(
                ["--batch-manifest", str(fixture.bundle.manifest_path), "--command", "run: repair"],
                environment={
                    "RENDER_ALLOW_REAL_EXECUTION": "1",
                    "OPENAI_API_KEY": secret,
                    "OPENAI_BASE_URL": relay,
                },
                repo_reports_dir=fixture.repo_reports_dir,
                route_reader=ready_route,
                image_executor_factory=lambda _context: recorder,
                output=output,
            )
            journal = fixture.bundle.manifest_path.parent / "fixture_product.events.jsonl"
            exposed = journal.read_text(encoding="utf-8") + output.getvalue()

            self.assertEqual(0, code)
            self.assertTrue(recorder.calls)
            self.assertTrue(all(call.step == "renders" for call in recorder.calls))
            self.assertNotIn("qc", [call.step for call in recorder.calls])
            self.assertNotIn(original_prompt, exposed)
            self.assertNotIn(secret, exposed)
            self.assertNotIn(relay, exposed)


if __name__ == "__main__":
    unittest.main()
