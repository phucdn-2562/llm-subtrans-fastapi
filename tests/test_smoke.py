"""Run: python -m unittest discover -s tests (without contacting Gemini)."""
import time
import unittest
from fastapi.testclient import TestClient
from app import main


def fake_transcribe(source, output, language):
    output.write_text("1\n00:00:00,500 --> 00:00:01,500\nHello\n", encoding="utf-8")
    return output


def fake_translate(source, output, language):
    output.write_text(source.read_text(encoding="utf-8").replace("Hello", "Bonjour"), encoding="utf-8")
    return output


class APITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original_transcribe = main.transcribe
        cls.original_translate = main.translate
        main.transcribe = fake_transcribe
        main.translate = fake_translate
        cls.client = TestClient(main.app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        main.transcribe = cls.original_transcribe
        main.translate = cls.original_translate

    def wait_completed(self, job_id):
        for _ in range(100):
            value = self.client.get(f"/v1/jobs/{job_id}").json()
            if value["status"] in ("completed", "failed"):
                return value
            time.sleep(.02)
        self.fail("Job took too long")

    def test_transcription_and_download(self):
        r = self.client.post("/v1/transcriptions", files={"file": ("audio.wav", b"fake audio")},
                             data={"sourceLanguage": "English", "format": "srt"})
        self.assertEqual(r.status_code, 202)
        state = self.wait_completed(r.json()["jobId"])
        self.assertEqual(state["status"], "completed")
        self.assertIn("Hello", self.client.get(state["result"]["downloadPath"]).text)

    def test_translation_and_download(self):
        r = self.client.post("/v1/translations", files={"file": ("input.srt", b"Hello")},
                             data={"targetLanguage": "French"})
        self.assertEqual(r.status_code, 202)
        state = self.wait_completed(r.json()["jobId"])
        self.assertEqual(state["status"], "completed")
        self.assertIn("Bonjour", self.client.get(state["result"]["downloadPath"]).text)

    def test_validation(self):
        self.assertEqual(self.client.post("/v1/transcriptions", files={"file": ("a.exe", b"x")}).status_code, 415)
        self.assertEqual(self.client.post("/v1/transcriptions", files={"file": ("a.wav", b"")}).status_code, 422)
        self.assertEqual(self.client.get("/v1/jobs/none").status_code, 404)


if __name__ == "__main__":
    unittest.main()
