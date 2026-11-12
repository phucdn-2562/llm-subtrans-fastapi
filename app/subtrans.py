import os
import signal
import subprocess
import tempfile
import logging
from pathlib import Path

from app import config


class SubtransError(RuntimeError):
    """Safe machine-readable code for job responses; internal details stay in logs."""
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


log = logging.getLogger(__name__)


def _base_env():
    if not config.GEMINI_API_KEY:
        raise SubtransError("GEMINI_NOT_CONFIGURED")
    if not config.LLM_SUBTRANS_DIR.is_dir():
        raise SubtransError("ENGINE_NOT_INSTALLED")
    env = os.environ.copy()
    env["GEMINI_API_KEY"] = config.GEMINI_API_KEY
    return env


def _run(command: list[str], cwd: Path, env: dict):
    """Disk-backed logs; terminate subprocess group on timeout (Linux/POSIX)."""
    with tempfile.TemporaryFile(mode="w+b", dir=config.TEMP_DIR) as log:
        proc = subprocess.Popen(
            command, cwd=str(cwd), env=env,
            stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            proc.wait(timeout=config.SUBPROCESS_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as exc:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
            log.error("LLM-Subtrans timed out after %ss", config.SUBPROCESS_TIMEOUT_SECONDS)
            raise SubtransError("ENGINE_TIMEOUT") from exc
        if proc.returncode != 0:
            log.seek(0, os.SEEK_END)
            size = log.tell()
            log.seek(max(0, size - 6000))
            message = log.read().decode("utf-8", errors="replace")
            # Avoid leaking credentials passed as CLI options through tool logs.
            message = message.replace(config.GEMINI_API_KEY, "[REDACTED]") if config.GEMINI_API_KEY else message
            log.error("LLM-Subtrans exited %s: %s", proc.returncode, message[-6000:])
            raise SubtransError("ENGINE_EXECUTION_FAILED")


def transcribe(media_path: Path, output_path: Path, language: str | None = None):
    script = config.LLM_SUBTRANS_DIR / "scripts" / "transcribe.py"
    if not script.is_file():
        raise SubtransError("TRANSCRIPTION_SCRIPT_MISSING")
    command = [
        config.PYTHON_BIN, str(script), str(media_path),
        "--provider", config.TRANSCRIPTION_PROVIDER,
        "--model", config.TRANSCRIPTION_MODEL,
        "--format", output_path.suffix.lstrip("."),
        "--output", str(output_path),
    ]
    if config.ENABLE_ALIGN:
        command.append("--align")
    if config.ENABLE_DIARIZE:
        command.append("--diarize")
    if language:
        command += ["--language", language]
    _run(command, config.LLM_SUBTRANS_DIR, _base_env())
    if not output_path.is_file():
        raise SubtransError("TRANSCRIPTION_OUTPUT_MISSING")
    return output_path


def translate(subtitle_path: Path, output_path: Path, target_language: str):
    script = config.LLM_SUBTRANS_DIR / "scripts" / "gemini-subtrans.py"
    if not script.is_file():
        raise SubtransError("TRANSLATION_SCRIPT_MISSING")
    command = [
        config.PYTHON_BIN, str(script),
        "--model", config.TRANSLATION_MODEL,
        "--target-language", target_language,
        "-o", str(output_path), str(subtitle_path),
    ]
    _run(command, config.LLM_SUBTRANS_DIR, _base_env())
    if not output_path.is_file():
        raise SubtransError("TRANSLATION_OUTPUT_MISSING")
    return output_path
