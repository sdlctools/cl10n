"""The Groq connector — `Translator` backed by the `groq` async client.

This is the Groq integration that lived in `cl10n/queue_runner.py` before
CLN-1, moved verbatim behind the existing `Translator` seam. Two things loaded
here, and both are Groq-specific:

- `GroqTranslator` — the async client, the model, `response_format`, and the
  call that produces a completion. The client is built **lazily**: `AsyncGroq`
  raises when `GROQ_API_KEY` is unset, so constructing it at module scope would
  make this module unimportable on a machine without credentials (including
  CI). That property is load-bearing and is preserved here.
- `classify` — maps the `groq` library's exception taxonomy onto the queue
  schema's error kinds, so retries and the rate-limit gate behave the same as
  before. Provider-agnostic cases (timeouts, connection loss, anything
  unrecognised) fall through to `base.classify`.

Behavior is identical to today (AC3): same model default, same lazy client,
same JSON-envelope handling (via `base.extract_translation`), same exception
classification.
"""

from __future__ import annotations


# THE NAME COLLISION, AND WHY IT IS NOW A NON-EVENT.
#
# This module is `cl10n.providers.groq` and the PyPI library is `groq`. Before
# the package existed, both were reachable as the bare top-level name `groq`,
# and the whole file was arranged around keeping them apart: the providers
# directory had to stay off `sys.path` (or `import groq` here found *itself*),
# and `base` had to be loaded from an explicit file path because importing it
# by name would have required exactly that.
#
# Inside a package, Python 3's absolute-import rule settles it: `import groq`
# below is unambiguously the top-level library, and the sibling is only ever
# reachable as `cl10n.providers.groq`. The two names cannot alias. That is
# what makes the imports below ordinary — the collision is resolved by the
# layout, not by any ordering this file has to maintain.
# `test_providers.py` pins it: it asserts the connector got the real library.
import groq  # the groq PyPI library — exception taxonomy

from cl10n.core import groq_api  # DEFAULT_MODEL for the connector fallback
from cl10n.providers.base import (
    Failure,
    _message,
    _retry_after,
    extract_translation,
)
from cl10n.providers.base import classify as _base_classify


def classify(exc: BaseException) -> Failure:
    """Map a `groq` exception onto the queue schema's error kinds (spec §4/§5).

    Retryable: 429, 5xx, request timeouts, connection loss — the failures that
    say "not now" rather than "not ever". Terminal: authentication, malformed
    request, unsupported model or language, and anything unrecognised. An
    unrecognised exception is usually a bug in *this* code, and retrying a bug
    three times only bills for it three times.

    The generic tail (asyncio/OS timeouts, connection loss, the unknown case)
    is delegated to `base.classify` so the runner's "unrecognised ⇒ terminal"
    rule is applied identically by every connector.
    """
    if isinstance(exc, groq.RateLimitError):
        return Failure("rate_limit", _message(exc), True, _retry_after(exc))
    if isinstance(exc, groq.APITimeoutError):
        return Failure("network", f"request timed out: {_message(exc)}", True)
    if isinstance(exc, groq.APIConnectionError):
        return Failure("network", _message(exc), True)
    if isinstance(exc, groq.APIStatusError):
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


class GroqTranslator:
    """`Translator` backed by the Groq async client (AC3 — behavior-identical)."""

    def __init__(self, model: str | None = None, client=None, max_tokens: int = 4096):
        self.model = model or groq_api.DEFAULT_MODEL
        self._client = client
        self.max_tokens = max_tokens

    @property
    def client(self):
        # Lazy construction (see module docstring): `AsyncGroq` raises when
        # GROQ_API_KEY is unset, so the client is built on first use only.
        if self._client is None:
            self._client = groq_api.get_client()
        return self._client

    async def translate(self, prompt: str) -> str:
        completion = await self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=self.max_tokens,
            response_format={"type": "json_object"},
        )
        return extract_translation(completion.choices[0].message.content or "")
