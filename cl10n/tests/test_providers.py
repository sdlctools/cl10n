"""Tests for the pluggable-provider registry and the NVIDIA connector (CLN-1).

The Groq connector's `classify` and `GroqTranslator` are still exercised by
`test_queue_runner.py` (their assertions hold unchanged via the runner's
backward-compat re-exports, AC3). This file covers the *new* behavior:

- the config-driven registry and `--model`/`--provider` routing (AC1, AC2);
- the NVIDIA connector's `classify` (openai exception taxonomy → the same
  `Failure` kinds, AC4) — so retries and the rate-limit gate behave the same;
- the NVIDIA connector constructing without a key (AC6 — the seam stays narrow).

No test imports a provider library at runtime through the connector in a way
that needs a key: `openai` exceptions are built the conftest way, and a
`NvidiaTranslator` is constructed with an injected fake client exactly like
the Groq translator test. `openai` is a real installed dependency (it is in
`requirements.txt`), so importing it for the taxonomy tests is fine; what is
tested is that no *network* call and no *key* is needed.
"""

from __future__ import annotations

import asyncio
import os
import sys

import httpx
import openai
import pytest
from conftest import connection_error, status_error

from cl10n.providers import (
    ProviderConfig,
    Registry,
    build_translator,
    get_classify,
    load_creds_file,
    load_registry,
    resolve_route,
)
from cl10n.providers import nvidia as nvidia_mod
from cl10n.providers.base import _retry_after as _base_retry_after

# The conftest helpers special-case groq's APITimeoutError (request-only ctor)
# but the openai twin is a different class, so build it inline.
_NVIDIA_REQUEST = httpx.Request("POST", "https://integrate.api.nvidia.com/v1/chat/completions")


def _openai_api_timeout():
    return openai.APITimeoutError(request=_NVIDIA_REQUEST)


# --------------------------------------------------------------------------
# Registry + routing (AC1, AC2)
# --------------------------------------------------------------------------


def test_the_registry_loads_every_declared_provider():
    r = load_registry()
    assert r.default == "groq"
    assert set(r.names()) == {"anthropic_api", "groq", "mistral", "nvidia"}


def test_each_provider_declares_its_key_env_and_default_model_ac2():
    r = load_registry()
    groq = r.get("groq")
    assert groq.connector == "groq:GroqTranslator"
    assert groq.api_key_env == "GROQ_API_KEY"
    assert groq.api_key_creds_file == "groq_creds.txt"
    assert groq.base_url is None  # groq library default

    nvidia = r.get("nvidia")
    assert nvidia.connector == "nvidia:NvidiaTranslator"
    assert nvidia.api_key_env == "NVIDIA_NIM_API_KEY"
    assert nvidia.base_url == "https://integrate.api.nvidia.com/v1"
    assert nvidia.default_model  # non-empty

    mistral = r.get("mistral")
    assert mistral.connector == "mistral:MistralTranslator"
    assert mistral.api_key_env == "MISTRAL_API_KEY"
    assert mistral.api_key_creds_file == "mistral-creds.txt"
    assert mistral.base_url is None  # the mistralai SDK targets its own host
    assert mistral.default_model  # non-empty


def test_a_third_provider_is_a_config_change_plus_a_connector_module_ac2(tmp_path):
    """Adding a provider needs no runner change: declare it, resolve it, build it."""
    config = tmp_path / "providers.toml"
    config.write_text(
        'default = "groq"\n\n'
        '[providers.groq]\n'
        'connector = "groq:GroqTranslator"\n'
        'default_model = "openai/gpt-oss-120b"\n'
        'api_key_env = "GROQ_API_KEY"\n\n'
        '[providers.nvidia]\n'
        'connector = "nvidia:NvidiaTranslator"\n'
        'default_model = "nvidia/nemotron-3-ultra-550b-a55b"\n'
        'api_key_env = "NVIDIA_NIM_API_KEY"\n'
        'base_url = "https://integrate.api.nvidia.com/v1"\n'
    )
    r = load_registry(str(config))
    assert "nvidia" in r


