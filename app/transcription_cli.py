"""Run the upstream CLI with a process-local, logging-only Gemini observer."""
import importlib
import json
import logging
import runpy
import sys
from functools import wraps
from pathlib import Path


USAGE_PREFIX = "SUBTRANS_GEMINI_USAGE "


def _field(value: object, name: str) -> object:
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def emit_usage(response: object) -> None:
    """Log numeric metadata only; never serialize the provider response itself."""
    try:
        usage = _field(response, "usage")
        input_tokens = _count(_field(usage, "total_input_tokens"))
        generated_tokens = _count(_field(usage, "total_output_tokens"))
        thought_tokens = _count(_field(usage, "total_thought_tokens"))
        # Interactions reports generated responses and thoughts separately.
        output_tokens = None if generated_tokens is None else generated_tokens + (thought_tokens or 0)
        total_tokens = _count(_field(usage, "total_tokens"))
        if total_tokens is None and input_tokens is not None and output_tokens is not None:
            total_tokens = input_tokens + output_tokens
        print(USAGE_PREFIX + json.dumps({
            "input_tokens": input_tokens, "output_tokens": output_tokens,
            "thought_tokens": thought_tokens or 0, "total_tokens": total_tokens,
        }), flush=True)
    except Exception:
        logging.warning("Gemini usage metadata could not be logged")


def install_observer(client_class: type) -> None:
    """Observe the existing retry method's return value without extra API calls."""
    original = getattr(client_class, "_create_interaction")

    @wraps(original)
    def observed(self: object, *args: object, **kwargs: object) -> object:
        response = original(self, *args, **kwargs)
        emit_usage(response)
        return response

    setattr(client_class, "_create_interaction", observed)


def main() -> None:
    script = Path(sys.argv[1]).resolve()
    sys.argv = sys.argv[1:]
    # Match Python's normal script import path (check_imports is a CLI sibling).
    sys.path.insert(0, str(script.parent))
    try:
        module = importlib.import_module(
            "PySubtrans.Transcription.Providers.Clients.GeminiTranscriptionClient"
        )
        install_observer(module.GeminiTranscriptionClient)
    except Exception:
        logging.warning("Gemini usage observer unavailable; running original transcription CLI")
    runpy.run_path(str(script), run_name="__main__")


if __name__ == "__main__":
    main()
