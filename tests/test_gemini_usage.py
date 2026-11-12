"""Regression coverage for Interactions usage without calling Gemini."""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import config
from app.subtrans import _run, transcribe
from app.transcription_cli import USAGE_PREFIX, emit_usage, install_observer
from app.usage import log_usage


class GeminiUsageTests(unittest.TestCase):
    def emit(self, usage: object) -> str:
        with redirect_stdout(io.StringIO()) as captured:
            emit_usage(SimpleNamespace(usage=usage, output_text="private transcript"))
        return captured.getvalue()

    def summarize(self, content: str) -> dict:
        with patch.dict(config.TOKEN_PRICES_USD_PER_MILLION, {"transcription": ("2", "12")}):
            with self.assertLogs("uvicorn.error.llm_usage") as captured:
                log_usage(io.BytesIO(content.encode()), operation="transcription", model="test",
                          job_id="job", returncode=0, elapsed_seconds=1)
        return json.loads(captured.records[0].getMessage())

    def test_accumulates_chunks_and_thinking_cost(self):
        usage = SimpleNamespace(total_input_tokens=1500, total_output_tokens=175,
                                total_thought_tokens=25, total_tokens=1700)
        content = self.emit(usage) + self.emit(usage)
        summary = self.summarize(content)
        self.assertEqual(summary["input_tokens"], 3000)
        self.assertEqual(summary["output_tokens"], 400)
        self.assertEqual(summary["thought_tokens"], 50)
        self.assertEqual(summary["total_tokens"], 3400)
        self.assertEqual(summary["estimated_cost_usd"], "0.0108")
        self.assertEqual(summary["usage_source"], "gemini_response")
        self.assertEqual(summary["observed_requests"], 2)
        self.assertTrue(summary["usage_complete"])
        self.assertNotIn("private", content)

    def test_missing_usage_is_explicit_and_partial_cost_is_not_presented_as_total(self):
        valid = {"total_input_tokens": 100, "total_output_tokens": 20}
        summary = self.summarize(self.emit(valid) + self.emit(None))
        self.assertEqual(summary["input_tokens"], 100)
        self.assertFalse(summary["usage_complete"])
        self.assertEqual(summary["requests_with_usage"], 1)
        self.assertIsNone(summary["estimated_cost_usd"])
        missing = self.summarize(self.emit(None))
        self.assertIsNone(missing["input_tokens"])
        self.assertEqual(missing["usage_source"], "unavailable")

    def test_zero_usage_is_valid_but_invalid_counts_are_not(self):
        summary = self.summarize(self.emit({"total_input_tokens": 0, "total_output_tokens": 0}))
        self.assertEqual(summary["estimated_cost_usd"], "0")
        for value in (True, -1, "123"):
            with self.subTest(value=value):
                summary = self.summarize(self.emit({"total_input_tokens": value, "total_output_tokens": 1}))
                self.assertIsNone(summary["estimated_cost_usd"])

    def test_malformed_marker_does_not_discard_valid_chunks(self):
        content = USAGE_PREFIX + "[]\n" + self.emit({"total_input_tokens": 1, "total_output_tokens": 2})
        summary = self.summarize(content)
        self.assertEqual(summary["input_tokens"], 1)
        self.assertFalse(summary["usage_complete"])

    def test_observer_preserves_arguments_response_identity_and_exceptions(self):
        response = SimpleNamespace(usage=None)
        calls = []

        class Client:
            def _create_interaction(self, client, audio_file):
                calls.append((client, audio_file))
                if audio_file == "error":
                    raise RuntimeError("provider failed")
                return response

        install_observer(Client)
        with redirect_stdout(io.StringIO()) as captured:
            self.assertIs(Client()._create_interaction("sdk", "audio"), response)
            with self.assertRaises(RuntimeError):
                Client()._create_interaction("sdk", "error")
        self.assertEqual(calls, [("sdk", "audio"), ("sdk", "error")])
        self.assertEqual(captured.getvalue().count(USAGE_PREFIX), 1)

    def test_transcribe_routes_only_gemini_through_observer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            (root / "scripts" / "transcribe.py").touch()
            output = root / "original.srt"
            output.touch()
            for provider in ("Gemini", "Other"):
                with self.subTest(provider=provider), patch.object(config, "LLM_SUBTRANS_DIR", root):
                    with patch.object(config, "TRANSCRIPTION_PROVIDER", provider):
                        with patch("app.subtrans._base_env", return_value={}), patch("app.subtrans._run") as run:
                            self.assertEqual(transcribe(root / "old-video.mp4", output, "English"), output)
                command = run.call_args.args[0]
                self.assertEqual(any("transcription_cli.py" in part for part in command), provider == "Gemini")
                self.assertIn("--language", command)
                self.assertIn("--output", command)

    def test_actual_launcher_collects_usage_and_preserves_cli_output(self):
        # Fake only the provider package; exercise the real child launcher and log reader.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root
            for name in ("PySubtrans", "Transcription", "Providers", "Clients"):
                package /= name
                package.mkdir()
                (package / "__init__.py").touch()
            (package / "GeminiTranscriptionClient.py").write_text(
                "class GeminiTranscriptionClient:\n"
                "    def _create_interaction(self, client, audio_file):\n"
                "        return {'usage': {'total_input_tokens': 1500, 'total_output_tokens': 175}}\n"
            )
            script = root / "transcribe.py"
            output = root / "subtitle.srt"
            script.write_text(
                "import sys\nfrom pathlib import Path\n"
                "from PySubtrans.Transcription.Providers.Clients.GeminiTranscriptionClient import GeminiTranscriptionClient\n"
                "assert sys.argv[1] == 'old-video.mp4'\n"
                "client = GeminiTranscriptionClient()\n"
                "for chunk in range(2): client._create_interaction(None, chunk)\n"
                "Path(sys.argv[2]).write_text('original subtitle')\n"
            )
            launcher = Path(__file__).resolve().parents[1] / "app" / "transcription_cli.py"
            command = [sys.executable, str(launcher), str(script), "old-video.mp4", str(output)]
            with patch.dict(config.TOKEN_PRICES_USD_PER_MILLION, {"transcription": ("2", "12")}):
                with self.assertLogs("uvicorn.error.llm_usage") as captured:
                    _run(command, root, os.environ.copy(), operation="transcription", model="test", job_id="job")
            summary = json.loads(captured.records[0].getMessage())
            self.assertEqual(output.read_text(), "original subtitle")
            self.assertEqual(summary["input_tokens"], 3000)
            self.assertEqual(summary["output_tokens"], 350)
            self.assertEqual(summary["estimated_cost_usd"], "0.0102")


if __name__ == "__main__":
    unittest.main()