@pytest.mark.parametrize("model,provider,exp_provider,exp_model", [
    # AC1: prefix routes and overrides.
    ("nvidia:nvidia/nemotron-3-ultra-550b-a55b", None, "nvidia", "nvidia/nemotron-3-ultra-550b-a55b"),
    ("groq:vendor/specific", None, "groq", "vendor/specific"),
    # AC1: --provider selects; bare model resolves against its default.
    (None, "nvidia", "nvidia", "nvidia/nemotron-3-ultra-550b-a55b"),
    ("nvidia/teeny", "nvidia", "nvidia", "nvidia/teeny"),
    # AC1: nothing set → backward compatible (Groq, default model).
    (None, None, "groq", "openai/gpt-oss-120b"),
    ("bare/model", None, "groq", "bare/model"),
])
def test_resolve_route_ac1(model, provider, exp_provider, exp_model):
    r = load_registry()
    route = resolve_route(r, model=model, provider=provider)
    assert route.provider == exp_provider
    assert route.model == exp_model


def test_an_unknown_provider_prefix_is_rejected():
    r = load_registry()
    with pytest.raises(KeyError):
        resolve_route(r, model="bogus:model")


def test_an_unknown_provider_flag_is_rejected():
    r = load_registry()
    with pytest.raises(KeyError):
        resolve_route(r, provider="bogus")


def test_a_registry_with_a_missing_default_fails_loudly():
    with pytest.raises((ValueError, KeyError)):
        Registry({}, "groq")  # default not declared


# --------------------------------------------------------------------------
# NVIDIA connector — classify (AC4)
# --------------------------------------------------------------------------
#
# `openai`'s taxonomy mirrors `groq`'s (groq's SDK is a fork of openai's), so
# the same matrix the Groq connector is tested with must hold for NVIDIA. The
# point is not that the kinds match Groq's — it is that each openai exception
# lands on the right *Failure* kind with the right retryability, which is what
# the rate-limit gate and retries depend on.

classify = nvidia_mod.classify


@pytest.mark.parametrize("exc,kind,retryable", [
    (status_error(openai.RateLimitError, 429), "rate_limit", True),
    (status_error(openai.InternalServerError, 500), "api_error", True),
    (status_error(openai.InternalServerError, 503), "api_error", True),
    (status_error(openai.AuthenticationError, 401), "api_error", False),
    (status_error(openai.PermissionDeniedError, 403), "api_error", False),
    (status_error(openai.BadRequestError, 400), "api_error", False),
    (status_error(openai.NotFoundError, 404), "api_error", False),
    (status_error(openai.UnprocessableEntityError, 422), "api_error", False),
    (status_error(openai.ConflictError, 409), "api_error", True),
    (connection_error(openai.APIConnectionError), "network", True),
    (_openai_api_timeout(), "network", True),
    (asyncio.TimeoutError(), "network", True),
    (ConnectionResetError("reset by peer"), "network", True),
    (ValueError("bug in our own code"), "api_error", False),
])
def test_nvidia_classify(exc, kind, retryable):
    failure = classify(exc)
    assert failure.kind == kind
    assert failure.retryable is retryable


def test_nvidia_classify_reads_retry_after_from_response_headers():
    exc = status_error(openai.RateLimitError, 429, headers={"retry-after": "12.5"})
    assert classify(exc).retry_after == 12.5


def test_nvidia_classify_ignores_http_date_retry_after():
    exc = status_error(
        openai.RateLimitError, 429,
        headers={"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"},
    )
    assert classify(exc).retry_after is None


# --------------------------------------------------------------------------
# NVIDIA connector — constructibility + envelope (AC4, AC6)
# --------------------------------------------------------------------------


