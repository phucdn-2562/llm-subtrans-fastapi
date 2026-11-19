"""Observe Gemini translation response usage without modifying the upstream CLI."""
import importlib
import json
import logging
import runpy
import sys
from functools import wraps
from pathlib import Path

try:
    from .transcription_cli import USAGE_PREFIX, _count, _field
except ImportError:  # Executed as a standalone CLI launcher.
    from transcription_cli import USAGE_PREFIX, _count, _field


def install_observer(client_class: type) -> None:
    original = getattr(client_class, "_process_gemini_response")

    @wraps(original)
    def observed(self: object, response: object, *args: object, **kwargs: object) -> object:
        try:
            usage = _field(response, "usage_metadata")
            input_tokens = _count(_field(usage, "prompt_token_count"))
            generated = _count(_field(usage, "candidates_token_count"))
            thoughts = _count(_field(usage, "thoughts_token_count"))
            total = _count(_field(usage, "total_token_count"))
            # Total includes thinking and other billed output where reported.
            output = total - input_tokens if total is not None and input_tokens is not None and total >= input_tokens else None
            if output is None and generated is not None:
                output = generated + (thoughts or 0)
            if total is None and input_tokens is not None and output is not None:
                total = input_tokens + output
            print(USAGE_PREFIX + json.dumps({"input_tokens": input_tokens, "output_tokens": output,
                  "total_tokens": total, "thought_tokens": thoughts}), flush=True)
        except Exception:
            logging.warning("Gemini usage metadata could not be logged")
        return original(self, response, *args, **kwargs)

    setattr(client_class, "_process_gemini_response", observed)


def main() -> None:
    script = Path(sys.argv[1]).resolve()
    sys.argv = sys.argv[1:]
    sys.path.insert(0, str(script.parent))
    try:
        module = importlib.import_module("PySubtrans.Providers.Clients.GeminiClient")
        install_observer(module.GeminiClient)
    except Exception:
        logging.warning("Gemini usage observer unavailable; running original translation CLI")
    runpy.run_path(str(script), run_name="__main__")


if __name__ == "__main__":
    main()
