"""The Anthropic API connector — `Translator` against the metered Messages API.

This is the Anthropic connector reached with an `ANTHROPIC_API_KEY` — the
metered API at platform.claude.com via the `anthropic` SDK's `AsyncAnthropic`
client. The OAuth-flow connector (`anthropic_oauth`) is a separate sub-task
(its own `CLAUDE_CODE_OAUTH_TOKEN`); this one is the straightforward of the
two, and the issue asked for it first.

`cl10n/providers/nvidia.py` is the closest existing shape — the `anthropic`
SDK's exception taxonomy is openai-shaped (a class per status, sharing
httpx), so `classify` mirrors it almost line for line. Three things differ
from every other connector, and each is a real property of the `anthropic`
SDK confirmed against the installed package (`inspect.signature`, not memory):

- **The reply is a list of content blocks, not `choices[0].message.content`.**
  `messages.create` returns a `Message` whose `.content` is a list of blocks;
  text lives on blocks whose `.type == "text"` (a `TextBlock` with a `.text`
  string). Concatenating the text blocks into one string before handing it
  to `base.extract_translation` is the whole flattening step. Passing the
  block list straight through stringifies a Python repr into the translation
  memory, which renders as garbage and passes every other check — this is
  exactly the mistake `mistral.py` documents, and it is why both flatten.
- **`max_tokens` is REQUIRED by `messages.create`, not optional.** It has no
  default in the SDK, so the connector always sends it (a sensible ceiling).
- **There is no `response_format={"type": "json_object"}`.** Anthropic's
  Messages API has no JSON-mode knob; the shared `TRANSLATION_PROMPT` rule 4
  asks for a JSON object, and the tolerant `base.extract_translation`
  unwraps the reply (a bare string, a fenced block, or an object under any
  plausible key all resolve) — the same approach the NVIDIA connector takes,
  for the same reason (a wider model surface where JSON mode is not a given).

The shared `base._retry_after` reads `Retry-After` straight off
`exc.response.headers`, which `APIStatusError` exposes (the SDK sets
`self.response` and `self.status_code` in `__init__`) — so unlike Mistral no
connector-local override of the helper is needed; the shared one works.

The lazy client construction is load-bearing, identical to the other
connectors: `AsyncAnthropic` is built on first use, not at import, so this
module stays importable without `ANTHROPIC_API_KEY` (CI imports connectors to
inspect them). `AsyncAnthropic(api_key=None)` does not raise — it is the
first API call that would fail on a missing key — but building lazily still
keeps a key-free import the documented invariant, and is what the
`_client is None` test asserts.
"""

from __future__ import annotations

import os

from cl10n.providers.base import (
    Failure,
    _message,
    _retry_after,
    extract_translation,
)
from cl10n.providers.base import classify as _base_classify

# Default model for the Anthropic connector. Mirrors `cl10n/providers.toml`
# `[providers.anthropic_api] default_model`. A capable, widely available model;
# overridable via `anthropic_api:<model>` on `--model` (AC1).
DEFAULT_MODEL = "claude-sonnet-5"

# Fallback env var for the API key when the registry does not pass one.
# `providers.toml` is the source of truth (`api_key_env`); this only covers a
# connector constructed directly.
DEFAULT_API_KEY_ENV = "ANTHROPIC_API_KEY"


def _load_anthropic():
    """Import the `anthropic` SDK lazily so the module is importable without it.

    `anthropic` is an optional extra (`cl10n[anthropic_api]`); importing it at
    module scope would drag it into every process that imports this module —
    including a Groq-only run that never routes to Anthropic. The registry
    lazy-imports the connector too, so this is belt-and-braces, but it keeps
    the "import a connector, inspect its config, without talking to any
    provider library" property true.
    """
    import anthropic  # noqa: WPS433  (lazy by design)
    return anthropic


def _flatten(content) -> str:
    """`Message.content` (a list of content blocks) → one string.

    A reply is a list of typed blocks; text lives on blocks whose `.type ==
    "text"`. Concatenate the text blocks; anything without text (a tool-use
    block, a thinking block) contributes nothing. Passing the block list
    straight to `extract_translation` would stringify a Python repr into the
    translation memory, which renders as garbage and passes every other
    check — exactly the mistake `mistral.py` documents, which is why both
    flatten. A plain string (the model returning a single text block as a
    raw string, which the SDK does not but which a test fake might) is passed
    through.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        parts = []
        for block in content:
            text = None
            if getattr(block, "type", None) == "text":
                text = getattr(block, "text", None)
            if text is None and isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
        return "".join(parts)
    return str(content)


def classify(exc: BaseException) -> Failure:
    """Map an `anthropic` exception onto the queue schema's error kinds.

    `anthropic`'s exception taxonomy is structurally identical to `groq`'s
    and `openai`'s (groq's SDK is a fork of openai's, and the `anthropic`
    SDK follows the same openai-derived shape): `RateLimitError` (429),
    `APITimeoutError` / `APIConnectionError` (network), `APIStatusError` with
    `status_code` read from `response.status_code`, and named 4xx subclasses.
    So this mirrors `groq.classify` / `nvidia.classify` line for line; the
    kinds and retryability match exactly, which is what keeps the rate-limit
    gate and retries behaving the same across providers (AC4).

    `base._retry_after` reads down `exc.response.headers`, which
    `APIStatusError` exposes, so the shared helper works unmodified — no
    connector-local override (unlike `mistral.py`, whose `SDKError` hides it).
    """
    anthropic = _load_anthropic()
    if isinstance(exc, anthropic.RateLimitError):
        return Failure("rate_limit", _message(exc), True, _retry_after(exc))
    if isinstance(exc, anthropic.APITimeoutError):
        return Failure("network", f"request timed out: {_message(exc)}", True)
    if isinstance(exc, anthropic.APIConnectionError):
        return Failure("network", _message(exc), True)
    if isinstance(exc, anthropic.APIStatusError):
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


class AnthropicApiTranslator:
    """`Translator` backed by the Anthropic async Messages API."""

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
        # Which env var holds the key. Passed in by the registry from
        # `providers.toml` (`api_key_env`) rather than hardcoded, so renaming
        # the variable is a config change and the connector has one less fact
        # to keep in step.
        self.api_key_env = api_key_env

    @property
    def client(self):
        # Lazy construction (see module docstring): built on first use so the
        # module stays importable without the key set.
        if self._client is None:
            anthropic = _load_anthropic()
            self._client = anthropic.AsyncAnthropic(
                api_key=os.environ.get(self.api_key_env),
            )
        return self._client

    async def translate(self, prompt: str) -> str:
        # `max_tokens` is required by messages.create (no SDK default), so it
        # is always sent. There is no `response_format` knob on the Messages
        # API; the shared prompt asks for a JSON object and the tolerant
        # `extract_translation` unwraps it. (AC3: content blocks are flattened
        # to a string before extraction.)
        message = await self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
        )
        return extract_translation(_flatten(message.content))
