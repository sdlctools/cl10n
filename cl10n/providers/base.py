"""Provider-agnostic primitives shared by every connector.

Nothing here imports a provider library (`groq`, `openai`): a connector brings
its own client and its own exception taxonomy, and everything in this module is
the provider-neutral surface the runner and the registry rest on. Keeping it
import-free of provider libs is what lets the registry lazy-import a connector
only when it is resolved, so adding NVIDIA does not drag the `openai` package
into a Groq run.

This module must not import `queue_runner` (the runner imports from here), to
avoid a cycle.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from dataclasses import dataclass
from typing import Protocol

# `utc_now` lives in `l10n_store`, which is stdlib-only (no providers, no
# cycle) — `base` may import it. The timestamp is part of the error record
# the queue schema requires, so `as_error` must produce it identically to
# how the (pre-CLN-1) runner's `Failure` did.
sys.path[:0] = [os.path.dirname(os.path.dirname(os.path.abspath(__file__)))]
from l10n_store import utc_now  # noqa: E402


@dataclass(frozen=True)
class Failure:
    """One attempt's outcome when it wasn't a usable translation.

    `kind` is the queue schema's error kind; `retryable` decides which edge out
    of `failed` the job takes (rate_limit | network | api_error |
    placeholder_lost). `retry_after` carries a provider-stated wait, in
    seconds, when one was given.
    """

    kind: str
    detail: str
    retryable: bool
    retry_after: float | None = None

    def as_error(self) -> dict:
        return {"kind": self.kind, "detail": self.detail, "at": utc_now()}


def _message(exc: BaseException) -> str:
    """A one-line, whitespace-tight message; the type name when blank."""
    return " ".join(str(exc).split()) or type(exc).__name__


def _retry_after(exc) -> float | None:
    """Seconds from a `Retry-After` header, when the provider sent one.

    Only the delta-seconds form is honoured; the HTTP-date form is rare from
    JSON APIs and a bad parse is worse than falling back to our own backoff.
    Works against any exception shape that exposes `.response.headers`,
    which both `groq` and `openai` status errors do (they share httpx).
    """
    response = getattr(exc, "response", None)
    raw = getattr(response, "headers", {}).get("retry-after") if response else None
    if raw is None:
        return None
    try:
        seconds = float(str(raw).strip())
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")


def extract_translation(content: str) -> str:
    """Pull the translation out of the model's reply.

    The shared `TRANSLATION_PROMPT` rule 4 asks for a JSON object, and the model
    obliges — `{"translation": "..."}`. Handing that raw string to the TM
    stores the envelope as if it were the translation. Tolerant by design: a
    bare string, a fenced block, or an object under any of the plausible keys
    all resolve, because a hard parse failure here would reject a translation
    that was actually fine. This is provider-agnostic and lives here so a
    connector never needs to reinvent it.
    """
    text = content.strip()
    candidate = _FENCE.sub("", text).strip()
    try:
        parsed = json.loads(candidate)
    except (ValueError, TypeError):
        return text
    if isinstance(parsed, str):
        return parsed.strip()
    if isinstance(parsed, dict):
        for key in ("translation", "translated_text", "text", "output"):
            value = parsed.get(key)
            if isinstance(value, str):
                return value.strip()
        # A single-key object under an unexpected name is still unambiguous.
        if len(parsed) == 1:
            (value,) = parsed.values()
            if isinstance(value, str):
                return value.strip()
    return text


class Translator(Protocol):
    """What the runner needs from a provider, and nothing else.

    Returning the translated *text* (not a raw completion) is what keeps the
    JSON-envelope handling a connector detail and lets a test stub be three
    lines long — the "no test requires a live API key" property (AC6) falls
    out of the seam being this narrow. Streaming connectors aggregate to a
    final string before returning, so this is always one complete translation,
    never an iterator.
    """

    async def translate(self, prompt: str) -> str: ...


def classify(exc: BaseException) -> Failure:
    """Generic, provider-library-free failure classification.

    Handles only the failure shapes that are the same across every provider:
    asyncio/OS timeouts and connection loss (retryable), and everything else
    as a terminal `api_error`. A connector's own `classify` knows its library's
    exception taxonomy and should call this for the cases below its
    `isinstance` chain (the generic tail), so the runner's "unrecognised ⇒
    terminal" rule is applied identically everywhere.

    The runner defaults to its default provider's `classify` (which extends
    this), and `main()` always injects the resolved provider's `classify`, so
    this function is the shared floor, not a path the runner takes directly in
    production.
    """
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError)):
        return Failure("network", f"request failed: {_message(exc)}", True)
    return Failure("api_error", f"{type(exc).__name__}: {_message(exc)}", False)
