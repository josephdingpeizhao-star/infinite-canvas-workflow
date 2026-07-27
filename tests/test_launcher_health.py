import io
import json
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from launcher.orchestrator import ServiceSpec, make_http_health_checker


class LauncherHealthProbeTests(unittest.TestCase):
    def test_http_503_is_a_real_response_and_preserves_worker_status(self):
        body = {
            "workers": {
                "style_reference_intake": {
                    "status": "waiting_canvas",
                    "lastStatusAt": 1,
                }
            }
        }
        error = urllib.error.HTTPError(
            "http://127.0.0.1:17373/workbench-health",
            503,
            "Service Unavailable",
            {},
            io.BytesIO(json.dumps(body).encode("utf-8")),
        )
        spec = ServiceSpec(
            name="workbench",
            label="画布工作台服务",
            command=("python", "serve"),
            cwd=Path(r"C:\dp01"),
            ports=(17373,),
            health_url="http://127.0.0.1:17373/workbench-health",
            expected_statuses=(200,),
            identity_marker_groups=(("python", "serve"),),
            environment={},
            critical_workers=("style_reference_intake",),
        )

        with patch("launcher.orchestrator.urllib.request.urlopen", side_effect=error):
            probe = make_http_health_checker(0.1)(spec)

        self.assertTrue(probe.responded)
        self.assertEqual(probe.status, 503)
        self.assertEqual(
            probe.body["workers"]["style_reference_intake"]["status"],
            "waiting_canvas",
        )


if __name__ == "__main__":
    unittest.main()
