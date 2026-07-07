from __future__ import annotations

import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from weather_quant import runtime_logs
from weather_quant.llm import (
    LlmSummaryError,
    OpenAILlmSummaryClient,
    compact_result_for_llm,
    extract_response_text,
)


class LlmSummaryTest(unittest.TestCase):
    def test_compact_ensemble_signal_omits_large_member_and_chart_payloads(self) -> None:
        compact = compact_result_for_llm(
            "ensemble-signal",
            {
                "summary": {
                    "cityName": "Ankara",
                    "targetDate": "2026-07-08",
                    "memberCount": 51,
                    "empiricalMean": 26.9,
                },
                "probabilities": [
                    {"bucketLabel": "25", "probability": 0.20, "hitCount": 10, "totalMembers": 51},
                    {"bucketLabel": "26", "probability": 0.50, "hitCount": 25, "totalMembers": 51},
                ],
                "signals": [
                    {"outcome": "26", "signalScore": 90, "edge": 0.12},
                    {"outcome": "25", "signalScore": 40, "edge": -0.02},
                ],
                "members": [{"memberId": str(index), "value": index} for index in range(51)],
                "chart": {"bucketLabels": ["25", "26"], "memberValues": [1, 2, 3]},
            },
        )

        self.assertEqual(compact["type"], "ensemble-signal")
        self.assertNotIn("members", compact)
        self.assertNotIn("chart", compact)
        self.assertEqual(compact["highestProbabilityBuckets"][0]["bucketLabel"], "26")
        self.assertEqual(compact["topSignals"][0]["outcome"], "26")

    def test_compact_portfolio_includes_worst_scenarios(self) -> None:
        compact = compact_result_for_llm(
            "portfolio",
            {
                "summary": {"recommendation": "HEDGE", "currentCost": 100},
                "scenarios": [
                    {"outcome": "25", "probability": 0.10, "netPnl": -20},
                    {"outcome": "26", "probability": 0.50, "netPnl": 30},
                ],
            },
        )

        self.assertEqual(compact["type"], "portfolio")
        self.assertEqual(compact["worstScenarios"][0]["outcome"], "25")
        self.assertEqual(compact["likelyScenarios"][0]["outcome"], "26")

    def test_extract_response_text_reads_output_text(self) -> None:
        self.assertEqual(
            extract_response_text({"output_text": "姜楠，信号偏强。"}),
            "姜楠，信号偏强。",
        )

    def test_missing_api_key_raises_clear_error(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            client = OpenAILlmSummaryClient(api_key="")

        with self.assertRaisesRegex(LlmSummaryError, "OPENAI_API_KEY"):
            client.summarize(kind="portfolio", result={"summary": {}})

    def test_client_reads_env_file_before_environment_lookup(self) -> None:
        def fake_load_dotenv() -> dict[str, str]:
            os.environ["OPENAI_API_KEY"] = "sk-from-env-file"
            os.environ["OPENAI_MODEL"] = "gpt-from-env-file"
            return {
                "OPENAI_API_KEY": "sk-from-env-file",
                "OPENAI_MODEL": "gpt-from-env-file",
            }

        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch("weather_quant.llm.load_dotenv", side_effect=fake_load_dotenv),
        ):
            client = OpenAILlmSummaryClient()

        self.assertEqual(client.api_key, "sk-from-env-file")
        self.assertEqual(client.model, "gpt-from-env-file")

    def test_openai_request_failure_writes_daily_log_without_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as logdir:
            client = OpenAILlmSummaryClient(api_key="sk-test-secret", model="gpt-test")
            with (
                mock.patch.object(runtime_logs, "LOG_DIR", Path(logdir)),
                mock.patch(
                    "weather_quant.llm.urllib.request.urlopen",
                    side_effect=urllib.error.URLError("network down"),
                ),
            ):
                with self.assertRaisesRegex(LlmSummaryError, "request failed"):
                    client.summarize(kind="portfolio", result={"summary": {}})
            log_files = list(Path(logdir).glob("*.log"))
            log_text = "\n".join(path.read_text(encoding="utf-8") for path in log_files)
            log_entries = [
                json.loads(line)
                for path in log_files
                for line in path.read_text(encoding="utf-8").splitlines()
            ]

        self.assertNotIn("sk-test-secret", log_text)
        self.assertTrue(
            any(
                entry["source"] == "external_api"
                and entry["details"]["provider"] == "openai"
                and entry["details"]["endpoint"] == "/responses"
                and entry["details"]["model"] == "gpt-test"
                for entry in log_entries
            )
        )


if __name__ == "__main__":
    unittest.main()
