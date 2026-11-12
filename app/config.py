from pathlib import Path
import os
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data")).resolve()
UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "outputs"
TEMP_DIR = DATA_DIR / "tmp"

for directory in (UPLOAD_DIR, OUTPUT_DIR, TEMP_DIR):
    directory.mkdir(parents=True, exist_ok=True)

LLM_SUBTRANS_DIR = Path(
    os.getenv("LLM_SUBTRANS_DIR", BASE_DIR / "vendor" / "llm-subtrans")
).resolve()
PYTHON_BIN = os.getenv("PYTHON_BIN", os.sys.executable)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
TRANSLATION_MODEL = os.getenv("TRANSLATION_MODEL", "gemini-2.5-flash")
TRANSCRIPTION_MODEL = os.getenv("TRANSCRIPTION_MODEL", "gemini-3.5-transcribe")
TRANSCRIPTION_PROVIDER = os.getenv("TRANSCRIPTION_PROVIDER", "Gemini")
# Optional rates for the configured models; unset/invalid values disable estimates.
TOKEN_PRICES_USD_PER_MILLION = {
    "translation": (
        os.getenv("TRANSLATION_INPUT_USD_PER_MILLION", ""),
        os.getenv("TRANSLATION_OUTPUT_USD_PER_MILLION", ""),
    ),
    "transcription": (
        os.getenv("TRANSCRIPTION_INPUT_USD_PER_MILLION", ""),
        os.getenv("TRANSCRIPTION_OUTPUT_USD_PER_MILLION", ""),
    ),
}
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "1024"))
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "2"))
API_KEY = os.getenv("SERVICE_API_KEY", "").strip()
ENVIRONMENT = (os.getenv("ENVIRONMENT") or os.getenv("APP_ENV") or os.getenv("NODE_ENV") or "development").strip().lower()
if ENVIRONMENT in {"production", "prod"} and not API_KEY:
    raise RuntimeError("SERVICE_API_KEY must be configured in production")

# Wrapper resource limits (independent of provider/model limits).
MAX_PENDING_JOBS = max(0, int(os.getenv("MAX_PENDING_JOBS", "8")))
SUBPROCESS_TIMEOUT_SECONDS = max(30, int(os.getenv("SUBPROCESS_TIMEOUT_SECONDS", "1800")))
JOB_RETENTION_HOURS = max(1, int(os.getenv("JOB_RETENTION_HOURS", "24")))
CLEANUP_INTERVAL_SECONDS = max(60, int(os.getenv("CLEANUP_INTERVAL_SECONDS", "3600")))
MAX_SUBTITLE_MB = max(1, int(os.getenv("MAX_SUBTITLE_MB", "10")))
ENABLE_ALIGN = os.getenv("ENABLE_ALIGN", "true").lower() == "true"
ENABLE_DIARIZE = os.getenv("ENABLE_DIARIZE", "true").lower() == "true"
JOB_DB_PATH = DATA_DIR / "jobs.sqlite3"
