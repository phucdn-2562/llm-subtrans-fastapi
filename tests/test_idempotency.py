"""Mock AI smoke test for duplicate submit safety; run: python -m unittest discover -s tests."""
import time
import unittest
from uuid import uuid4
from fastapi.testclient import TestClient
from app import main


class IdempotencyTests(unittest.TestCase):
    def test_duplicate_post_returns_single_job(self):
        seen = []
        original = main.transcribe
        def fake(source, output, language):
            seen.append(str(output))
            output.write_text('1\n00:00:00,100 --> 00:00:00,500\nTest\n', encoding='utf8')
        main.transcribe = fake
        key = uuid4().hex
        headers = {'Idempotency-Key': key}
        if main.config.API_KEY: headers['x-api-key'] = main.config.API_KEY
        try:
            with TestClient(main.app) as client:
                def submit():
                    return client.post('/v1/transcriptions', files={'file': ('audio.wav', b'test')},
                                       data={'format': 'srt'}, headers=headers)
                a = submit()
                b = submit()
                self.assertEqual(a.status_code, 202, a.text)
                self.assertEqual(b.status_code, 202, b.text)
                self.assertEqual(a.json()['jobId'], b.json()['jobId'])
                for _ in range(100):
                    state = client.get(f"/v1/jobs/{a.json()['jobId']}", headers=headers).json()
                    if state['status'] in ('completed', 'failed'): break
                    time.sleep(.02)
                self.assertEqual(state['status'], 'completed')
                self.assertEqual(len(seen), 1)
        finally:
            main.transcribe = original

if __name__ == '__main__': unittest.main()
