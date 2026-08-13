"""Ollama experiment: slower local inference for zero-credential, zero-cost use.

The development machine has no discrete GPU. Live-tested against a real local Ollama server
(see docs/DEV-PROCESS.md, Unit 8's live-verification notes, 2026-08-11 and 2026-08-13):

- ``smollm2:1.7b`` (capabilities: completion, tools -- no vision) declined a forced
  ``tool_choice`` call outright.
- ``moondream`` (capabilities: completion, vision -- no tools) does not advertise
  tool-calling support in Ollama at all.
- ``hf.co/Qwen/Qwen3-VL-8B-Instruct-GGUF`` (vision AND tools) is CONFIRMED WORKING against
  this package's actual production tool schema and a real Frame -- a complete, valid
  ActionChunk came back through the real SDKPolicy path, first attempt, in 25.4s once the
  model was warm (an earlier cold/loaded attempt against the same strict schema took over a
  minute; a cold model load can push a single decision well past a minute on CPU-only
  hardware -- this is genuinely too slow for the ~1.5s fast-policy cadence AgentLoop uses
  today, which is exactly the "accept the slowness" tradeoff this experiment was framed
  around, not a defect). Pulled via ``ollama pull hf.co/Qwen/Qwen3-VL-8B-Instruct-GGUF``,
  NOT Ollama's own library/registry -- pulling directly from Ollama's registry was hitting an
  unrelated, unresolved upstream Windows bug (401 Unauthorized on every new pull; see
  https://github.com/ollama/ollama/issues/15074) that has nothing to do with this project.
  Models already pulled before that bug appeared keep working; only new registry pulls were
  affected. The Hugging Face pull path (``ollama pull hf.co/<org>/<repo>``) bypasses Ollama's
  registry entirely and was unaffected.

Given this, the default below is a confirmed-working 8B vision+tools model, not a
smaller-but-broken one -- "small" for this experiment turned out to mean "smallest model that
actually does the job," not the smallest model available. It must be pulled before use; this
transport does not pull models itself.
"""

from __future__ import annotations

import importlib
from types import ModuleType
from typing import Any

from player2.agent.openai_transport import OpenAITransport

_DEFAULT_BASE_URL = "http://localhost:11434/v1"
_DEFAULT_MODEL = "hf.co/Qwen/Qwen3-VL-8B-Instruct-GGUF"
_PLACEHOLDER_API_KEY = "ollama"


def _optional_openai() -> ModuleType | None:
    """Load the optional compatibility client without affecting injected-client tests."""
    try:
        return importlib.import_module("openai")
    except ImportError:
        return None


openai = _optional_openai()


class OllamaTransport(OpenAITransport):
    """Use Ollama's OpenAI-compatible chat-completions endpoint."""

    def __init__(
        self,
        *,
        client: Any | None = None,
        model: str = _DEFAULT_MODEL,
        base_url: str = _DEFAULT_BASE_URL,
    ) -> None:
        if client is None:
            if openai is None:
                raise ImportError(
                    "OllamaTransport requires the optional 'openai' package "
                    "(used for its OpenAI-compatible client, not to call OpenAI itself); "
                    "install player2[models] or inject a client"
                )
            client = openai.OpenAI(
                base_url=base_url, api_key=_PLACEHOLDER_API_KEY, max_retries=0
            )
        super().__init__(client=client, model=model)
