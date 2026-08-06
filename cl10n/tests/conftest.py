"""Shared fixtures and provider stubs for the queue-runner tests.

Nothing here contacts a provider and nothing reads `GROQ_API_KEY`: every test
in this suite runs against a stub (AC7). The pipeline is imported as the
installed `cl10n` package; `REPO` is only for the fixtures that need this
*checkout* (the `md/` corpus, the workflow files), never for imports.
"""

from __future__ import annotations

import asyncio
import json
import os

import httpx
import pytest

from cl10n import l10n_store, resources

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# --------------------------------------------------------------------------
# Provider stubs
# --------------------------------------------------------------------------


class StubTranslator:
    """Records every prompt it is handed and replies from a scripted plan.

    `responses` maps a job's source text to either a string (returned) or an
    exception (raised). `default` covers everything else. `latency` makes the
    concurrency tests measurable without a network.
    """

    model = "stub/model"

    def __init__(self, responses=None, default="<translated>", latency=0.0):
        self.responses = responses or {}
        self.default = default
        self.latency = latency
        self.prompts: list[str] = []
        self.concurrent = 0
        self.max_concurrent = 0

    async def translate(self, prompt: str) -> str:
        self.prompts.append(prompt)
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            if self.latency:
                await asyncio.sleep(self.latency)
            for needle, reply in self.responses.items():
                if needle in prompt:
                    if isinstance(reply, BaseException):
                        raise reply
                    if callable(reply):
                        return reply(prompt)
                    return reply
            return self.default
        finally:
            self.concurrent -= 1

    @property
    def calls(self) -> int:
        return len(self.prompts)


class ScriptedTranslator:
    """Replies from a per-source list, one entry per attempt.

    Lets a test say "429, then 429, then success" for one job while another
    job's script runs independently — which is how the retry path is asserted
    without a sleep.
    """

    model = "stub/model"

    def __init__(self, scripts: dict[str, list], default="<translated>"):
        self.scripts = {k: list(v) for k, v in scripts.items()}
        self.default = default
        self.prompts: list[str] = []

    async def translate(self, prompt: str) -> str:
        self.prompts.append(prompt)
        for needle, script in self.scripts.items():
            if needle in prompt:
                reply = script.pop(0) if script else self.default
                if isinstance(reply, BaseException):
                    raise reply
                return reply
        return self.default

    @property
    def calls(self) -> int:
        return len(self.prompts)


# --------------------------------------------------------------------------
# Provider exceptions, constructed the way the real client raises them
# --------------------------------------------------------------------------


def status_error(cls, status: int, headers: dict | None = None, message: str = "boom"):
    """A real `groq` status exception — so `classify` is tested, not mocked."""
    request = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    response = httpx.Response(status, headers=headers or {}, request=request)
    return cls(message, response=response, body=None)


def connection_error(cls, message: str = "connection reset"):
    """`APIConnectionError` takes a message; `APITimeoutError` fixes its own."""
    request = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    if cls is __import__("groq").APITimeoutError:
        return cls(request=request)
    return cls(message=message, request=request)


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def schemas():
    """The three JSON Schemas, loaded once — the contract tests validate against.

    Read as package data, so these tests validate against the schemas that
    actually ship rather than against a copy of them in the checkout.
    """
    out = {}
    for name in ("queue", "translation-memory", "manifest"):
        with open(resources.schema_path(name), encoding="utf-8") as fh:
            out[name] = json.load(fh)
    return out


@pytest.fixture
def workspace(tmp_path):
    """An isolated `l10n/` tree: queue path plus TM directory."""

    class Workspace:
        def __init__(self):
            self.root = tmp_path
            self.queue_path = str(tmp_path / "l10n" / "queue" / "queue.json")
            self.tm_dir = str(tmp_path / "l10n" / "tm")

        def write_queue(self, jobs, **overrides):
            queue = l10n_store.new_queue("a" * 40, jobs)
            queue.update(overrides)
            l10n_store.save_queue(self.queue_path, queue)
            return queue

        def read_queue(self):
            return l10n_store.load_queue(self.queue_path)

        def read_tm(self, lang):
            path = l10n_store.tm_path(self.tm_dir, lang)
            if not os.path.exists(path):
                return None
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)

        def jobs_by_id(self):
            return {job["id"]: job for job in self.read_queue()["jobs"]}

    return Workspace()


@pytest.fixture
def job_factory():
    """`new_job` with test-friendly defaults and a distinct hash per call."""
    counter = {"n": 0}

    def make(source="Hello world.", lang="he", **kwargs):
        counter["n"] += 1
        kwargs.setdefault("unit_hash", f"{counter['n']:016x}")
        kwargs.setdefault("action", "TRANSLATE")
        return l10n_store.new_job(source=source, lang=lang, **kwargs)

    return make


@pytest.fixture
def no_sleep():
    """Swallow backoff waits, recording what was asked for.

    Carries a matching fake clock: the rate-limit gate waits until a deadline,
    so a sleep that returns without advancing time would spin forever.
    """
    waits: list[float] = []
    now = {"t": 0.0}

    async def sleep(seconds):
        waits.append(seconds)
        now["t"] += seconds

    sleep.waits = waits
    sleep.clock = lambda: now["t"]
    return sleep
