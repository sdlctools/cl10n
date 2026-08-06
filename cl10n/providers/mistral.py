"""The Mistral connector — `Translator` backed by the `mistralai` SDK.

Mistral speaks its own SDK rather than an OpenAI-compatible endpoint, so this
connector is the first one that is not a near-copy of `groq.py`. Three things
differ from the other connectors, and each is a real property of the SDK rather
than a style choice:

- **The client is `mistralai.client.Mistral`.** In `mistralai` 2.x the SDK moved
  under `mistralai.client`; `from mistralai import Mistral` raises on this
  version. One client object serves both sync and async — the async call is
  `client.chat.complete_async(...)`, not a separate `AsyncMistral` class.
- **One error class, not a hierarchy.** Where `groq`/`openai` raise
  `RateLimitError`, `AuthenticationError` and friends, Mistral raises a single
  `SDKError` carrying the HTTP status. `classify` therefore branches on
  `status_code` rather than on the exception type, and reads `Retry-After` from
  the error's own `headers` — `SDKError` exposes `raw_response`/`headers` but
  **not** `.response`, so the shared `base._retry_after` finds nothing.
- **`content` is `Union[str, List[ContentChunk]]`.** A reply may arrive as
  chunks rather than a plain string, so it is flattened before the envelope is
  unwrapped. Handing a chunk list to `extract_translation` would stringify a
  repr into the translation memory.

Transport errors surface as raw `httpx` exceptions. Those are **not** `OSError`
subclasses, so `base.classify`'s generic tail would call them terminal; they are
matched explicitly here and classified as retryable `network`.

`response_format={"type": "json_object"}` is sent, as with Groq: Mistral's chat
API supports JSON mode, so the reply shape is guaranteed rather than lucky.

The client is built lazily — `Mistral(api_key=...)` with a missing key would
otherwise make this module unimportable without credentials, including in CI.
"""

from __future__ import annotations

import os

from cl10n.providers.base import Failure, _message, extract_translation
from cl10n.providers.base import classify as _base_classify

# Default model for the Mistral connector. Mirrors `cl10n/providers.toml`
# `[providers.mistral] default_model`.
DEFAULT_MODEL = "mistral-large-latest"

# Fallback env var for the API key when the registry does not pass one.
# `providers.toml` is the source of truth (`api_key_env`).
DEFAULT_API_KEY_ENV = "MISTRAL_API_KEY"


def _load_mistral():
    """Import the SDK lazily so the module is importable without it.

    In `mistralai` 2.x the client lives at `mistralai.client`; the top level is
    a namespace package and `from mistralai import Mistral` fails.
    """
    from mistralai.client import Mistral  # noqa: WPS433  (lazy by design)
    return Mistral


def _retry_after(exc) -> float | None:
    """Seconds from `Retry-After`, read off `SDKError`'s own headers.

    `SDKError` exposes `headers` and `raw_response` but no `.response`, which is
    what `base._retry_after` looks for — so that helper always returns None for
    Mistral and this one replaces it. Only the delta-seconds form is honoured;
    an HTTP-date falls back to our own backoff, as everywhere else.
    """
    headers = getattr(exc, "headers", None)
    if headers is None:
        response = getattr(exc, "raw_response", None)
        headers = getattr(response, "headers", None)
    if not headers:
        return None
    raw = headers.get("retry-after")
    if raw is None:
        return None
    try:
        seconds = float(str(raw).strip())
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def _flatten(content) -> str:
    """`Union[str, List[ContentChunk]]` → one string.

    A chunk list is concatenated over the text-bearing chunks; anything without
    text (an image chunk, say) contributes nothing. Passing the list straight to
    `extract_translation` would store a Python repr as the translation.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        parts = []
        for chunk in content:
            text = getattr(chunk, "text", None)
            if text is None and isinstance(chunk, dict):
                text = chunk.get("text")
            if isinstance(text, str):
                parts.append(text)
        return "".join(parts)
    return str(content)


def classify(exc: BaseException) -> Failure:
    """Map a `mistralai` exception onto the queue schema's error kinds.

    Mistral raises one `SDKError` for every HTTP failure rather than a class per
    status, so this branches on `status_code`. The resulting kinds match the
    other connectors exactly — that is what keeps retries and the account-wide
    rate-limit gate behaving identically whichever provider is running.

    `httpx` transport errors are matched before the generic tail because they
    are not `OSError` subclasses, so `base.classify` would otherwise call a
    connection reset terminal and reject the job on its first failure.
    """
    import httpx

    from mistralai.client import errors as mistral_errors

    if isinstance(exc, mistral_errors.SDKError):
        status = getattr(exc, "status_code", 0) or 0
        # 5xx and 408/409 are "try again"; 401/403/400/404/422 never will be.
        retryable = status >= 500 or status in (408, 409, 429)
        return Failure(
            "rate_limit" if status == 429 else "api_error",
            f"HTTP {status}: {_message(exc)}",
            retryable,
            _retry_after(exc),
        )

    # No response at all — the request went out and nothing came back.
    if isinstance(exc, mistral_errors.NoResponseError):
        return Failure("network", _message(exc), True)

    if isinstance(exc, httpx.TimeoutException):
        return Failure("network", f"request timed out: {_message(exc)}", True)
    if isinstance(exc, httpx.TransportError):
        # ConnectError, ReadError, RemoteProtocolError, … — all retryable, and
        # none of them is an OSError, so the generic tail would miss them.
        return Failure("network", _message(exc), True)

    return _base_classify(exc)


class MistralTranslator:
    """`Translator` backed by the Mistral async chat API."""

    def __init__(
        self,
        model: str | None = None,
        client=None,
        max_tokens: int = 4096,
        api_key_env: str = DEFAULT_API_KEY_ENV,
    ):
        self.model = model or DEFAULT_MODEL
        self._client = client
        self.max_tokens = max_tokens
        # Which env var holds the key; passed in by the registry from
        # providers.toml rather than hardcoded.
        self.api_key_env = api_key_env

    @property
    def client(self):
        # Lazy construction (see module docstring): built on first use so the
        # module stays importable without the key set.
        if self._client is None:
            Mistral = _load_mistral()
            self._client = Mistral(api_key=os.environ.get(self.api_key_env))
        return self._client

    async def translate(self, prompt: str) -> str:
        completion = await self.client.chat.complete_async(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=self.max_tokens,
            response_format={"type": "json_object"},
        )
        return extract_translation(_flatten(completion.choices[0].message.content))
