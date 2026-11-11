from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Lock
from uuid import uuid4

executor = ThreadPoolExecutor(max_workers=2)
_jobs = {}
_lock = Lock()

def create_job(kind: str, payload: dict) -> str:
    job_id = str(uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with _lock:
        _jobs[job_id] = {
            "jobId": job_id,
            "type": kind,
            "status": "queued",
            "progress": 0,
            "stage": "queued",
            "createdAt": now,
            "updatedAt": now,
            "payload": payload,
            "result": None,
            "error": None,
        }
    return job_id

def update_job(job_id: str, **fields):
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            return
        job.update(fields)
        job["updatedAt"] = datetime.now(timezone.utc).isoformat()

def get_job(job_id: str):
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            return None
        return {k: v for k, v in job.items() if k != "payload"}

def submit(fn, *args):
    executor.submit(fn, *args)
