"""Bounded, single-process task runner with durable SQLite job records.

Run uvicorn with --workers 1. NestJS/BullMQ should be the durable *queue*;
this wrapper only accepts a bounded amount of work in one Python process.
"""
import json
import logging
import shutil
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import BoundedSemaphore, Lock
from uuid import uuid4

from app import config

log = logging.getLogger(__name__)
_executor = ThreadPoolExecutor(max_workers=max(1, config.MAX_WORKERS), thread_name_prefix="subtrans")
_slots = BoundedSemaphore(max(1, config.MAX_WORKERS) + config.MAX_PENDING_JOBS)
_lock = Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connection():
    conn = sqlite3.connect(config.JOB_DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def init_jobs():
    with _lock, _connection() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL,
            progress INTEGER NOT NULL, stage TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            result_json TEXT, error TEXT
        )""")
        # Migration: existing SQLite databases do not yet contain idempotency_key.
        columns = {row['name'] for row in conn.execute('PRAGMA table_info(jobs)').fetchall()}
        if 'idempotency_key' not in columns:
            conn.execute('ALTER TABLE jobs ADD COLUMN idempotency_key TEXT')
        conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS ix_jobs_idempotency_key ON jobs(idempotency_key) WHERE idempotency_key IS NOT NULL')
        # Work is not resumed after a process restart: make this explicit.
        conn.execute("""UPDATE jobs SET status='failed', stage='interrupted',
            error='Python worker restarted before completion', updated_at=?
            WHERE status IN ('queued','processing')""", (_now(),))


def reserve_slot() -> bool:
    return _slots.acquire(blocking=False)


def release_slot():
    _slots.release()


def create_job(kind: str, payload: dict | None = None) -> str:
    job_id = str(uuid4())
    now = _now()
    with _lock, _connection() as conn:
        conn.execute("""INSERT INTO jobs
            (job_id,kind,status,progress,stage,created_at,updated_at,result_json,error)
            VALUES (?,?, 'queued',0,'queued',?,?,NULL,NULL)""", (job_id, kind, now, now))
    return job_id


def get_job_by_key(key: str):
    with _connection() as conn:
        row = conn.execute('SELECT job_id,kind FROM jobs WHERE idempotency_key=?', (key,)).fetchone()
    return {'jobId': row['job_id'], 'type': row['kind']} if row else None


def create_or_get_job(kind: str, key: str | None) -> tuple[str, bool]:
    if key is None:
        return create_job(kind), True
    job_id = str(uuid4())
    now = _now()
    with _lock, _connection() as conn:
        cursor = conn.execute('''INSERT OR IGNORE INTO jobs
            (job_id,kind,status,progress,stage,created_at,updated_at,result_json,error,idempotency_key)
            VALUES (?,?, 'queued',0,'queued',?,?,NULL,NULL,?)''', (job_id, kind, now, now, key))
        if cursor.rowcount == 1:
            return job_id, True
        row = conn.execute('SELECT job_id,kind FROM jobs WHERE idempotency_key=?', (key,)).fetchone()
        if not row or row['kind'] != kind:
            raise ValueError('Idempotency-Key already used for another task type')
        return row['job_id'], False


def update_job(job_id: str, **fields):
    allowed = {"status", "progress", "stage", "result", "error"}
    if set(fields) - allowed:
        raise ValueError("Unsupported job field")
    mapping = {"result": "result_json"}
    values = {mapping.get(k, k): (json.dumps(v) if k == "result" else v) for k, v in fields.items()}
    values["updated_at"] = _now()
    with _lock, _connection() as conn:
        conn.execute("UPDATE jobs SET " + ",".join(f"{k}=?" for k in values) + " WHERE job_id=?",
                     [*values.values(), job_id])


def get_job(job_id: str):
    with _connection() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    if not row:
        return None
    return {"jobId": row["job_id"], "type": row["kind"],
            "status": row["status"], "progress": row["progress"],
            "stage": row["stage"], "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
            "error": row["error"]}


def submit(fn, *args):
    """Caller must have reserved one slot; wrapper releases it on completion."""
    def wrapped():
        try:
            fn(*args)
        except Exception:
            log.exception("Unhandled job exception")
        finally:
            release_slot()
    try:
        _executor.submit(wrapped)
    except Exception:
        release_slot()
        raise


def cleanup_expired():
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=config.JOB_RETENTION_HOURS)).isoformat()
    with _lock, _connection() as conn:
        rows = conn.execute("""SELECT job_id FROM jobs
            WHERE status IN ('completed','failed') AND updated_at < ?""", (cutoff,)).fetchall()
        for row in rows:
            job_id = row["job_id"]
            output = config.OUTPUT_DIR / job_id
            if output.is_dir() and not output.is_symlink():
                shutil.rmtree(output, ignore_errors=True)
            conn.execute("DELETE FROM jobs WHERE job_id=?", (job_id,))
    # Also remove abandoned upload files from a worker that died mid-request.
    active_cutoff = datetime.now(timezone.utc) - timedelta(hours=config.JOB_RETENTION_HOURS)
    for upload in config.UPLOAD_DIR.iterdir():
        try:
            if upload.is_file() and not upload.is_symlink() and datetime.fromtimestamp(upload.stat().st_mtime, timezone.utc) < active_cutoff:
                upload.unlink(missing_ok=True)
        except OSError:
            log.exception("Could not remove expired upload")
    return len(rows)


init_jobs()
