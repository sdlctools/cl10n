"""The NVIDIA connector — `Translator` against NVIDIA's OpenAI-compatible API.

NVIDIA's `https://integrate.api.nvidia.com/v1` endpoint speaks the OpenAI
Chat Completions wire format, so the connector uses the `openai` library with
a custom `base_url` rather than a provider-native SDK. It differs from the
Groq connector in three places only:

- the client class (`AsyncOpenAI` vs `AsyncGroq`) and its `base_url`;
- the env var / creds file the key comes from (`NVIDIA_API_KEY`);
- `response_format` is **not** sent. The shared `TRANSLATION_PROMPT` rule 4
  asks the model for a JSON object, and `base.extract_translation` unwraps it
  tolerantly (a bare string, a fenced block, or an object under any plausible
  key all resolve). Groq sends `response_format={"type":"json_object"}` because
  its models reliably support it; NVIDIA's NIM model zoo is wider and not every
  model accepts JSON mode, so the connector relies on the prompt + the tolerant
  extractor instead. A model that returns prose-with-translation still unwraps.

Streaming (AC4 note): the reference NVIDIA example uses `stream=True` and
reads `reasoning_content`, but the connector sends `stream=False` and returns
the aggregated `choices[0].message.content`. That is permitted by the issue
("may stream internally … but must aggregate and return the final content
string") and is what keeps the connector as stubbable as Groq (AC6): a test
fake is a client whose `chat.completions.create` returns one completion
object, identical in shape to the Groq test. Reasoning content, if the model
produces it, sits in a sibling field and is discarded — it is not translation.

The lazy client construction is load-bearing, identical to Groq's:
`AsyncOpenAI` is built on first use, not at import, so this module stays
importable without `NVIDIA_API_KEY` (CI imports connectors to inspect them).
"""

from __future__ import annotations

import importlib.util
import os
import sys


def _load_sibling(name: str, path: str):
    """Import a module from an explicit file path, once, by cache key.

    Inlined in each connector rather than shared, because a shared helper would
    itself have to be imported by name — the problem this solves. The cache key
    is shared with the other connectors, so `base` is executed once per process.
    """
    key = f"_cl10n_providers_{name}"
    if key in sys.modules:
        return sys.modules[key]
    spec = importlib.util.spec_from_file_location(key, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    spec.loader.exec_module(module)
    return module


_HERE = os.path.dirname(os.path.abspath(__file__))
_CL10N = os.path.dirname(_HERE)
_APP = os.path.join(os.path.dirname(_CL10N), "app")
# `app/`'s modules are bare on the path (scripts-in-a-directory convention).
sys.path[:0] = [_APP]

# Loaded from an explicit file path so this resolves identically however the
# connector was reached — script run, registry import, or pytest collection.
_base = _load_sibling("base", os.path.join(_HERE, "base.py"))
Failure = _base.Failure
_message = _base._message
_retry_after = _base._retry_after
_base_classify = _base.classify
extract_translation = _base.extract_translation

# The OpenAI-compatible base URL for NVIDIA's hosted endpoint (AC4).
NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"

# Default model for the NVIDIA connector. Mirrors `cl10n/providers.toml`
# `[providers.nvidia] default_model`. A capable Nemotron model; the model is
# overridable via `nvidia:<model>` on `--model` (AC1).
DEFAULT_MODEL = "nvidia/nemotron-3-ultra-550b-a55b"


def _load_openai():
    """Import `openai` lazily so the module is importable without it.

    `openai` is a runtime dep (requirements.txt), but importing it at module
    scope would pull it into every process that imports this module — including
    a Groq-only run that never resolves NVIDIA. The registry lazy-imports the
    connector too, so this is belt-and-braces, but it keeps the "import a
    connector, inspect its config, without talking to any provider library"
    property true.
    """
    import openai  # noqa: WPS433  (lazy by design)
    return openai


def classify(exc: BaseException) -> Failure:
    """Map an `openai` exception onto the queue schema's error kinds.

    `openai`'s exception taxonomy is structurally identical to `groq`'s
    (groq's SDK is a fork of openai's): `RateLimitError` (429),
    `APITimeoutError` / `APIConnectionError` (network), `APIStatusError` with
    `status_code` read from `response.status_code`, and named 4xx subclasses.
    So this mirrors `groq.classify` line for line; the kinds and retryability
    match exactly, which is what keeps the rate-limit gate and retries behaving
    the same across providers (AC4).
    """
    openai = _load_openai()
    if isinstance(exc, openai.RateLimitError):
        return Failure("rate_limit", _message(exc), True, _retry_after(exc))
    if isinstance(exc, openai.APITimeoutError):
        return Failure("network", f"request timed out: {_message(exc)}", True)
    if isinstance(exc, openai.APIConnectionError):
        return Failure("network", _message(exc), True)
    if isinstance(exc, openai.APIStatusError):
        status = getattr(exc, "status_code", 0) or 0
        # 5xx and 408/409 are "try again"; 401/403/400/404/422 never will be.
        retryable = status >= 500 or status in (408, 409, 429)
        return Failure(
            "rate_limit" if status == 429 else "api_error",
            f"HTTP {status}: {_message(exc)}",
            retryable,
            _retry_after(exc),
        )
    return _base_classify(exc)


class NvidiaTranslator:
    """`Translator` backed by NVIDIA's OpenAI-compatible async endpoint (AC4)."""

    def __init__(
        self,
        model: str | None = None,
        client=None,
        base_url: str = NVIDIA_BASE_URL,
        max_tokens: int = 4096,
    ):
        self.model = model or DEFAULT_MODEL
        self._client = client
        self.base_url = base_url
        self.max_tokens = max_tokens

    @property
    def client(self):
        # Lazy construction (see module docstring): built on first use so the
        # module stays importable without NVIDIA_API_KEY.
        if self._client is None:
            openai = _load_openai()
            self._client = openai.AsyncOpenAI(
                api_key=os.environ.get("NVIDIA_API_KEY"),
                base_url=self.base_url,
            )
        return self._client

    async def translate(self, prompt: str) -> str:
        completion = await self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=self.max_tokens,
        )
        return extract_translation(completion.choices[0].message.content or "")