def test_nvidia_translator_is_constructible_without_an_api_key_ac6(monkeypatch):
    """The connector, like Groq's, is importable/constructible with no key.

    Building a translator makes no network call and needs no credential — the
    client is lazy. This is what keeps the seam stubbable.
    """
    monkeypatch.delenv("NVIDIA_NIM_API_KEY", raising=False)
    r = load_registry()
    cfg = r.get("nvidia")
    tr = build_translator(cfg, nvidia_mod.DEFAULT_MODEL)
    assert tr.model == nvidia_mod.DEFAULT_MODEL
    assert tr.base_url == "https://integrate.api.nvidia.com/v1"
    assert tr._client is None  # nothing built yet — no key needed


def test_nvidia_translator_unwraps_the_envelope_ac4():
    """NVIDIA's connector returns translated text, not a raw completion.

    No `response_format` is sent (see the connector docstring: not every NIM
    model accepts JSON mode), so the envelope is unwrapped by the shared
    `extract_translation` — the same tolerant extractor the Groq connector
    uses. A model that returns prose-with-translation still unwraps.
    """
    sent = {}

    class FakeCompletions:
        async def create(self, **kwargs):
            sent.update(kwargs)
            # No `response_format` asserted absent here — see the test below.
            message = type("M", (), {"content": '{"translation": "{a: 1} תחביר"}'})()
            return type("C", (), {"choices": [type("Ch", (), {"message": message})()]})()

    client = type("Client", (), {
        "chat": type("Chat", (), {"completions": FakeCompletions()})()
    })()

    r = load_registry()
    cfg = r.get("nvidia")
    tr = build_translator(cfg, "nvidia/teeny")
    tr._client = client
    assert asyncio.run(tr.translate("prompt")) == "{a: 1} תחביר"
    # NVIDIA does NOT send response_format (unlike Groq) — see connector docstring.
    assert "response_format" not in sent
    assert sent["model"] == "nvidia/teeny"
    assert sent["temperature"] == 0.1


def test_the_registry_pairs_a_translator_with_its_own_classify():
    """`main()` injects the resolved provider's classify; the registry exposes it."""
    r = load_registry()
    groq_classify = get_classify(r.get("groq"))
    nvidia_classify = get_classify(r.get("nvidia"))
    # Each comes from its own connector file, not from a provider library.
    assert groq_classify.__module__.endswith("groq")
    assert nvidia_classify.__module__.endswith("nvidia")
    assert groq_classify is not nvidia_classify


# --------------------------------------------------------------------------
# The `groq` name collision (regression)
# --------------------------------------------------------------------------


def test_the_groq_connector_is_not_shadowed_by_the_groq_library():
    """`cl10n/providers/groq.py` and the `groq` PyPI package share a leaf name.

    Before the package layout, both were reachable as the bare top-level name
    `groq`. The library is normally already in `sys.modules` (the connector
    imports it for its exception taxonomy), so resolving the connector by
    module *name* returned the library, and the lookup died with
    `module 'groq' has no attribute 'GroqTranslator'`.

    The package resolves it: the connector is only ever
    `cl10n.providers.groq`, the library only ever `groq`, and the two names
    cannot alias. With the real library imported first, the registry must
    still find the connector's class, and the library must survive intact.
    """
    import groq as groq_library  # the PyPI package, imported first on purpose

    assert hasattr(groq_library, "AsyncGroq"), "the real groq library must be importable"

    r = load_registry()
    translator = build_translator(r.get("groq"), "some/model")
    assert type(translator).__name__ == "GroqTranslator"
    assert translator.model == "some/model"
    # Resolved inside the package, not to the site-packages library.
    assert type(translator).__module__ == "cl10n.providers.groq"
    module_file = sys.modules["cl10n.providers.groq"].__file__
    assert module_file.endswith(os.path.join("cl10n", "providers", "groq.py"))
    # And the library is still the library — the connector reached the real
    # one, which is what its `classify` maps exceptions from.
    assert hasattr(groq_library, "AsyncGroq")
    assert sys.modules["cl10n.providers.groq"].groq is groq_library


