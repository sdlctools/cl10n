"""The Anthropic OAuth connector — translate on a Claude Code subscription.

This is the first connector whose transport is **not an HTTP client**.
`claude_agent_sdk.query()` spawns the Claude Code CLI as a **subprocess** and
streams messages back over its stdout; there is no `base_url`, no
`api_key=` constructor argument, and no request object to read a
`Retry-After` header off. If you are adding a non-HTTP provider, this is the
module to copy — `groq.py`/`nvidia.py`/`mistral.py` will all mislead you.

Read `cl10n/PROVIDERS.md` §2 "Do not assume your SDK looks like groq's"
first. Everything below was read off the installed package (0.2.132), not a
README, because every one of these is a rejected job if guessed:

WHAT THIS SDK ACTUALLY DOES

- **`query()` is an async generator**, keyword-only:
  `query(*, prompt, options=None, transport=None)`. It yields message objects
  — `AssistantMessage`, `ResultMessage`, `SystemMessage`, `RateLimitEvent` —
  rather than returning one completion. `translate` aggregates the
  `TextBlock`s of the `AssistantMessage`(s) into one string, which the
  `Translator` protocol explicitly permits ("a streaming connector aggregates
  to a final string before returning"), so it still returns one complete
  translation and never an iterator.

- **The typed exception taxonomy is partly erased in flight.**
  `CLINotFoundError` is raised from `transport.connect()` on the main code
  path and arrives at `translate` with its real type. But a failure *during*
  the stream (the CLI dying, the pipe breaking) is caught by the SDK's read
  task, re-sent through the message channel as a `{"type": "error"}` frame,
  and re-raised by `receive_messages()` as a **bare `Exception(text)`** — the
  `ProcessError`/`CLIConnectionError` class is gone by the time it reaches us.
  So `classify` matches `type(exc) is Exception` *exactly* and calls it a
  retryable `network` failure: by elimination, a bare Exception escaping this
  SDK mid-stream is a transport or process failure. A genuine bug in this
  connector still surfaces as its own type (`AttributeError`, `TypeError`, …)
  and stays terminal, which is the property that rule must not break.

- **`CLINotFoundError` subclasses `CLIConnectionError`**, so it is matched
  *first*. Reverse the two and a missing CLI is classified retryable, and the
  runner then fails three times slower to reach the same place.

- **Rate limits are not HTTP 429s.** The subscription's usage limit surfaces
  *in the stream*: a `RateLimitEvent` whose `rate_limit_info.status` is
  `"rejected"`, or an `AssistantMessage.error` of `"rate_limit"`. Both are
  yielded, not raised, so `translate` raises `_StreamError` to put them back
  on the exception path where `classify` — and therefore the runner's
  account-wide `RateLimitGate` — can see them. `RateLimitInfo.resets_at` is a
  **Unix timestamp**, not a duration, so it is converted to seconds-from-now;
  when absent, `retry_after` is `None` and the runner's own exponential
  backoff carries the wait, exactly as the issue specifies.

- **No `response_format`, no JSON mode.** Like `nvidia.py`, this relies on the
  shared prompt plus `base.extract_translation`.

- **The turn is constrained** so the model answers instead of starting an
  agent loop: `allowed_tools=[]` (no tools at all) and `max_turns=1`.

THE `ANTHROPIC_API_KEY` SHADOWING GUARD — AND WHY IT FAILS RATHER THAN SCRUBS

If `ANTHROPIC_API_KEY` is set, the Claude Code CLI bills **the metered API
key instead of the subscription**, silently. The CLI inherits our environment:
the SDK builds the subprocess env as `{**os.environ, …, **options.env}`.

The issue permits either failing loudly or scrubbing the variable from the
subprocess environment. **This connector fails loudly**, for a mechanical
reason: `options.env` is a `dict[str, str]` that is merged *over* the
inherited environment, so it can override a variable but cannot *delete* one.
Mapping it to `""` is not a deletion either — it is an empty credential, and
whether the CLI treats that as "absent" or as "present but invalid" is its
choice, not ours, and it is free to change it. A guard that might silently
stop working is worse than no guard, because the failure it prevents is an
invisible mis-billing.

The local trap this defends against is real and specific: `anthropic-api-creds.txt`
exists in this same repository for the sibling `anthropic_api` provider, and
the registry's `load_creds_file` uses `os.environ.setdefault` — so an
`ANTHROPIC_API_KEY` exported in an earlier shell session survives into an
OAuth run and is invisible at the call site.

CONCURRENCY IS SUBPROCESS-SHAPED

`-c 8` here means eight Claude Code processes, each with its own Node runtime
— a different cost profile from eight in-flight HTTP requests. The ceiling is
expressed **inside this connector** (`MAX_CONCURRENCY`, enforced by a
module-level semaphore) rather than by teaching the runner this provider's
name, which spec §7 invariant 2 forbids. The runner still opens the slots it
was asked for; this connector simply does not use more than its own ceiling.

The client is lazy in the sense that matters here: constructing the translator
spawns no subprocess, makes no network call, imports no SDK and needs no
token. The first `translate` call is the first time any of that happens.
"""

