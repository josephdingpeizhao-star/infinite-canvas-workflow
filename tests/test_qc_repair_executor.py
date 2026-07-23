from __future__ import annotations

import hashlib
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

from executor_contract import (  # noqa: E402
    ExecutionRequest,
    ExecutionResult,
    ExecutorContext,
    ExecutorExecutionError,
)
from qc_repair import prepare_repair_plan  # noqa: E402
from qc_repair_executor import QcRepairExecutor  # noqa: E402
from qc_repair_fixtures import build_qc_repair_fixture  # noqa: E402
from render_task_assembler import assemble_render_tasks  # noqa: E402


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_png(path: Path, width: int, height: int) -> None:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (width, height), color=(245, 245, 245)).save(path, format="PNG")


class RecordingImageExecutor:
    name = "openai-image"

    def __init__(self, *, fail_ids: set[str] | None = None, two_by_three_ids: set[str] | None = None):
        self.fail_ids = fail_ids or set()
        self.two_by_three_ids = two_by_three_ids or set()
        self.calls: list[ExecutionRequest] = []

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        self.calls.append(request)
        config_id = request.payload.output_path.stem
        if config_id in self.fail_ids:
            raise ExecutorExecutionError("fixture provider failure " + request.payload.prompt)
        if config_id.startswith("main_"):
            dimensions = (10, 10)
        elif config_id in self.two_by_three_ids:
            dimensions = (16, 24)
        else:
            dimensions = (9, 12)
        write_png(request.payload.output_path, *dimensions)
        return ExecutionResult(
            detail="generated",
            outputs=(request.payload.output_path,),
            provider=self.name,
            model="fixture-model",
        )


