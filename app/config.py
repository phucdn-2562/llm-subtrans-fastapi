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
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "1024"))
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "2"))
API_KEY = os.getenv("SERVICE_API_KEY", "")
