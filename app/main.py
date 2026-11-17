"""HTTP compatibility layer for LLM-Subtrans; public response shapes preserved."""
import asyncio
import hmac
import re
import logging
import shutil
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile, Request
from fastapi.responses import FileResponse

from app import config
from app.jobs import (
    cleanup_expired, create_or_get_job, get_job, get_job_by_key, release_slot,
    reserve_slot, submit, update_job,
)
from app.subtrans import SubtransError, transcribe, translate

log = logging.getLogger(__name__)
bridge_log = logging.getLogger('uvicorn.error.llm_bridge')
ALLOWED_MEDIA = {".mp4", ".mkv", ".mov", ".avi", ".mp3", ".wav", ".m4a", ".aac", ".flac", ".webm"}
ALLOWED_SUBTITLES = {".srt", ".vtt", ".ass", ".ssa", ".txt"}
LANGUAGES = {"Dutch", "English", "French", "German", "Italian", "Portuguese", "Spanish"}
FORMATS = {"srt", "vtt", "ass"}

# Translation-only aliases. Keep existing full-name inputs and CLI arguments unchanged.
TRANSLATION_LANGUAGE_CODES = {
    "nl": "Dutch",
    "en": "English",
    "fr": "French",
    "de": "German",
    "it": "Italian",
    "pt": "Portuguese",
    "es": "Spanish",
    "vi": "Vietnamese",
    "th": "Thai",
    "ja": "Japanese",
    "ko": "Korean",
    "zh": "Chinese",
    "ar": "Arabic",
    "hi": "Hindi",
    "id": "Indonesian",
    "ms": "Malay",
    "tr": "Turkish",
    "ru": "Russian",
    "pl": "Polish",
    "sv": "Swedish",
    "da": "Danish",
    "no": "Norwegian",
    "fi": "Finnish",
    "el": "Greek",
    "cs": "Czech",
    "hu": "Hungarian",
    "ro": "Romanian",
    "uk": "Ukrainian",
    "he": "Hebrew",
    "ca": "Catalan",
    "hr": "Croatian",
    "sk": "Slovak",
    "bg": "Bulgarian",
    "fil": "Filipino",
    "bn": "Bengali",
    "ta": "Tamil",
    "ur": "Urdu",
}


def resolve_translation_language(value: str) -> str | None:
    """Accept ISO-style codes or full names; pass full names to LLM-Subtrans."""
    code = value.strip().lower()
    if code in TRANSLATION_LANGUAGE_CODES:
        return TRANSLATION_LANGUAGE_CODES[code]
    for name in TRANSLATION_LANGUAGE_CODES.values():
        if code == name.lower():
            return name
    return None