def read_events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class QcRepairExecutorTest(unittest.TestCase):
    def _build(self, root: Path, *, recorder: RecordingImageExecutor | None = None):
        fixture = build_qc_repair_fixture(root)
        prepared = prepare_repair_plan(
            fixture.bundle.manifest,
            fixture.bundle.manifest_path,
            repo_reports_dir=fixture.repo_reports_dir,
        )
        image_executor = recorder or RecordingImageExecutor()
        journal = root / "fixture.events.jsonl"
        environment = {
            "RENDER_ALLOW_REAL_EXECUTION": "1",
            "OPENAI_API_KEY": "fixture-" + uuid.uuid4().hex,
            "OPENAI_BASE_URL": "https://" + uuid.uuid4().hex + ".invalid/v1",
        }
        context = ExecutorContext(
            manifest=fixture.bundle.manifest,
            manifest_path=fixture.bundle.manifest_path,
            environment=environment,
        )
        executor = QcRepairExecutor(
            context,
            plan=prepared.plan,
            journal_path=journal,
            request_id="fixture-request",
            image_executor_factory=lambda _context: image_executor,
        )
        return fixture, prepared.plan, image_executor, journal, executor

    def test_each_work_order_calls_existing_image_production_once_with_same_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture, plan, image_executor, _journal, executor = self._build(Path(tmp))
            original = assemble_render_tasks(fixture.bundle.manifest, fixture.bundle.index_path)
            expected = {task.output_path.stem: task.reference_images for task in original.tasks}

            executor.execute(ExecutionRequest(step="repair"))

            self.assertEqual(len(plan.work_orders), len(image_executor.calls))
            for call in image_executor.calls:
                self.assertEqual("renders", call.step)
                self.assertEqual(expected[call.payload.output_path.stem], call.payload.reference_images)

    def test_outputs_land_under_repaired_with_original_names_and_renders_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture, plan, _image_executor, _journal, executor = self._build(Path(tmp))
            for mode, count in (("main", 6), ("detail", 8)):
                for number in range(1, count + 1):
                    path = fixture.bundle.renders_dir / f"{mode}_{number:02d}.png"
                    path.write_bytes(f"protected-{mode}-{number}".encode("ascii"))
            before = {path.name: sha256(path) for path in fixture.bundle.renders_dir.glob("*.png")}

            executor.execute(ExecutionRequest(step="repair"))

            after = {path.name: sha256(path) for path in fixture.bundle.renders_dir.glob("*.png")}
            self.assertEqual(before, after)
            self.assertEqual(
                {f"{order.config_id}.png" for order in plan.work_orders},
                {path.name for path in fixture.repaired_dir.glob("*.png")},
            )

    def test_one_failure_continues_to_later_orders_without_automatic_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            recorder = RecordingImageExecutor(fail_ids={"main_02"})
            _fixture, _plan, image_executor, _journal, executor = self._build(Path(tmp), recorder=recorder)

            result = executor.execute(ExecutionRequest(step="repair"))
            called_ids = [call.payload.output_path.stem for call in image_executor.calls]

            self.assertEqual(1, called_ids.count("main_02"))
            self.assertIn("main_05", called_ids)
            self.assertIn("detail_06", called_ids)
            self.assertEqual(("main_02",), result.metadata["failed"])

    def test_all_success_writes_step_succeeded_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _fixture, _plan, _image_executor, journal, executor = self._build(Path(tmp))

            result = executor.execute(ExecutionRequest(step="repair"))
            events = read_events(journal)

            self.assertEqual("succeeded", result.metadata["status"])
            self.assertEqual("step_succeeded", events[-1]["event"])
            self.assertEqual("repair", events[-1]["step"])

    def test_partial_failure_writes_completed_with_failures_and_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            recorder = RecordingImageExecutor(fail_ids={"main_02", "detail_04"})
            _fixture, _plan, _image_executor, journal, executor = self._build(Path(tmp), recorder=recorder)

            result = executor.execute(ExecutionRequest(step="repair"))
            events = read_events(journal)

            self.assertEqual("completed_with_failures", result.metadata["status"])
            self.assertEqual(("main_02", "detail_04"), result.metadata["failed"])
            self.assertEqual("step_completed_with_failures", events[-1]["event"])
            self.assertEqual(["main_02", "detail_04"], events[-1]["failed_config_ids"])

    def test_existing_valid_repaired_is_skipped_without_transport_or_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture, plan, image_executor, _journal, executor = self._build(Path(tmp))
            existing = fixture.repaired_dir / "main_01.png"
            write_png(existing, 10, 10)
            before = sha256(existing)

            result = executor.execute(ExecutionRequest(step="repair"))

            self.assertEqual(before, sha256(existing))
            self.assertNotIn("main_01", [call.payload.output_path.stem for call in image_executor.calls])
            self.assertEqual(("main_01",), result.metadata["skipped"])
            self.assertEqual(len(plan.work_orders) - 1, len(image_executor.calls))

    def test_two_by_three_detail_is_audited_and_padded_under_repaired_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            recorder = RecordingImageExecutor(two_by_three_ids={"detail_05"})
            fixture, _plan, _image_executor, journal, executor = self._build(Path(tmp), recorder=recorder)

            executor.execute(ExecutionRequest(step="repair"))

            from PIL import Image

            with Image.open(fixture.repaired_dir / "detail_05.png") as image:
                self.assertEqual((18, 24), image.size)
            audit = (
                Path(fixture.bundle.manifest["workspace"]["root"])
                / "artifacts"
                / "audit"
                / "repaired"
                / "render_originals"
                / "detail_05.png"
            )
            with Image.open(audit) as image:
                self.assertEqual((16, 24), image.size)
            self.assertIn("repair_auto_padded", [event["event"] for event in read_events(journal)])

    def test_existing_lock_rejects_without_image_calls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture, _plan, image_executor, _journal, executor = self._build(Path(tmp))
            lock = fixture.repaired_dir / ".repair.lock"
            lock.write_text("already running", encoding="utf-8")

            with self.assertRaises(ExecutorExecutionError):
                executor.execute(ExecutionRequest(step="repair"))

            self.assertEqual([], image_executor.calls)
            self.assertTrue(lock.is_file())


if __name__ == "__main__":
    unittest.main()
