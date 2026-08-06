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

import httpx
import openai
import pytest
from conftest import connection_error, status_error

CL10N = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(CL10N)
import sys  # noqa: E402
sys.path[:0] = [CL10N, os.path.join(REPO, "app")]

# The conftest helpers special-case groq's APITimeoutError (request-only ctor)
# but the openai twin is a different class, so build it inline.
_NVIDIA_REQUEST = httpx.Request("POST", "https://integrate.api.nvidia.com/v1/chat/completions")


def _openai_api_timeout():
    return openai.APITimeoutError(request=_NVIDIA_REQUEST)

from providers import (  # noqa: E402
    ProviderConfig,
    Registry,
    build_translator,
    get_classify,
    load_creds_file,
    load_registry,
    resolve_route,
)
from providers import nvidia as nvidia_mod  # noqa: E402


# --------------------------------------------------------------------------
# Registry + routing (AC1, AC2)
# --------------------------------------------------------------------------


def test_the_registry_loads_every_declared_provider():
    r = load_registry()
    assert r.default == "groq"
    assert set(r.names()) == {"groq", "nvidia"}


def test_each_provider_declares_its_key_env_and_default_model_ac2():
    r = load_registry()
    groq = r.get("groq")
    assert groq.connector == "groq:GroqTranslator"
    assert groq.api_key_env == "GROQ_API_KEY"
    assert groq.api_key_creds_file == "groq_creds.txt"
    assert groq.base_url is None  # groq library default

    nvidia = r.get("nvidia")
    assert nvidia.connector == "nvidia:NvidiaTranslator"
    assert nvidia.api_key_env == "NVIDIA_API_KEY"
    assert nvidia.base_url == "https://integrate.api.nvidia.com/v1"
    assert nvidia.default_model  # non-empty


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
        'api_key_env = "NVIDIA_API_KEY"\n'
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
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
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
    assert get_classify(r.get("groq")).__module__ == "cl10n.providers.groq"
    assert get_classify(r.get("nvidia")).__module__ == "cl10n.providers.nvidia"


# --------------------------------------------------------------------------
# creds-file loader (generalised, AC2)
# --------------------------------------------------------------------------


def test_load_creds_file_reads_the_named_env_var_into_the_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    creds = tmp_path / "nvidia_creds.txt"
    creds.write_text('export NVIDIA_API_KEY="nx_12345"\nOTHER= thing\n')
    load_creds_file(str(creds), "NVIDIA_API_KEY")
    assert os.environ.get("NVIDIA_API_KEY") == "nx_12345"


def test_load_creds_file_nevers_overrides_an_existing_env_var(tmp_path, monkeypatch):
    """A real env var always wins (setdefault semantics)."""
    monkeypatch.setenv("NVIDIA_API_KEY", "real")
    creds = tmp_path / "nvidia_creds.txt"
    creds.write_text('NVIDIA_API_KEY=fromfile\n')
    load_creds_file(str(creds), "NVIDIA_API_KEY")
    assert os.environ.get("NVIDIA_API_KEY") == "real"


def test_load_creds_file_missing_file_is_a_noop(monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    load_creds_file("does/not/exist", "NVIDIA_API_KEY")
    assert os.environ.get("NVIDIA_API_KEY") is None
