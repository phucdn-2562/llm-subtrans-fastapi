"""Verify Gemini SDK requests use the configured gateway without network calls."""
import unittest
from unittest.mock import patch

import httpx
from google import genai
from google.genai import types

from app import config
from app.subtrans import _base_env


class GeminiGatewayTests(unittest.TestCase):
    def test_sdk_routes_translation_and_transcription_requests_to_gateway(self):
        gateway = "https://gateway.example/llm/gemini"
        requests = []

        def capture(request):
            requests.append(request)
            return httpx.Response(400, json={"error": {"message": "test"}})

        with patch.object(config, "GEMINI_API_KEY", "test-key"), \
                patch.object(config, "GEMINI_BASE_URL", gateway):
            env = _base_env()
        with patch.dict("os.environ", env, clear=True):
            client = genai.Client(
                api_key=env["GEMINI_API_KEY"],
                http_options=types.HttpOptions(
                    api_version="v1beta",
                    client_args={"transport": httpx.MockTransport(capture)},
                ),
            )
            with self.assertRaises(Exception):
                client.models.generate_content(model="test-model", contents="test")
            with self.assertRaises(Exception):
                client.interactions.create(model="test-model", input="test")

        self.assertEqual(len(requests), 2)
        for request in requests:
            self.assertTrue(str(request.url).startswith(gateway + "/"))
            self.assertEqual(request.headers["x-goog-api-key"], "test-key")
        self.assertIn("/models/test-model:generateContent", requests[0].url.path)
        self.assertIn("/interactions", requests[1].url.path)


if __name__ == "__main__":
    unittest.main()
