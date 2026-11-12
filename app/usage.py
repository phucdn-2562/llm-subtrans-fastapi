"""Observe CLI usage summaries without inspecting subtitle/prompt content."""
import json
import logging
import re
from decimal import Decimal, InvalidOperation
from typing import BinaryIO

from app import config
from app.transcription_cli import USAGE_PREFIX


# Inherit Uvicorn's INFO handler so usage is visible with the default launcher.
log = logging.getLogger("uvicorn.error.llm_usage")
TOKEN_SUMMARY = re.compile(r"Token usage: (\d+) in / (\d+) out \((\d+) total\)\s*$")
COST_SUMMARY = re.compile(r"Translation cost: \$(\d+(?:\.\d+)?)\s*$")
TOKENS_PER_MILLION = Decimal(1_000_000)


def _rate(value: str) -> Decimal | None:
    try:
        rate = Decimal(value)
    except InvalidOperation:
        return None
    return rate if rate.is_finite() and rate >= 0 else None


def log_usage(
    output: BinaryIO, *, operation: str, model: str, job_id: str,
    returncode: int | None, elapsed_seconds: float,
) -> None:
    """Emit one summary per invocation, including partial usage on failure.

    CLI token summaries cover translated batches; they may omit rejected/retried
    responses and Gemini thinking tokens. Configured costs are estimates only.
    Missing usage/rates stay null rather than suggesting an unbilled request.
    """
    try:
        input_tokens = output_tokens = total_tokens = None
        reported_cost = None
        observed_requests = 0
        complete_requests = 0
        response_input = response_output = response_total = thought_tokens = 0
        output.seek(0)
        # Keep memory bounded even if the engine emits a very long line.
        while line := output.readline(65536):
            text = line.decode("utf-8", errors="replace")
            if text.startswith(USAGE_PREFIX):
                try:
                    usage = json.loads(text[len(USAGE_PREFIX):])
                except (ValueError, TypeError):
                    usage = {}
                if not isinstance(usage, dict):
                    usage = {}
                observed_requests += 1
                counts = [usage.get(key) for key in ("input_tokens", "output_tokens", "total_tokens")]
                if all(isinstance(count, int) and not isinstance(count, bool) and count >= 0 for count in counts):
                    complete_requests += 1
                    response_input += counts[0]
                    response_output += counts[1]
                    response_total += counts[2]
                    thoughts = usage.get("thought_tokens")
                    if isinstance(thoughts, int) and not isinstance(thoughts, bool) and thoughts >= 0:
                        thought_tokens += thoughts
            if match := TOKEN_SUMMARY.search(text):
                input_tokens, output_tokens, total_tokens = map(int, match.groups())
            if match := COST_SUMMARY.search(text):
                reported_cost = match.group(1)

        if observed_requests:
            input_tokens = response_input if complete_requests else None
            output_tokens = response_output if complete_requests else None
            total_tokens = response_total if complete_requests else None
        usage_complete = complete_requests == observed_requests if observed_requests else input_tokens is not None

        input_rate, output_rate = (
            _rate(value) for value in config.TOKEN_PRICES_USD_PER_MILLION.get(operation, ("", ""))
        )
        estimated_cost = None
        if usage_complete and input_tokens is not None and input_rate is not None and output_rate is not None:
            estimated_cost = str(
                (input_tokens * input_rate + output_tokens * output_rate) / TOKENS_PER_MILLION
            )
        summary = {
            "event": "llm_usage", "job_id": job_id, "operation": operation,
            "model": model, "returncode": returncode,
            "elapsed_seconds": round(elapsed_seconds, 3),
            "input_tokens": input_tokens, "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "usage_source": ("gemini_response" if complete_requests else "unavailable")
            if observed_requests else ("engine_summary" if input_tokens is not None else "unavailable"),
            "usage_complete": usage_complete,
            "observed_requests": observed_requests,
            "requests_with_usage": complete_requests,
            "thought_tokens": thought_tokens if complete_requests else None,
            "currency": "USD", "engine_reported_cost_usd": reported_cost,
            "estimated_cost_usd": estimated_cost,
            "input_usd_per_million": str(input_rate) if input_rate is not None else None,
            "output_usd_per_million": str(output_rate) if output_rate is not None else None,
        }
        log.info("%s", json.dumps(summary, ensure_ascii=True))
    except Exception:
        # Observability must never change the outcome of a translation job.
        log.warning("LLM usage summary unavailable for job %s", job_id)