# TXT is accepted only when its content is already in SRT cue format.
SRT_TXT_HEADER = re.compile(
    r"(?m)^\s*\d+\s*\r?\n\s*\d{2}:\d{2}:\d{2}[,.]\d{3}"
    r"\s*-->\s*\d{2}:\d{2}:\d{2}[,.]\d{3}\s*$"
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    async def janitor():
        while True:
            await asyncio.sleep(config.CLEANUP_INTERVAL_SECONDS)
            try:
                await asyncio.to_thread(cleanup_expired)
            except Exception:
                log.exception("Retention cleanup failed")

    task = asyncio.create_task(janitor())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


app = FastAPI(
    title="LLM-Subtrans Local API",
    version="0.2.0",
    description="Bounded HTTP wrapper around LLM-Subtrans CLI.",
    lifespan=lifespan,
)


@app.middleware("http")
async def observe_bridge_requests(request: Request, call_next):
    """Diagnostic logs only: preserve all responses, error codes and request bodies."""
    start = time.monotonic()
    correlation = request.headers.get("x-correlation-id", "")
    correlation = correlation[:80] if re.fullmatch(r"[A-Za-z0-9_-]{1,80}", correlation) else None
    path = request.url.path  # Deliberately exclude query, secrets and subtitle payload.
    try:
        response = await call_next(request)
    except Exception as exc:
        bridge_log.exception("bridge_http_exception correlationId=%s method=%s path=%s type=%s elapsedMs=%s",
                             correlation, request.method, path, type(exc).__name__,
                             round((time.monotonic() - start) * 1000))
        raise
    # Avoid flooding logs with successful GET /v1/jobs polling.
    if request.method == "POST" or "/files/" in path or response.status_code >= 400:
        bridge_log.info("bridge_http_response correlationId=%s method=%s path=%s status=%s elapsedMs=%s",
                        correlation, request.method, path, response.status_code,
                        round((time.monotonic() - start) * 1000))
    return response


def auth(x_api_key: str | None):
    if config.API_KEY and (not x_api_key or not hmac.compare_digest(x_api_key, config.API_KEY)):
        raise HTTPException(status_code=401, detail="Invalid service API key")


async def save_upload(file: UploadFile, directory: Path, allowed: set[str]) -> Path:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in allowed:
        raise HTTPException(status_code=415, detail=f"Unsupported file extension: {suffix}")
    limit_mb = config.MAX_SUBTITLE_MB if allowed is ALLOWED_SUBTITLES else config.MAX_UPLOAD_MB
    limit = limit_mb * 1024 * 1024
    destination = directory / f"{uuid4().hex}{suffix}"
    total = 0
    try:
        with destination.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > limit:
                    raise HTTPException(status_code=413, detail=f"File exceeds {limit_mb} MB")
                await asyncio.to_thread(out.write, chunk)
        if total == 0:
            raise HTTPException(status_code=422, detail="Empty upload")
        if suffix == ".txt" and allowed is ALLOWED_SUBTITLES:
            # Upstream Gemini subtitle CLI expects an SRT extension. Preserve
            # the original bytes/timestamps; only normalize the filename.
            try:
                text = destination.read_text(encoding="utf-8-sig")
            except UnicodeDecodeError:
                raise HTTPException(status_code=422, detail="TXT must be UTF-8 encoded")
            if not SRT_TXT_HEADER.search(text):
                raise HTTPException(status_code=422, detail="TXT must contain SRT-format numbered cues and timestamps")
            normalized = destination.with_suffix(".srt")
            destination.rename(normalized)
            return normalized
        return destination
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    finally:
        await file.close()


def _finish_error(job_id: str, exc: Exception):
    # Internal diagnostics are redacted before logging and never persisted into SQLite.
    import traceback
    details = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    if config.GEMINI_API_KEY:
        details = details.replace(config.GEMINI_API_KEY, "[REDACTED]")
    if config.API_KEY:
        details = details.replace(config.API_KEY, "[REDACTED]")
    log.error("Job %s failed: %s", job_id, details[-6000:])
    code = exc.code if isinstance(exc, SubtransError) else "INTERNAL_PROCESSING_ERROR"
    update_job(job_id, status="failed", stage="failed", error=code)


def _transcribe_worker(job_id: str, source: Path, language: str | None, fmt: str):
    try:
        update_job(job_id, status="processing", progress=10, stage="transcribing")
        out = config.OUTPUT_DIR / job_id / f"original.{fmt}"
        out.parent.mkdir(parents=True, exist_ok=True)
        transcribe(source, out, language)
        update_job(job_id, status="completed", progress=100, stage="completed", result={
            "format": fmt,
            "downloadPath": f"/v1/jobs/{job_id}/files/{out.name}",
        })
    except Exception as exc:
        _finish_error(job_id, exc)
    finally:
        source.unlink(missing_ok=True)


def _translate_worker(job_id: str, source: Path, target_language: str):
    started = time.monotonic()
    bridge_log.info("translation_worker_start remoteJobId=%s targetLanguage=%s inputBytes=%s",
                    job_id, target_language, source.stat().st_size if source.exists() else None)
    try:
        update_job(job_id, status="processing", progress=10, stage="translating")
        out = config.OUTPUT_DIR / job_id / f"translated-{target_language.lower()}{source.suffix}"
        out.parent.mkdir(parents=True, exist_ok=True)
        translate(source, out, target_language)
        bridge_log.info("translation_worker_engine_done remoteJobId=%s outputBytes=%s elapsedMs=%s",
                        job_id, out.stat().st_size if out.exists() else None,
                        round((time.monotonic() - started) * 1000))
        update_job(job_id, status="completed", progress=100, stage="completed", result={
            "targetLanguage": target_language,
            "downloadPath": f"/v1/jobs/{job_id}/files/{out.name}",
        })
    except Exception as exc:
        bridge_log.error("translation_worker_failed remoteJobId=%s type=%s elapsedMs=%s",
                         job_id, type(exc).__name__, round((time.monotonic() - started) * 1000))
        _finish_error(job_id, exc)
    finally:
        source.unlink(missing_ok=True)


def _process_worker(job_id: str, source: Path, language: str | None, targets: list[str], fmt: str):
    try:
        job_dir = config.OUTPUT_DIR / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        original = job_dir / f"original.{fmt}"
        update_job(job_id, status="processing", progress=5, stage="transcribing")
        transcribe(source, original, language)
        results = []
        for index, target in enumerate(targets):
            update_job(job_id, progress=15 + int(index * 80 / max(len(targets), 1)),
                       stage=f"translating_{target.lower()}")
            translated = job_dir / f"{target.lower()}.{fmt}"
            translate(original, translated, target)
            results.append({
                "language": target,
                "file": str(translated),  # kept for compatibility; internal consumers should use downloadPath
                "downloadPath": f"/v1/jobs/{job_id}/files/{translated.name}",
            })
        update_job(job_id, status="completed", progress=100, stage="completed", result={
            "original": {
                "language": language or "auto", "file": str(original),
                "downloadPath": f"/v1/jobs/{job_id}/files/{original.name}",
            },
            "translations": results,
            "format": fmt,
        })
    except Exception as exc:
        _finish_error(job_id, exc)
    finally:
        source.unlink(missing_ok=True)


async def _accept(file: UploadFile, allowed: set[str], kind: str, fn, *args, key: str | None = None):
    if key is not None:
        if not re.fullmatch(r'[A-Za-z0-9_-]{16,128}', key):
            raise HTTPException(status_code=422, detail='Invalid Idempotency-Key')
        existing = get_job_by_key(key)
        if existing:
            if existing['type'] != kind:
                raise HTTPException(status_code=409, detail='Idempotency-Key already used')
            await file.close()
            return {'jobId': existing['jobId'], 'status': get_job(existing['jobId'])['status']}
    if not reserve_slot():
        raise HTTPException(status_code=429, detail="AI worker queue is full. Retry later.",
                            headers={"Retry-After": "10"})
    transferred = False
    source = None
    try:
        source = await save_upload(file, config.UPLOAD_DIR, allowed)
        try:
            job_id, created = create_or_get_job(kind, key)
        except ValueError:
            raise HTTPException(status_code=409, detail='Idempotency-Key already used')
        if not created:
            source.unlink(missing_ok=True)
            return {'jobId': job_id, 'status': get_job(job_id)['status']}
        # submit() takes ownership of the reserved slot and releases it on completion.
        transferred = True
        try:
            submit(fn, job_id, source, *args)
        except Exception:
            update_job(job_id, status="failed", stage="submit_failed", error="Could not schedule Python worker")
            source.unlink(missing_ok=True)
            raise
        return {"jobId": job_id, "status": "queued"}
    except BaseException:
        if not transferred and source is not None:
            source.unlink(missing_ok=True)
        raise
    finally:
        if not transferred:
            release_slot()


@app.get("/health")
def health():
    return {"status": "ok", "llmSubtransInstalled": config.LLM_SUBTRANS_DIR.is_dir(),
            "geminiKeyConfigured": bool(config.GEMINI_API_KEY)}


@app.post("/v1/transcriptions", status_code=202)
async def create_transcription(
    file: UploadFile = File(...), sourceLanguage: str | None = Form(default=None),
    format: str = Form(default="srt"), x_api_key: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None, alias='Idempotency-Key'),
):
    auth(x_api_key)
    if format not in FORMATS:
        raise HTTPException(status_code=422, detail="format must be srt, vtt or ass")
    if sourceLanguage and sourceLanguage not in LANGUAGES:
        raise HTTPException(status_code=422, detail="Invalid sourceLanguage")
    return await _accept(file, ALLOWED_MEDIA, "transcription", _transcribe_worker, sourceLanguage, format, key=idempotency_key)