from __future__ import annotations

import asyncio
import os
import time

from cl10n.providers.base import Failure, _message, extract_translation
from cl10n.providers.base import classify as _base_classify

# Default model for the OAuth connector. Mirrors `cl10n/providers.toml`
# `[providers.anthropic_oauth] default_model`. A bare Claude model id — the
# `anthropic_oauth:` prefix is *our* routing syntax and is stripped by the
# registry before it ever reaches the CLI.
DEFAULT_MODEL = "claude-sonnet-5"

# Fallback env var when the registry does not pass one. `providers.toml` is the
# source of truth (`api_key_env`). Note this names an OAuth **token**, not an
# API key — see the comment on the TOML entry.
DEFAULT_API_KEY_ENV = "CLAUDE_CODE_OAUTH_TOKEN"

# The env var that silently re-bills an OAuth run to the metered API.
SHADOWING_ENV = "ANTHROPIC_API_KEY"

# How many Claude Code subprocesses this connector will run at once, whatever
# `-c` the runner was given. Each job here is a Node process, not a socket.
# Expressed here rather than in the runner: spec §7 invariant 2 — the runner
# must never learn a provider's name.
MAX_CONCURRENCY = 4

# One semaphore per process, created on first use (asyncio objects must not be
# built at import time — they bind to the running loop).
_semaphore: asyncio.Semaphore | None = None
_semaphore_loop: asyncio.AbstractEventLoop | None = None


def _gate() -> asyncio.Semaphore:
    """The process-wide subprocess ceiling, bound to the running loop.

    Rebuilt when the loop changes so a second `asyncio.run` (every test does
    this) does not await a semaphore owned by a closed loop.
    """
    global _semaphore, _semaphore_loop
    loop = asyncio.get_running_loop()
    if _semaphore is None or _semaphore_loop is not loop:
        _semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
        _semaphore_loop = loop
    return _semaphore


class ApiKeyShadowError(RuntimeError):
    """`ANTHROPIC_API_KEY` is set, so an OAuth run would bill the API instead.

    Terminal by construction: retrying cannot unset an environment variable,
    and the whole point of the guard is that the run must not proceed.
    """


class StreamError(RuntimeError):
    """An error the SDK *yielded* rather than raised.

    `AssistantMessage.error` and a rejected `RateLimitEvent` are in-stream
    signals. Wrapping them in an exception is what puts them back on the path
    `classify` (and therefore the runner's retry logic and rate-limit gate)
    can act on. `kind` is the SDK's own literal; `retry_after` is seconds when
    the SDK stated a reset time.
    """

    def __init__(self, kind: str, detail: str, retry_after: float | None = None):
        super().__init__(detail)
        self.kind = kind
        self.retry_after = retry_after


def _load_sdk():
    """Import `claude_agent_sdk` lazily so this module imports without it.

    The SDK is an optional extra (`markdown-localization[anthropic_oauth]`), so a Groq run —
    and the whole test suite — must be able to import this connector with the
    package absent. Every use is funnelled through here for that reason.
    """
    import claude_agent_sdk  # noqa: WPS433  (lazy by design)

    return claude_agent_sdk


def _resets_in(resets_at) -> float | None:
    """`RateLimitInfo.resets_at` (a Unix timestamp) → seconds from now.

    The runner floors its backoff with whatever we return, so a stale or
    nonsensical value must become `None` rather than a negative or absurd
    wait. A reset already in the past means "try now", which is the runner's
    own backoff, i.e. `None`.
    """
    if resets_at is None:
        return None
    try:
        remaining = float(resets_at) - time.time()
    except (TypeError, ValueError):
        return None
    return remaining if remaining > 0 else None


