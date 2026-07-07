from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from weather_quant import runtime_logs


class RuntimeLogsTest(unittest.TestCase):
    def test_external_api_failure_writes_daily_log_file(self) -> None:
        with tempfile.TemporaryDirectory() as logdir:
            with mock.patch.object(runtime_logs, "LOG_DIR", Path(logdir)):
                runtime_logs.log_external_api_failure(
                    provider="open-meteo",
                    action="fetch_forecast",
                    endpoint="/v1/forecast",
                    details={"model": "ecmwf_ifs025"},
                    error=RuntimeError("temporary eof"),
                )
            log_files = list(Path(logdir).glob("*.log"))
            entries = [
                json.loads(line)
                for path in log_files
                for line in path.read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(len(log_files), 1)
        self.assertRegex(log_files[0].name, r"^\d{4}-\d{2}-\d{2}\.log$")
        self.assertEqual(entries[0]["level"], "ERROR")
        self.assertEqual(entries[0]["source"], "external_api")
        self.assertEqual(entries[0]["details"]["provider"], "open-meteo")
        self.assertEqual(entries[0]["details"]["endpoint"], "/v1/forecast")
        self.assertEqual(entries[0]["details"]["model"], "ecmwf_ifs025")
        self.assertEqual(entries[0]["errorType"], "RuntimeError")
        self.assertIn("temporary eof", entries[0]["error"])


if __name__ == "__main__":
    unittest.main()
