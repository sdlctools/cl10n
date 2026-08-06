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

import importlib.util
import os
import sys


def _load_sibling(name: str, path: str):
    """Import a module from an explicit file path, once, by cache key.

    Inlined in each connector rather than shared, because a shared helper would
    itself have to be imported by name — the problem this solves.
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
# `prompt`, `groq_api` live in app/ as bare modules (the repo's
# scripts-in-a-directory convention). `_HERE` is deliberately NOT put on the
# path: this file is `cl10n/providers/groq.py` and the PyPI `groq` library
# share the top-level name `groq`, so a providers dir on `sys.path` would make
# `import groq` below find *this* connector instead of the library — a circular
# import that only shows up at first use.
sys.path[:0] = [_APP]

import groq  # noqa: E402  (the groq PyPI library — exception taxonomy)
import groq_api  # noqa: E402  (app/ — DEFAULT_MODEL for the connector fallback)

# `base` is loaded from its file path rather than by name, so this works
# identically however the connector was reached: `python3 cl10n/queue_runner.py`
# (no package), the registry's `cl10n.providers.groq` import, or pytest
# collection. Importing by name would need the providers dir on the path, which
# is exactly what the `groq` collision above forbids.
_base = _load_sibling("base", os.path.join(_HERE, "base.py"))
Failure = _base.Failure
_message = _base._message
_retry_after = _base._retry_after
_base_classify = _base.classify
extract_translation = _base.extract_translation


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
