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


class TranslationCompatibilityTests(unittest.TestCase):
    def test_all_language_codes_and_legacy_names(self):
        for code, name in main.TRANSLATION_LANGUAGE_CODES.items():
            self.assertEqual(main.resolve_translation_language(code), name)
            self.assertEqual(main.resolve_translation_language(name), name)
        self.assertIsNone(main.resolve_translation_language("invalid"))

    def test_txt_srt_upload_is_converted_without_changing_content(self):
        from io import BytesIO
        from fastapi import UploadFile
        from pathlib import Path
        import tempfile
        import asyncio

        payload = ("1\n00:00:06,810 --> 00:00:07,290\nCome in\n\n"
                   "2\n00:00:14,220 --> 00:00:16,590\nOur house is the monkey's\n")
        with tempfile.TemporaryDirectory() as directory:
            upload = UploadFile(filename="input.txt", file=BytesIO(payload.encode("utf-8")))
            path = asyncio.run(main.save_upload(upload, Path(directory), main.ALLOWED_SUBTITLES))
            self.assertEqual(path.suffix, ".srt")
            self.assertEqual(path.read_text(encoding="utf-8"), payload)

    def test_plain_txt_is_rejected(self):
        from io import BytesIO
        from fastapi import UploadFile, HTTPException
        from pathlib import Path
        import tempfile
        import asyncio

        with tempfile.TemporaryDirectory() as directory:
            upload = UploadFile(filename="plain.txt", file=BytesIO(b"Hello world"))
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(main.save_upload(upload, Path(directory), main.ALLOWED_SUBTITLES))
            self.assertEqual(ctx.exception.status_code, 422)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_translation_api_accepts_short_code_and_txt(self):
        from unittest.mock import patch
        from uuid import uuid4
        import time
        from fastapi.testclient import TestClient

        body = b"1\n00:00:06,810 --> 00:00:07,290\nHello\n"
        calls = []

        def fake_translate(source, output, language):
            calls.append((source.suffix, language))
            output.write_bytes(source.read_bytes())

        headers = {"Idempotency-Key": uuid4().hex}
        if main.config.API_KEY:
            headers["x-api-key"] = main.config.API_KEY
        with patch.object(main, "translate", side_effect=fake_translate), TestClient(main.app) as client:
            response = client.post("/v1/translations", files={"file": ("example.txt", body)},
                                   data={"targetLanguage": "vi"}, headers=headers)
            self.assertEqual(response.status_code, 202, response.text)
            job_id = response.json()["jobId"]
            for _ in range(100):
                job = client.get(f"/v1/jobs/{job_id}", headers=headers).json()
                if job["status"] in ("completed", "failed"):
                    break
                time.sleep(.02)
            self.assertEqual(job["status"], "completed", job)
            self.assertEqual(calls, [(".srt", "Vietnamese")])
            self.assertTrue(job["result"]["downloadPath"].endswith("translated-vietnamese.srt"))
            downloaded = client.get(job["result"]["downloadPath"], headers=headers)
            self.assertEqual(downloaded.content, body)


if __name__ == "__main__":
    unittest.main()
