from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from weather_quant.env import load_dotenv


class EnvLoaderTest(unittest.TestCase):
    def test_load_dotenv_supports_export_quotes_and_comments(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "# local config",
                        'export OPENAI_API_KEY="sk-test"',
                        "OPENAI_MODEL=gpt-test # inline comment",
                        "EMPTY_VALUE=",
                    ]
                ),
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {}, clear=True):
                loaded = load_dotenv(env_path)
                self.assertEqual(os.environ["OPENAI_API_KEY"], "sk-test")
                self.assertEqual(os.environ["OPENAI_MODEL"], "gpt-test")
                self.assertEqual(os.environ["EMPTY_VALUE"], "")

        self.assertEqual(loaded["OPENAI_API_KEY"], "sk-test")

    def test_load_dotenv_does_not_override_existing_environment_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            env_path.write_text("OPENAI_MODEL=from-file", encoding="utf-8")
            with mock.patch.dict(os.environ, {"OPENAI_MODEL": "from-shell"}, clear=True):
                loaded = load_dotenv(env_path)
                self.assertEqual(os.environ["OPENAI_MODEL"], "from-shell")

        self.assertEqual(loaded, {})


if __name__ == "__main__":
    unittest.main()
