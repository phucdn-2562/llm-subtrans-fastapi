# FastAPI Wrapper optimization (based on repomix 2026-11-11)

## Scope

This package overlays only the FastAPI wrapper files; it does NOT include or alter
`vendor/llm-subtrans`, `.venv`, `data`, or the original AI algorithms.
API routes and main success response structures are preserved.

### Changes
- SQLite state persisted to `data/jobs.sqlite3` (states survive restart). Jobs queued/running at restart are marked `failed/interrupted`, **not resumed**.
- Configurable bounded processing (`MAX_WORKERS + MAX_PENDING_JOBS`); overload returns HTTP 429 and `Retry-After: 10`.
- Configurable subprocess timeout; terminate entire POSIX process group and stream combined stdout/stderr into temporary disk-backed log.
- Upload is deleted after job completion/failure. Completed/failed job records and downloaded results expire after `JOB_RETENTION_HOURS`.
- Upload errors clean partial files; subtitle upload has separate size limit.
- Compare API key in constant time; validate language, deduplicate target languages.
- Optional flags `ENABLE_ALIGN`, `ENABLE_DIARIZE` (default true to retain behavior).
- Removed unreachable translation command code.
- Three REST endpoints and polling/download paths remain unchanged.

## Apply to your existing checkout

Backup first, then copy `app/`, and merge new `.env.example` settings into your `.env`.

```bash
cp -a app "app.backup.$(date +%Y%m%d%H%M%S)"
cp -a /path/to/optimized/app/. ./app/
cat /path/to/optimized/.env.example  # copy the new variables to your .env
```

Suggested .env (tune for your machine and API quota):

```dotenv
SERVICE_API_KEY=SET_A_LONG_RANDOM_PRIVATE_TOKEN
MAX_WORKERS=2
MAX_PENDING_JOBS=8
SUBPROCESS_TIMEOUT_SECONDS=1800
JOB_RETENTION_HOURS=24
CLEANUP_INTERVAL_SECONDS=3600
MAX_UPLOAD_MB=1024
MAX_SUBTITLE_MB=10
ENABLE_ALIGN=true
ENABLE_DIARIZE=true
```

Run **one Uvicorn worker per data directory** (SQLite handles persistence,
but the bounded in-process queue and work reservations are not shared across Uvicorn workers).
For this wrapper, do not deploy `uvicorn --workers 2+`, multi-pod replicas against the same SQLite directory, or `--reload` during in-flight jobs.

```bash
source .venv/bin/activate
uvicorn app.main:app --host 127.0.0.1 --port 8100 --workers 1
python -m unittest discover -s tests
```

Only expose externally behind firewall/private networking + valid `SERVICE_API_KEY`.
NestJS should keep its durable BullMQ jobs and upload/result recovery workflow.
Set NestJS network timeouts **long enough for uploads** and poll Python until done; prefer audio to video.

## Behavior / limitations

- Python job state is persistent, but queued/running jobs **are not resumed after crashes**; NestJS should retry at workflow level with idempotency safeguards. Submitting retries blindly may duplicate model calls.
- On restart the wrapper marks in-flight work interrupted. An abrupt crash may leave orphan uploads; retention cleanup deletes old files only.
- No provider retry or parallel translation added: both can alter token spend and rate-limit behavior. Translation of multiple languages remains sequential.
- Existing progress percentages are stages, **not precise model progress**.
- File uploads still use extension-based acceptance and size caps, not a complete malware/content validation pipeline; deploy reverse-proxy size limits and optional ffprobe checks as needed.
- CLI arguments still follow the existing project integration; verify `gemini-subtrans.py`, model identifiers, and provider flags on your machine.
- The service emits subtitle files as before; let NestJS parse the downloaded SRT to your `subtitleTrack.segments[]` contract.
- Disk-based output retention starts at completion/update time; download before the expiry window.
- Only POSIX/Linux is targeted for process-group termination.

## Smoke test (without Gemini)

`python -m unittest discover -s tests -v` mocks AI calls and verifies HTTP submission/status/file delivery. Real model E2E remains to be run on your server with API key and a sample clip.