@app.post("/v1/translations", status_code=202)
async def create_translation(
    file: UploadFile = File(...), targetLanguage: str = Form(...),
    x_api_key: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None, alias='Idempotency-Key'),
    x_correlation_id: str | None = Header(default=None, alias='X-Correlation-ID'),
):
    auth(x_api_key)
    normalized_language = resolve_translation_language(targetLanguage)
    if normalized_language is None:
        raise HTTPException(status_code=422, detail=f"Unsupported targetLanguage; use one of {sorted(TRANSLATION_LANGUAGE_CODES)}")
    accepted = await _accept(file, ALLOWED_SUBTITLES, "translation", _translate_worker, normalized_language, key=idempotency_key)
    bridge_log.info("translation_accepted correlationId=%s remoteJobId=%s status=%s targetLanguage=%s",
                    (x_correlation_id if x_correlation_id and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", x_correlation_id) else None), accepted["jobId"],
                    accepted["status"], normalized_language)
    return accepted


@app.post("/v1/process", status_code=202)
async def create_full_process(
    file: UploadFile = File(...), sourceLanguage: str | None = Form(default=None),
    targetLanguages: str = Form(default=""), format: str = Form(default="srt"),
    x_api_key: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None, alias='Idempotency-Key'),
):
    auth(x_api_key)
    targets = list(dict.fromkeys(x.strip() for x in targetLanguages.split(",") if x.strip()))
    invalid = sorted(set(targets) - LANGUAGES)
    if invalid:
        raise HTTPException(status_code=422, detail={"invalidTargetLanguages": invalid, "allowed": sorted(LANGUAGES)})
    if sourceLanguage and sourceLanguage not in LANGUAGES:
        raise HTTPException(status_code=422, detail="Invalid sourceLanguage")
    if format not in FORMATS:
        raise HTTPException(status_code=422, detail="format must be srt, vtt or ass")
    return await _accept(file, ALLOWED_MEDIA, "transcribe_and_translate", _process_worker, sourceLanguage, targets, format, key=idempotency_key)


@app.get("/v1/jobs/{job_id}")
def job_status(job_id: str, x_api_key: str | None = Header(default=None)):
    auth(x_api_key)
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.get("/v1/jobs/{job_id}/files/{filename}")
def download_job_file(job_id: str, filename: str, x_api_key: str | None = Header(default=None)):
    auth(x_api_key)
    job = get_job(job_id)
    if not job or job["status"] != "completed":
        raise HTTPException(status_code=404, detail="Job output not available")
    job_root = (config.OUTPUT_DIR / job_id).resolve()
    candidate = (job_root / filename).resolve()
    if job_root not in candidate.parents or not candidate.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(candidate, filename=filename)
