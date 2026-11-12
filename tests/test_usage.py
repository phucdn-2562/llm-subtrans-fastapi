"""Usage logging tests; no provider requests or credentials required."""
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import config
from app.subtrans import SubtransError, _run
from app.usage import log_usage


class UsageTests(unittest.TestCase):
    def summary(self, content: bytes, rates=("2", "4")) -> dict:
        with patch.dict(config.TOKEN_PRICES_USD_PER_MILLION, {"translation": rates}):
            with self.assertLogs("uvicorn.error.llm_usage", level="INFO") as captured:
                log_usage(io.BytesIO(content), operation="translation", model="test-model",
                          job_id="test-job", returncode=0, elapsed_seconds=1.23)
        return json.loads(captured.records[0].getMessage())

    def test_usage_and_decimal_cost(self):
        value = self.summary(b"INFO Token usage: 100000 in / 50000 out (150000 total)\n")
        self.assertEqual(value["input_tokens"], 100000)
        self.assertEqual(value["output_tokens"], 50000)
        self.assertEqual(value["total_tokens"], 150000)
        self.assertEqual(value["estimated_cost_usd"], "0.4")

    def test_missing_usage_is_not_zero(self):
        value = self.summary(b"Saved subtitles\n")
        self.assertIsNone(value["input_tokens"])
        self.assertIsNone(value["estimated_cost_usd"])
        self.assertEqual(value["usage_source"], "unavailable")

    def test_invalid_or_missing_rates_do_not_break_usage(self):
        for rate in ("", "invalid", "NaN", "Infinity", "-1"):
            with self.subTest(rate=rate):
                value = self.summary(b"Token usage: 10 in / 5 out (15 total)\n", (rate, "4"))
                self.assertEqual(value["total_tokens"], 15)
                self.assertIsNone(value["estimated_cost_usd"])

    def test_reported_cost_separate_from_estimate_and_content_not_logged(self):
        value = self.summary(b"private subtitle and API key\n"
                             b"Token usage: 100000 in / 50000 out (150000 total)\n"
                             b"Translation cost: $0.1234\n")
        self.assertEqual(value["engine_reported_cost_usd"], "0.1234")
        self.assertNotIn("private", json.dumps(value))

    def test_subprocess_success_and_failure_both_log_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            for code in (0, 1):
                command = [sys.executable, "-c",
                           f"print('Token usage: 10 in / 5 out (15 total)'); raise SystemExit({code})"]
                with self.subTest(code=code), self.assertLogs("uvicorn.error.llm_usage") as captured:
                    with patch("app.subtrans.log"):
                        if code:
                            with self.assertRaises(SubtransError) as error:
                                _run(command, Path(directory), {}, operation="translation",
                                     model="test", job_id="job")
                            self.assertEqual(error.exception.code, "ENGINE_EXECUTION_FAILED")
                        else:
                            _run(command, Path(directory), {}, operation="translation",
                                 model="test", job_id="job")
                self.assertEqual(json.loads(captured.records[0].getMessage())["returncode"], code)

    def test_timeout_preserves_error_and_logs_partial_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            command = [sys.executable, "-c",
                       "import time; print('Token usage: 10 in / 5 out (15 total)', flush=True); time.sleep(30)"]
            with patch.object(config, "SUBPROCESS_TIMEOUT_SECONDS", 0.2):
                with self.assertLogs("uvicorn.error.llm_usage") as captured, patch("app.subtrans.log"):
                    with self.assertRaises(SubtransError) as error:
                        _run(command, Path(directory), {}, operation="translation", model="test", job_id="job")
            self.assertEqual(error.exception.code, "ENGINE_TIMEOUT")
            self.assertEqual(json.loads(captured.records[0].getMessage())["total_tokens"], 15)


if __name__ == "__main__":
    unittest.main()