def test_an_unknown_connector_module_fails_with_a_clear_message(tmp_path):
    config = tmp_path / "providers.toml"
    config.write_text(
        'default = "ghost"\n\n'
        '[providers.ghost]\n'
        'connector = "no_such_connector:Thing"\n'
        'default_model = "x/y"\n'
        'api_key_env = "X_KEY"\n'
    )
    r = load_registry(str(config))
    with pytest.raises(ModuleNotFoundError):
        build_translator(r.get("ghost"), "x/y")


# --------------------------------------------------------------------------
# creds-file loader (generalised, AC2)
# --------------------------------------------------------------------------


def test_load_creds_file_reads_the_named_env_var_into_the_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("NVIDIA_NIM_API_KEY", raising=False)
    creds = tmp_path / "nvidia_creds.txt"
    creds.write_text('export NVIDIA_NIM_API_KEY="nx_12345"\nOTHER= thing\n')
    load_creds_file(str(creds), "NVIDIA_NIM_API_KEY")
    assert os.environ.get("NVIDIA_NIM_API_KEY") == "nx_12345"


def test_load_creds_file_nevers_overrides_an_existing_env_var(tmp_path, monkeypatch):
    """A real env var always wins (setdefault semantics)."""
    monkeypatch.setenv("NVIDIA_NIM_API_KEY", "real")
    creds = tmp_path / "nvidia_creds.txt"
    creds.write_text('NVIDIA_NIM_API_KEY=fromfile\n')
    load_creds_file(str(creds), "NVIDIA_NIM_API_KEY")
    assert os.environ.get("NVIDIA_NIM_API_KEY") == "real"


def test_load_creds_file_missing_file_is_a_noop(monkeypatch):
    monkeypatch.delenv("NVIDIA_NIM_API_KEY", raising=False)
    load_creds_file("does/not/exist", "NVIDIA_NIM_API_KEY")
    assert os.environ.get("NVIDIA_NIM_API_KEY") is None


# --------------------------------------------------------------------------
# Mistral connector (its own SDK, not an OpenAI-compatible endpoint)
# --------------------------------------------------------------------------
#
# Mistral is the first connector whose SDK is not a groq/openai lookalike, so
# these cover the three places it genuinely differs: one SDKError instead of a
# class hierarchy, Retry-After on the error's own headers rather than a
# `.response`, and a content field that may be a chunk list rather than a str.

from cl10n.providers import mistral as mistral_mod  # noqa: E402

_MISTRAL_REQUEST = httpx.Request("POST", "https://api.mistral.ai/v1/chat/completions")


def _sdk_error(status: int, headers: dict | None = None, message: str = "boom"):
    """A real `mistralai` SDKError — so `classify` is tested, not mocked."""
    from mistralai.client import errors as mistral_errors

    response = httpx.Response(status, headers=headers or {}, request=_MISTRAL_REQUEST)
    return mistral_errors.SDKError(message, raw_response=response, body="")


@pytest.mark.parametrize("exc,kind,retryable", [
    (_sdk_error(429), "rate_limit", True),
    (_sdk_error(500), "api_error", True),
    (_sdk_error(503), "api_error", True),
    (_sdk_error(408), "api_error", True),
    (_sdk_error(409), "api_error", True),
    (_sdk_error(401), "api_error", False),
    (_sdk_error(403), "api_error", False),
    (_sdk_error(400), "api_error", False),
    (_sdk_error(404), "api_error", False),
    (_sdk_error(422), "api_error", False),
    # httpx transport errors are NOT OSError subclasses, so the generic tail
    # in base.classify would call them terminal. They must be caught here.
    (httpx.ConnectError("connection refused", request=_MISTRAL_REQUEST), "network", True),
    (httpx.ReadTimeout("timed out", request=_MISTRAL_REQUEST), "network", True),
    (httpx.RemoteProtocolError("peer closed", request=_MISTRAL_REQUEST), "network", True),
    (asyncio.TimeoutError(), "network", True),
    (ConnectionResetError("reset by peer"), "network", True),
    (ValueError("bug in our own code"), "api_error", False),
])
def test_mistral_classify(exc, kind, retryable):
    failure = mistral_mod.classify(exc)
    assert failure.kind == kind
    assert failure.retryable is retryable