def classify(exc: BaseException) -> Failure:
    """Map a `claude_agent_sdk` failure onto the queue schema's error kinds.

    The kinds are not negotiable (PROVIDERS.md §2): the runner's retries and
    its account-wide rate-limit gate key off them, so they must mean the same
    thing here as for every HTTP provider.

    | condition | kind | retryable |
    | --- | --- | --- |
    | subscription / usage limit, billing | `rate_limit` | yes |
    | process or transport failure mid-stream | `network` | yes |
    | CLI not found, auth failure | `api_error` | **no** |
    | anything unrecognised | `base.classify` (terminal) | no |

    `CLINotFoundError` is matched before `CLIConnectionError` because it is a
    *subclass* of it — the specific case has to win. It is terminal on
    purpose: retrying cannot install the CLI, and its message names the
    remedy so the operator reads "install Claude Code", not "3 attempts
    failed".
    """
    if isinstance(exc, ApiKeyShadowError):
        return Failure("api_error", _message(exc), False)

    if isinstance(exc, StreamError):
        if exc.kind in ("rate_limit", "billing_error"):
            return Failure("rate_limit", _message(exc), True, exc.retry_after)
        if exc.kind == "authentication_failed":
            return Failure("api_error", _message(exc), False)
        if exc.kind == "server_error":
            return Failure("api_error", _message(exc), True)
        # invalid_request, unknown, and anything the SDK adds later.
        return Failure("api_error", _message(exc), False)

    try:
        sdk = _load_sdk()
    except ImportError:
        # The SDK is not installed, so no exception can be one of its types.
        return _base_classify(exc)

    # Specific before general: CLINotFoundError IS a CLIConnectionError.
    if isinstance(exc, sdk.CLINotFoundError):
        return Failure(
            "api_error",
            f"the Claude Code CLI is required by the anthropic_oauth provider "
            f"and was not found — install it (https://claude.ai/install.sh) or "
            f"route to another provider: {_message(exc)}",
            False,
        )
    if isinstance(exc, sdk.ProcessError):
        return Failure("network", f"claude process failed: {_message(exc)}", True)
    if isinstance(exc, sdk.CLIConnectionError):
        return Failure("network", f"claude transport failed: {_message(exc)}", True)
    if isinstance(exc, sdk.CLIJSONDecodeError):
        # A malformed frame is a broken stream, not a broken request.
        return Failure("network", f"malformed CLI output: {_message(exc)}", True)
    if isinstance(exc, sdk.ClaudeSDKError):
        # The remaining members of the SDK's own hierarchy — today that is
        # `MessageParseError`, which is not re-exported at the top level, so it
        # is caught by its public base rather than by reaching into
        # `claude_agent_sdk._errors`. Every one of them describes a stream that
        # went wrong rather than a request that was wrong, so: retryable.
        return Failure("network", f"claude SDK error: {_message(exc)}", True)

    # A bare `Exception` is how the SDK's read task re-raises a mid-stream
    # transport/process failure: the typed class is lost when the error is
    # round-tripped through the message channel (see the module docstring).
    # Matched exactly — a subclass is a real, unrecognised error and must stay
    # terminal, which is what keeps a bug in this connector from being retried.
    if type(exc) is Exception:  # noqa: E721  (exact match is the point)
        return Failure("network", f"claude stream failed: {_message(exc)}", True)

    return _base_classify(exc)


class AnthropicOauthTranslator:
    """`Translator` backed by a Claude Code subscription, over the CLI.

    Constructing this spawns nothing, imports no SDK and needs no token — the
    subprocess is created by the first `translate` call.
    """

    def __init__(
        self,
        model: str | None = None,
        client=None,
        max_turns: int = 1,
        api_key_env: str = DEFAULT_API_KEY_ENV,
    ):
        self.model = model or DEFAULT_MODEL
        # `_client`, here, is the injection seam for tests: a callable with
        # `query()`'s signature. The name matches the other connectors so the
        # registry's "client must be lazy" test (`_client is None`) applies
        # unchanged — there is simply no client object to build in this one.
        self._client = client
        self.max_turns = max_turns
        # Which env var holds the token; passed by the registry from
        # providers.toml rather than hardcoded.
        self.api_key_env = api_key_env

    def _query(self):
        """The SDK's `query`, or the injected fake."""
        if self._client is not None:
            return self._client
        return _load_sdk().query

    def _options(self):
        """`ClaudeAgentOptions` constraining the turn to a single answer.

        `allowed_tools=[]` and `max_turns=1` are what stop the model starting
        an agent loop: this is a translation request, and a tool call or a
        second turn is never the right answer to it.
        """
        sdk = _load_sdk()
        return sdk.ClaudeAgentOptions(
            model=self.model,
            allowed_tools=[],
            max_turns=self.max_turns,
        )

    def _guard_shadowing_api_key(self) -> None:
        """Refuse to run when `ANTHROPIC_API_KEY` would re-bill this to the API.

        See the module docstring for why this fails rather than scrubbing.
        """
        if os.environ.get(SHADOWING_ENV):
            raise ApiKeyShadowError(
                f"{SHADOWING_ENV} is set, which makes the Claude Code CLI bill "
                f"the metered API instead of the subscription this provider "
                f"exists to use. Unset it for this run (it is often left over "
                f"from a sibling anthropic_api run, and the registry's creds "
                f"loader uses setdefault so it survives), or route to "
                f"--provider anthropic_api deliberately."
            )

    async def translate(self, prompt: str) -> str:
        self._guard_shadowing_api_key()

        sdk = _load_sdk()
        query = self._query()
        options = self._options()

        parts: list[str] = []
        async with _gate():
            async for message in query(prompt=prompt, options=options):
                # A rejected usage limit is yielded, not raised — put it back
                # on the exception path so the runner's gate can see it.
                if isinstance(message, sdk.RateLimitEvent):
                    info = message.rate_limit_info
                    if getattr(info, "status", None) == "rejected":
                        raise StreamError(
                            "rate_limit",
                            f"subscription usage limit reached "
                            f"({getattr(info, 'rate_limit_type', None) or 'unknown window'})",
                            _resets_in(getattr(info, "resets_at", None)),
                        )
                    continue

                if isinstance(message, sdk.AssistantMessage):
                    error = getattr(message, "error", None)
                    if error:
                        raise StreamError(error, f"claude reported {error}")
                    for block in message.content:
                        if isinstance(block, sdk.TextBlock):
                            parts.append(block.text)

        return extract_translation("".join(parts))
