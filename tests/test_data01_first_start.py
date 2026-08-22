from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

import canvas_workbench_service  # noqa: E402
import runtime_roots  # noqa: E402


class FirstStartDataLayoutTest(unittest.TestCase):
    def setUp(self) -> None:
        runtime_roots.reset_data_root_cache_for_tests()

    def tearDown(self) -> None:
        runtime_roots.reset_data_root_cache_for_tests()

    def test_missing_data_root_is_created_before_real_batch_creator_construction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            data_root = base / "brand-new-data-root"
            self.assertFalse(data_root.exists())
            state_root = base / "state"
            pointer_path = base / "diagnostics" / "data-root.json"
            manifest_path = base / "demo.batch_manifest.json"
            manifest_path.write_text(
                json.dumps({"workspace": {"root": str(base / "demo-workspace")}}),
                encoding="utf-8",
            )
            real_creator = canvas_workbench_service.batch_creator.BatchCreator
            real_pointer_writer = runtime_roots.write_pointer_file
            constructed = []

            def build_real_creator(*args, **kwargs):
                self.assertTrue((data_root / "杯类").is_dir())
                self.assertTrue(
                    (data_root / "workflow-runtime" / "manifests").is_dir()
                )
                self.assertTrue(
                    (data_root / "workflow-runtime" / "reports").is_dir()
                )
                creator = real_creator(*args, **kwargs)
                constructed.append(creator)
                return creator

            workbench = mock.Mock()
            with (
                mock.patch.dict(
                    os.environ,
                    {runtime_roots.DATA_ROOT_ENV: str(data_root)},
                    clear=False,
                ),
                mock.patch.object(
                    canvas_workbench_service,
                    "DEFAULT_STATE_ROOT",
                    state_root,
                ),
                mock.patch.object(
                    canvas_workbench_service.runtime_roots,
                    "write_pointer_file",
                    side_effect=lambda: real_pointer_writer(pointer_path),
                ),
                mock.patch.object(
                    canvas_workbench_service.batch_creator,
                    "BatchCreator",
                    side_effect=build_real_creator,
                ),
                mock.patch.object(
                    canvas_workbench_service,
                    "_load_existing_local_agent_token",
                    return_value="test-token",
                ),
                mock.patch.object(
                    canvas_workbench_service.workflow_demo_service,
                    "WorkflowDemoServiceLock",
                    side_effect=lambda _root: contextlib.nullcontext(),
                ),
                mock.patch.object(
                    canvas_workbench_service.workflow_batch_intake_service,
                    "BatchIntakeServiceLock",
                    side_effect=lambda _root: contextlib.nullcontext(),
                ),
                mock.patch.object(
                    canvas_workbench_service.workflow_demo_service,
                    "WorkflowDemoService",
                ),
                mock.patch.object(
                    canvas_workbench_service.workflow_batch_intake_service,
                    "WorkflowBatchIntakeService",
                ),
                mock.patch.object(
                    canvas_workbench_service.workflow_batch_intake_service,
                    "BatchUploadServer",
                ),
                mock.patch.object(
                    canvas_workbench_service.project_deletion_service,
                    "ProjectDeletionService",
                ),
                mock.patch.object(
                    canvas_workbench_service.canvas_readonly_assistant,
                    "CanvasReadonlyAssistant",
                ),
                mock.patch.object(
                    canvas_workbench_service.canvas_command_assistant,
                    "CanvasCommandAssistant",
                ),
                mock.patch.object(
                    canvas_workbench_service.workflow_production_service,
                    "WorkflowProductionService",
                ),
                mock.patch.object(
                    canvas_workbench_service.workflow_style_reference_removal,
                    "WorkflowStyleReferenceRemovalHandler",
                ),
                mock.patch.object(
                    canvas_workbench_service.workflow_style_reference_intake,
                    "WorkflowStyleReferenceService",
                ),
                mock.patch.object(
                    canvas_workbench_service.batch_recycle_service,
                    "BatchRecycleService",
                ),
                mock.patch.object(
                    canvas_workbench_service.workflow_production_http_server,
                    "WorkflowProductionHttpServer",
                ),
                mock.patch.object(
                    canvas_workbench_service,
                    "CanvasWorkbenchService",
                    return_value=workbench,
                ),
                mock.patch("builtins.print"),
            ):
                canvas_workbench_service.cmd_serve_canvas_workbench(
                    manifest_path,
                    0.01,
                )

            self.assertEqual(1, len(constructed))
            self.assertEqual(data_root / "杯类", constructed[0].workspace_parent)
            self.assertTrue((data_root / "杯类").is_dir())
            self.assertTrue((data_root / "workflow-runtime" / "manifests").is_dir())
            self.assertTrue((data_root / "workflow-runtime" / "reports").is_dir())
            pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
            self.assertEqual(str(data_root.resolve()), pointer["dataRoot"])
            workbench.serve_forever.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