def test_mistral_classify_reads_retry_after_from_the_errors_own_headers():
    """SDKError has `headers`/`raw_response` but no `.response`.

    `base._retry_after` looks for `exc.response.headers` and so finds nothing
    for Mistral — the connector supplies its own reader. If that regressed, a
    429 would fall back to our own backoff and ignore the server's wait.
    """
    exc = _sdk_error(429, headers={"retry-after": "37"})
    assert _base_retry_after(exc) is None, "precondition: the shared helper cannot see it"
    assert mistral_mod.classify(exc).retry_after == 37.0


def test_mistral_classify_ignores_http_date_retry_after():
    exc = _sdk_error(429, headers={"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"})
    assert mistral_mod.classify(exc).retry_after is None


def test_mistral_translator_is_constructible_without_an_api_key(monkeypatch):
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    r = load_registry()
    tr = build_translator(r.get("mistral"), mistral_mod.DEFAULT_MODEL)
    assert tr.model == mistral_mod.DEFAULT_MODEL
    assert tr._client is None  # nothing built yet — no key needed


def test_mistral_translator_unwraps_the_envelope_and_asks_for_json():
    sent = {}

    class FakeChat:
        async def complete_async(self, **kwargs):
            sent.update(kwargs)
            message = type("M", (), {"content": '{"translation": "{a: 1} תחביר"}'})()
            return type("C", (), {"choices": [type("Ch", (), {"message": message})()]})()

    client = type("Client", (), {"chat": FakeChat()})()

    r = load_registry()
    tr = build_translator(r.get("mistral"), "mistral-small-latest")
    tr._client = client
    assert asyncio.run(tr.translate("prompt")) == "{a: 1} תחביר"
    assert sent["response_format"] == {"type": "json_object"}
    assert sent["model"] == "mistral-small-latest"
    assert sent["temperature"] == 0.1


def test_mistral_flattens_a_chunked_content_reply():
    """`content` is `Union[str, List[ContentChunk]]`.

    Handing the list straight to `extract_translation` would store a Python
    repr in the translation memory, which renders as garbage and passes every
    other check.
    """
    chunks = [
        type("Chunk", (), {"text": '{"translation": "שלו'})(),
        type("Chunk", (), {"text": 'ם"}'})(),
    ]
    assert mistral_mod._flatten(chunks) == '{"translation": "שלום"}'
    assert mistral_mod._flatten("plain") == "plain"
    assert mistral_mod._flatten(None) == ""


def test_every_declared_provider_satisfies_the_connector_contract():
    """The contract from PROVIDERS.md, enforced for all providers at once.

    A new connector that forgets `classify`, or whose translator lacks `model`
    or `translate`, fails here rather than at the first paid API call.
    """
    r = load_registry()
    for name in r.names():
        cfg = r.get(name)
        translator = build_translator(cfg, "probe/model")
        assert translator.model == "probe/model", name
        assert callable(getattr(translator, "translate", None)), name
        assert translator._client is None, f"{name}: client must be lazy"
        assert callable(get_classify(cfg)), name


def test_the_mistral_default_model_is_one_the_api_offers():
    """`default_model` must be a real id, and must carry no provider prefix.

    The API's ids are bare (`mistral-large-latest`); the `mistral:` prefix is
    *our* routing syntax and is stripped before the call. Writing
    `mistral/mistral-large-latest` or `mistral:mistral-large-latest` into
    `default_model` would send an id the API does not know, and the failure
    arrives as a 400 on the first paid call rather than here.
    """
    r = load_registry()
    model = r.get("mistral").default_model
    assert model in {
        "mistral-large-latest", "mistral-medium-latest", "mistral-small-latest",
    }
    assert "/" not in model and ":" not in model


