from pathlib import Path
from uuid import uuid4
import shutil

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app import config
from app.jobs import create_job, get_job, submit, update_job
from app.subtrans import transcribe, translate, SubtransError

app = FastAPI(
    title="LLM-Subtrans Local API",
    version="0.1.0",
    description="REST wrapper around LLM-Subtrans CLI for local testing.",
)

ALLOWED_MEDIA = {".mp4", ".mkv", ".mov", ".avi", ".mp3", ".wav", ".m4a", ".aac", ".flac", ".webm"}
ALLOWED_SUBTITLES = {".srt", ".vtt", ".ass", ".ssa"}
LANGUAGES = {"Dutch", "English", "French", "German", "Italian", "Portuguese", "Spanish"}

def auth(x_api_key: str | None):
    if config.API_KEY and x_api_key != config.API_KEY:
        raise HTTPException(status_code=401, detail="Invalid service API key")

async def save_upload(file: UploadFile, directory: Path, allowed: set[str]) -> Path:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in allowed:
        raise HTTPException(status_code=415, detail=f"Unsupported file extension: {suffix}")
    destination = directory / f"{uuid4().hex}{suffix}"
    total = 0
    limit = config.MAX_UPLOAD_MB * 1024 * 1024
    with destination.open("wb") as out:
        while chunk := await file.read(1024 * 1024):
            total += len(chunk)
            if total > limit:
                destination.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail=f"File exceeds {config.MAX_UPLOAD_MB} MB")
            out.write(chunk)
    return destination

def _finish_error(job_id: str, exc: Exception):
    update_job(job_id, status="failed", stage="failed", error=str(exc))

def _transcribe_worker(job_id: str, source: Path, language: str | None, fmt: str):
    try:
        update_job(job_id, status="processing", progress=10, stage="transcribing")
        out = config.OUTPUT_DIR / job_id / f"original.{fmt}"
        out.parent.mkdir(parents=True, exist_ok=True)
        transcribe(source, out, language)
        update_job(
            job_id, status="completed", progress=100, stage="completed",
            result={
                "format": fmt,
                "downloadPath": f"/v1/jobs/{job_id}/files/{out.name}",
            }
        )
    except Exception as exc:
        _finish_error(job_id, exc)

def _translate_worker(job_id: str, source: Path, target_language: str):
    try:
        update_job(job_id, status="processing", progress=10, stage="translating")
        out = config.OUTPUT_DIR / job_id / f"translated-{target_language.lower()}{source.suffix}"
        out.parent.mkdir(parents=True, exist_ok=True)
        translate(source, out, target_language)
        update_job(
            job_id, status="completed", progress=100, stage="completed",
            result={
                "targetLanguage": target_language,
                "downloadPath": f"/v1/jobs/{job_id}/files/{out.name}",
            }
        )
    except Exception as exc:
        _finish_error(job_id, exc)

def _process_worker(job_id: str, source: Path, language: str | None, targets: list[str], fmt: str):
    try:
        job_dir = config.OUTPUT_DIR / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        original = job_dir / f"original.{fmt}"
        update_job(job_id, status="processing", progress=5, stage="transcribing")
        transcribe(source, original, language)
        results = []
        for index, target in enumerate(targets):
            progress = 15 + int((index / max(len(targets), 1)) * 80)
            update_job(job_id, progress=progress, stage=f"translating_{target.lower()}")
            translated = job_dir / f"{target.lower()}.{original.suffix.lstrip('.')}"
            translate(original, translated, target)
            results.append({
                "language": target,
                "file": str(translated),
                "downloadPath": f"/v1/jobs/{job_id}/files/{translated.name}",
            })
        update_job(
            job_id, status="completed", progress=100, stage="completed",
            result={
                "original": {
                    "language": language or "auto",
                    "file": str(original),
                    "downloadPath": f"/v1/jobs/{job_id}/files/{original.name}",
                },
                "translations": results,
                "format": fmt,
            }
        )
    except Exception as exc:
        _finish_error(job_id, exc)

class ProcessOptions(BaseModel):
    sourceLanguage: str | None = Field(default=None, description="Spoken language hint, e.g. English")
    targetLanguages: list[str] = Field(default_factory=list)
    format: str = "srt"

@app.get("/health")
def health():
    return {
        "status": "ok",
        "llmSubtransInstalled": config.LLM_SUBTRANS_DIR.exists(),
        "geminiKeyConfigured": bool(config.GEMINI_API_KEY),
    }

@app.post("/v1/transcriptions", status_code=202)
async def create_transcription(
    file: UploadFile = File(...),
    sourceLanguage: str | None = Form(default=None),
    format: str = Form(default="srt"),
    x_api_key: str | None = Header(default=None),
):
    auth(x_api_key)
    if format not in {"srt", "vtt", "ass"}:
        raise HTTPException(status_code=422, detail="format must be srt, vtt or ass")
    source = await save_upload(file, config.UPLOAD_DIR, ALLOWED_MEDIA)
    job_id = create_job("transcription", {})
    submit(_transcribe_worker, job_id, source, sourceLanguage, format)
    return {"jobId": job_id, "status": "queued"}

@app.post("/v1/translations", status_code=202)
async def create_translation(
    file: UploadFile = File(...),
    targetLanguage: str = Form(...),
    x_api_key: str | None = Header(default=None),
):
    auth(x_api_key)
    if targetLanguage not in LANGUAGES:
        raise HTTPException(status_code=422, detail=f"targetLanguage must be one of {sorted(LANGUAGES)}")
    source = await save_upload(file, config.UPLOAD_DIR, ALLOWED_SUBTITLES)
    job_id = create_job("translation", {})
    submit(_translate_worker, job_id, source, targetLanguage)
    return {"jobId": job_id, "status": "queued"}

@app.post("/v1/process", status_code=202)
async def create_full_process(
    file: UploadFile = File(...),
    sourceLanguage: str | None = Form(default=None),
    targetLanguages: str = Form(default=""),
    format: str = Form(default="srt"),
    x_api_key: str | None = Header(default=None),
):
    auth(x_api_key)
    targets = [x.strip() for x in targetLanguages.split(",") if x.strip()]
    invalid = sorted(set(targets) - LANGUAGES)
    if invalid:
        raise HTTPException(status_code=422, detail={"invalidTargetLanguages": invalid, "allowed": sorted(LANGUAGES)})
    if format not in {"srt", "vtt", "ass"}:
        raise HTTPException(status_code=422, detail="format must be srt, vtt or ass")
    source = await save_upload(file, config.UPLOAD_DIR, ALLOWED_MEDIA)
    job_id = create_job("transcribe_and_translate", {})
    submit(_process_worker, job_id, source, sourceLanguage, targets, format)
    return {"jobId": job_id, "status": "queued"}

@app.get("/v1/jobs/{job_id}")
def job_status(job_id: str, x_api_key: str | None = Header(default=None)):
    auth(x_api_key)
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found (demo jobs are held in memory)")
    return job

@app.get("/v1/jobs/{job_id}/files/{filename}")
def download_job_file(job_id: str, filename: str, x_api_key: str | None = Header(default=None)):
    auth(x_api_key)
    candidate = (config.OUTPUT_DIR / job_id / filename).resolve()
    job_root = (config.OUTPUT_DIR / job_id).resolve()
    if job_root not in candidate.parents or not candidate.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(candidate, filename=filename)