@pytest.mark.parametrize("model", [
    "mistral-large-latest", "mistral-medium-latest", "mistral-small-latest",
])
def test_a_prefixed_mistral_model_routes_and_is_stripped(model):
    """`--model mistral:<id>` selects the connector and passes the bare id."""
    route = resolve_route(load_registry(), model=f"mistral:{model}")
    assert route.provider == "mistral"
    assert route.model == model  # the prefix never reaches the provider


# --------------------------------------------------------------------------
# Anthropic API connector (its own SDK, an openai-shaped taxonomy but a
# content-block reply and a required max_tokens — no response_format)
# --------------------------------------------------------------------------
#
# The `anthropic` SDK's exception taxonomy is openai-shaped (a class per
# status, sharing httpx), so `classify` mirrors groq/nvidia rather than
# mistral's status_code-branching. The real differences this pins are:
# the reply is a list of content blocks that must be flattened, there is no
# `response_format` knob, and `max_tokens` is required.

from cl10n.providers import anthropic_api as anthropic_mod  # noqa: E402

# The classify matrix uses the REAL `anthropic` exception classes — classifying
# openai exceptions would prove nothing, since the connector's `classify` does
# `isinstance(exc, anthropic.RateLimitError)` and an openai exception is not
# one. `anthropic` is an installed extra (`cl10n[anthropic_api]`, pulled into
# `cl10n[dev]`), so importing it for taxonomy tests is fine; what is tested is
# no network call and no key, exactly like the openai taxing tests for NVIDIA.
import anthropic  # noqa: E402

_ANTHROPIC_REQUEST = httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def _anthropic_status_error(cls, status, headers=None, message="boom"):
    """A real `anthropic` status exception — so `classify` is tested, not mocked.

    `APIStatusError.__init__` sets `self.response` and `self.status_code`, the
    same shape `base._retry_after` reads down — so the shared helper works
    here (unlike mistral, whose `SDKError` hides the header).
    """
    response = httpx.Response(status, headers=headers or {}, request=_ANTHROPIC_REQUEST)
    return cls(message, response=response, body=None)


def _anthropic_api_timeout():
    """`APITimeoutError` takes only a request (no message kw) — build inline."""
    return anthropic.APITimeoutError(request=_ANTHROPIC_REQUEST)


def _anthropic_connection_error(message="connection reset"):
    """`APIConnectionError` takes a message + request (keyword-only message)."""
    return anthropic.APIConnectionError(message=message, request=_ANTHROPIC_REQUEST)


@pytest.mark.parametrize("exc,kind,retryable", [
    (_anthropic_status_error(anthropic.RateLimitError, 429), "rate_limit", True),
    (_anthropic_status_error(anthropic.InternalServerError, 500), "api_error", True),
    (_anthropic_status_error(anthropic.InternalServerError, 503), "api_error", True),
    (_anthropic_status_error(anthropic.AuthenticationError, 401), "api_error", False),
    (_anthropic_status_error(anthropic.PermissionDeniedError, 403), "api_error", False),
    (_anthropic_status_error(anthropic.BadRequestError, 400), "api_error", False),
    (_anthropic_status_error(anthropic.NotFoundError, 404), "api_error", False),
    (_anthropic_status_error(anthropic.UnprocessableEntityError, 422), "api_error", False),
    (_anthropic_status_error(anthropic.ConflictError, 409), "api_error", True),
    (_anthropic_connection_error(), "network", True),
    (_anthropic_api_timeout(), "network", True),
    (asyncio.TimeoutError(), "network", True),
    (ConnectionResetError("reset by peer"), "network", True),
    (ValueError("bug in our own code"), "api_error", False),
])
def test_anthropic_api_classify(exc, kind, retryable):
    failure = anthropic_mod.classify(exc)
    assert failure.kind == kind
    assert failure.retryable is retryable


def test_anthropic_api_classify_reads_retry_after_from_response_headers():
    """`base._retry_after` reads `exc.response.headers` and works for anthropic.

    Unlike mistral's `SDKError`, `APIStatusError` exposes `.response`, so the
    shared helper finds the `Retry-After` header without a connector-local
    override — this asserts that the shared path is the one in use.
    """
    exc = _anthropic_status_error(
        anthropic.RateLimitError, 429, headers={"retry-after": "12.5"},
    )
    assert _base_retry_after(exc) == 12.5
    assert anthropic_mod.classify(exc).retry_after == 12.5


def test_anthropic_api_classify_ignores_http_date_retry_after():
    exc = _anthropic_status_error(
        anthropic.RateLimitError, 429,
        headers={"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"},
    )
    assert anthropic_mod.classify(exc).retry_after is None


def test_anthropic_api_translator_is_constructible_without_an_api_key(monkeypatch):
    """The connector is importable/constructible with no key (AC2, AC6).

    The client is lazy — `_client is None` after construction — so the module
    stays importable without `ANTHROPIC_API_KEY`, which is what keeps the seam
    stubbable and lets `cl10n[dev]`'s test suite run with no Anthropic key.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    r = load_registry()
    cfg = r.get("anthropic_api")
    tr = build_translator(cfg, anthropic_mod.DEFAULT_MODEL)
    assert tr.model == anthropic_mod.DEFAULT_MODEL
    assert tr._client is None  # nothing built yet — no key needed


def test_anthropic_api_translator_unwraps_the_envelope_and_sends_max_tokens():
    """Anthropic returns a list of content blocks; the connector flattens and
    unwraps.

    No `response_format` is sent (the Messages API has no JSON-mode knob); the
    shared prompt plus the tolerant `extract_translation` carry it. `max_tokens`
    is required by `messages.create`, so it is always present in the call.
    """
    sent = {}

    class FakeMessages:
        async def create(self, **kwargs):
            sent.update(kwargs)
            # The reply is `message.content`, a LIST of content blocks, not
            # `choices[0].message.content`. A model returning a JSON object
            # arrives as a single text block holding the envelope string.
            block = type("Block", (), {
                "type": "text",
                "text": '{"translation": "{a: 1} תחביר"}',
            })()
            return type("Message", (), {"content": [block]})()

    client = type("Client", (), {
        "messages": FakeMessages()
    })()

    r = load_registry()
    cfg = r.get("anthropic_api")
    tr = build_translator(cfg, "claude-haiku-4-5-20251001")
    tr._client = client
    assert asyncio.run(tr.translate("prompt")) == "{a: 1} תחביר"
    # No response_format — the Messages API has no JSON-mode knob.
    assert "response_format" not in sent
    # max_tokens is required by the SDK, so it is always sent.
    assert sent["max_tokens"] == 4096
    assert sent["model"] == "claude-haiku-4-5-20251001"
    assert sent["temperature"] == 0.1


def test_anthropic_api_flattens_a_multi_block_content_reply():
    """A reply may span several text blocks. Concatenate the text ones; any
    non-text block (a tool-use or thinking block) contributes nothing.

    Handing the block list straight to `extract_translation` would store a
    Python repr in the translation memory — exactly the mistake `mistral.py`
    documents, which is why both flatten.
    """
    first = '{"translation": "שלו'
    last = 'ם"}'
    blocks = [
        type("Block", (), {"type": "text", "text": first})(),
        type("Block", (), {"type": "thinking", "text": "reasoning here"})(),
        type("Block", (), {"type": "text", "text": last})(),
    ]
    assert anthropic_mod._flatten(blocks) == '{"translation": "שלום"}'
    assert anthropic_mod._flatten("plain string") == "plain string"
    assert anthropic_mod._flatten(None) == ""


def test_anthropic_api_classify_uses_the_installed_sdk():
    """`classify` isolates the resolved provider's own taxonomy. Anthropic's
    `classify` is its own function in its own module, not groq's or openai's.
    """
    r = load_registry()
    ac = get_classify(r.get("anthropic_api"))
    assert ac is anthropic_mod.classify
    assert ac.__module__.endswith("anthropic_api")
