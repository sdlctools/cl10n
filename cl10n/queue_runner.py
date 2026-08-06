"""Resumable, bounded-concurrency runner for the translation queue.

Step 7 of `.claude/rules/l10n-pipeline-spec.md`: take a queue file the planner
produced, drive every job in it to a terminal state against the provider, and
keep the file on disk an accurate picture of progress at all times. It does not
decide *what* to translate (that is `tree_diff.plan`) and does not turn
translations back into Markdown (that is reassembly).

    venv/bin/python3 cl10n/queue_runner.py l10n/queue/queue.json
    venv/bin/python3 cl10n/queue_runner.py l10n/queue/queue.json --dry-run
    venv/bin/python3 cl10n/queue_runner.py l10n/queue/queue.json --concurrency 8

Three things carry the design, all from spec §4:

1. **Write before you act.** `pending → in_flight` is flushed *before* the
   request goes out, and the translation memory entry is written *before* the
   job is flushed `done`. Both orderings are chosen so that a process killed
   between the two writes loses nothing: the first leaves a job that looks
   in-flight (re-run, harmlessly), the second leaves a translation in the TM
   whose job re-runs and hits the TM shortcut. The reverse orderings would
   respectively lose a paid-for translation and mark a job done whose
   translation was never persisted.

2. **The restart rule is the whole of resumption.** Startup rewrites every
   `in_flight` job to `pending` and touches nothing else. Combined with the TM
   shortcut this gives at-least-once execution with exactly-once *effect*,
   because a TM upsert is idempotent under `(lang, unit_hash)`.

3. **Failure is a state, not an exception.** A job that fails records why,
   backs off, and retries on its own; nothing it does can stall a job it has no
   relationship with. Concurrency is bounded by a semaphore held only across
   the request itself — never across a backoff sleep, which would let one
   rate-limited job idle a slot other jobs could use.

Terminal vs. retryable, and where this reads spec §4 rather than quoting it:
the spec's diagram annotates `failed → rejected` with
`attempts == max_attempts`, which covers retry exhaustion. A provider error
that can never succeed — bad credentials, malformed request, unsupported
language — takes the same edge immediately, on its first failure, with
`attempts` left at its true value. Inflating `attempts` to `max_attempts` to
make the annotation literally true would put a lie in the state file (three
billed calls where one was made); rejecting early is the behaviour the issue
asks for ("terminal failures ... do not stall the rest of the queue").
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
import time
from dataclasses import dataclass, field

_HERE = os.path.dirname(os.path.abspath(__file__))
# This package plus `app/`, which owns the prompt (`prompt`, `groq_api`) and
# the planner (`tree_diff`). `cl10n/providers/` (the registry and connectors)
# is reached as a package import below. Bare scripts rather than an installed
# package is the repo's existing convention — see `app/tree_diff.py`.
sys.path[:0] = [_HERE, os.path.join(os.path.dirname(_HERE), "app")]

import prompt as prompt_mod  # noqa: E402  (app/ — the provider-agnostic prompt + PROMPT_VERSION)
from l10n_store import TranslationMemory, load_queue, save_queue, utc_now  # noqa: E402
from placeholders import describe as _describe_lost  # noqa: E402
from placeholders import lost_placeholders  # noqa: E402

TERMINAL_STATES = {"done", "rejected"}

DEFAULT_CONCURRENCY = 4  # conservative enough not to trip a cold-run rate limit
DEFAULT_REQUEST_TIMEOUT = 120.0  # seconds; a stuck job must free its slot
DEFAULT_TM_DIR = "l10n/tm"

BACKOFF_BASE = 1.0  # seconds
BACKOFF_CAP = 60.0

# Fallback model for `QueueRunner.model` when neither the route nor the
# injected translator carries one. Mirrors `providers.toml`'s `[providers.groq]
# default_model` — the backward-compatible default — so a test that builds a
# runner with a `StubTranslator(model="stub/model")` gets "stub/model", but one
# built without a `model` gets Groq's default, as it did pre-CLN-1.
DEFAULT_GROQ_MODEL = "openai/gpt-oss-120b"


# --------------------------------------------------------------------------
# Failure classification, envelope extraction, the Translator seam (CLN-1)
# --------------------------------------------------------------------------
#
# These moved to `cl10n/providers/` behind the pluggable-provider seam:
# `Failure`, `_retry_after`, `_message`, `extract_translation`, and the
# `Translator` Protocol live in `providers.base` (provider-agnostic); the Groq
# `classify`/`GroqTranslator` live in `providers.groq`. They are re-exported
# here so the tests and any caller importing `queue_runner.Failure`,
# `queue_runner.classify`, `queue_runner.GroqTranslator`,
# `queue_runner.extract_translation` keep working — adding a provider no longer
# adds a branch to the runner, so these re-exports are the backward-compat
# surface, not the implementation.
#
# `groq.classify` is the *default provider's* classifier. The runner holds the
# resolved provider's `classify` as `self.classify` (set by `main()` to the
# connector whose translator is in use), and falls back to this one when a
# translator is injected directly (tests, `--dry-run`).

from providers.base import (  # noqa: E402,F401  (_message/_retry_after: re-export only)
    Failure,
    Translator,
    _message,
    _retry_after,
    extract_translation,
)
from providers.groq import GroqTranslator, classify  # noqa: E402

# The default-classifier alias is the default provider's (Groq), so a
# `QueueRunner` constructed by a test with an injected translator falls back to
# classifying the way the runner always did — every existing `classify` test
# exercises this path (it builds `Groq`-shaped groq exceptions). `main()`
# overrides `classify` with the resolved provider's when it wires the runner.
DEFAULT_CLASSIFY = classify


# --------------------------------------------------------------------------
# Account-wide rate-limit backpressure
# --------------------------------------------------------------------------


class RateLimitGate:
    """One shared "not yet" for every worker in the run.

    A rate limit is a property of the *account*, not of the job that happened
    to discover it — Groq's is tokens-per-minute across the organisation. With
    per-job backoff alone, eight workers each retry privately into the same
    wall, so one throttle multiplies into eight, and jobs exhaust their retry
    budget against a condition that was never their fault. Measured on the real
    corpus at concurrency 8: 36 rate-limit failures and 4 jobs rejected purely
    from throttling.

    So the first job to see a 429 parks the whole run for the window the
    provider asked for. `trip` only ever extends the window, never shortens it,
    because a second 429 arriving mid-wait means the first estimate was low.
    """

    def __init__(self, sleep=asyncio.sleep, clock=time.monotonic):
        self._sleep = sleep
        self._clock = clock
        self._until = 0.0
        self.trips = 0

    def trip(self, delay: float) -> None:
        self.trips += 1
        self._until = max(self._until, self._clock() + max(0.0, delay))

    @property
    def remaining(self) -> float:
        return max(0.0, self._until - self._clock())

    async def wait(self) -> None:
        # Re-checked in a loop: another worker's 429 can extend the window
        # while this one is already waiting it out.
        while (remaining := self.remaining) > 0:
            await self._sleep(remaining)


# --------------------------------------------------------------------------
# The placeholder gate (spec §5)
# --------------------------------------------------------------------------


def placeholder_gate(source: str, translation: str, placeholders) -> Failure | None:
    """`None` when the translation may enter the TM, a `Failure` when not.

    The rule itself lives in `placeholders.lost_placeholders` because the
    renderer enforces the same one against the TM entries it splices; this is
    only the runner's retryable-`Failure` shape wrapped around it.
    """
    lost = lost_placeholders(source, translation, placeholders)
    if not lost:
        return None
    return Failure("placeholder_lost", _describe_lost(source, translation, lost), True)


# --------------------------------------------------------------------------
# Provider seam — see `cl10n/providers/` (re-exported above)
# --------------------------------------------------------------------------
#
# `Translator`, `extract_translation`, `GroqTranslator`, `classify`, `Failure`
# are imported from `cl10n/providers/` near the top of this module. The default
# provider's `classify` is available as `DEFAULT_CLASSIFY`; the resolved
# provider's `classify` is injected into each `QueueRunner` by `main()`.
# Adding a provider is a `providers.toml` entry plus a connector module —
# nothing here changes (CLN-1).


# --------------------------------------------------------------------------
# Prompt assembly
# --------------------------------------------------------------------------

_OUTPUT_CONTRACT = (
    'Return exactly this JSON object and nothing else:\n'
    '{"translation": "<the translated text>"}'
)


def build_prompt(job: dict, lost: list[str] | None = None) -> str:
    """The message for one attempt at one job.

    Everything appended to `TRANSLATION_PROMPT` is per-job payload — context to
    disambiguate with, the previous revision to reuse wording from, the
    placeholders a failed attempt dropped. The prompt's *rules* are untouched,
    which is why `PROMPT_VERSION` stays where it is.

    The prompt template and `PROMPT_VERSION` are provider-agnostic
    (`app/prompt.py`, moved out of the Groq connector in CLN-1): every
    connector sends identical rules, so a TM shortcut fires across providers.
    """
    lang_name = prompt_mod.LANG_NAMES.get(job["lang"], job["lang"])
    parts = [
        prompt_mod.TRANSLATION_PROMPT.format(
            target_lang=lang_name, text_to_translate=job["source"]
        )
    ]

    if job.get("context"):
        parts.append(
            "SECTION CONTEXT — the headings this text sits under. Use it to "
            "resolve gender, register and deixis. Do NOT translate it and do "
            "NOT include it in your output:\n" + job["context"]
        )

    if job["action"] == "REVISE" and job.get("prior_translation"):
        parts.append(
            f"This is a REVISION, not a fresh translation. The previous English "
            f"read:\n{job.get('old_source') or ''}\n\n"
            f"and was translated into {lang_name} as:\n{job['prior_translation']}\n\n"
            "Produce an updated translation of the new English text above, "
            "keeping the previous wording wherever the English is unchanged."
        )

    if lost:
        named = ", ".join(repr(p) for p in lost)
        parts.append(
            f"CORRECTION — your previous attempt dropped {named}. Every one of "
            "these must appear in your translation exactly as written in the "
            "source, character for character, untranslated."
        )

    parts.append(_OUTPUT_CONTRACT)
    return "\n\n".join(parts)


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------


@dataclass
class RunSummary:
    total: int = 0
    done: int = 0
    rejected: int = 0
    skipped_terminal: int = 0  # already terminal when the run started
    tm_hits: int = 0  # pending → done shortcut, no API call
    api_calls: int = 0
    elapsed: float = 0.0
    errors: dict[str, int] = field(default_factory=dict)

    def as_text(self) -> str:
        lines = [
            f"{self.total} jobs — {self.done} done, {self.rejected} rejected, "
            f"{self.skipped_terminal} already terminal",
            f"{self.api_calls} API call(s), {self.tm_hits} translation-memory hit(s) "
            f"in {self.elapsed:.1f}s",
        ]
        if self.errors:
            kinds = ", ".join(f"{k}={v}" for k, v in sorted(self.errors.items()))
            lines.append(f"failures by kind: {kinds}")
        return "\n".join(lines)


class QueueRunner:
    """Drives one queue file to completion.

    `sleep` and `rng` are injected so tests can assert the backoff schedule
    without spending it, and so a retry storm is reproducible.

    `classify` is the provider connector's failure classifier — the one that
    knows the translator's exception taxonomy. `main()` injects the resolved
    provider's; a test that builds a runner with an injected translator gets
    `DEFAULT_CLASSIFY` (the default provider's, Groq), which is what every
    existing `classify` test exercises (it raises Groq-shaped exceptions).
    """

    def __init__(
        self,
        queue_path: str,
        translator: Translator,
        *,
        tm_dir: str = DEFAULT_TM_DIR,
        concurrency: int = DEFAULT_CONCURRENCY,
        request_timeout: float | None = DEFAULT_REQUEST_TIMEOUT,
        model: str | None = None,
        sleep=asyncio.sleep,
        clock=time.monotonic,
        rng: random.Random | None = None,
        on_event=None,
        classify=DEFAULT_CLASSIFY,
    ):
        self.queue_path = queue_path
        self.translator = translator
        self.tm_dir = tm_dir
        self.concurrency = max(1, concurrency)
        self.request_timeout = request_timeout
        self.model = model or getattr(translator, "model", DEFAULT_GROQ_MODEL)
        self.classify = classify
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._on_event = on_event or (lambda *_: None)
        self.gate = RateLimitGate(sleep=sleep, clock=clock)
        self.queue = load_queue(queue_path)
        self.summary = RunSummary(total=len(self.queue["jobs"]))
        self._tms: dict[str, TranslationMemory] = {}

    # -- persistence -------------------------------------------------------

    def _flush(self) -> None:
        save_queue(self.queue_path, self.queue)

    def _tm(self, lang: str) -> TranslationMemory:
        if lang not in self._tms:
            self._tms[lang] = TranslationMemory.load(self.tm_dir, lang)
        return self._tms[lang]

    def reset_in_flight(self) -> int:
        """Spec §4's restart rule — the *only* startup mutation.

        `done` and `rejected` are never touched, which is what makes AC2 hold:
        a job that already cost an API call never costs a second one.
        """
        reset = 0
        for job in self.queue["jobs"]:
            if job["state"] == "in_flight":
                job["state"] = "pending"
                reset += 1
        if reset:
            self._flush()
        return reset

    # -- backoff -----------------------------------------------------------

    def backoff(self, attempts: int, retry_after: float | None = None) -> float:
        """Exponential with equal jitter, floored by any `Retry-After`.

        Equal jitter (half fixed, half random) rather than full jitter: full
        jitter can return a near-zero wait, which on a 429 is how a client
        turns one rate limit into a stampede of them.
        """
        window = min(BACKOFF_BASE * (2 ** max(0, attempts - 1)), BACKOFF_CAP)
        delay = window / 2 + self._rng.uniform(0, window / 2)
        if retry_after is not None:
            delay = max(delay, retry_after)
        return delay

    # -- execution ---------------------------------------------------------

    async def run(self) -> RunSummary:
        started = time.monotonic()
        self.reset_in_flight()

        semaphore = asyncio.Semaphore(self.concurrency)
        await asyncio.gather(
            *(self._run_job(job, semaphore) for job in self.queue["jobs"])
        )

        # No end-of-run TM flush: `_succeed` already saved, and a run that dies
        # before reaching here must have left the same file behind (spec §4).
        self.summary.elapsed = time.monotonic() - started
        return self.summary

    async def _run_job(self, job: dict, semaphore: asyncio.Semaphore) -> None:
        if job["state"] in TERMINAL_STATES:
            self.summary.skipped_terminal += 1
            if job["state"] == "done":
                self.summary.done += 1
            else:
                self.summary.rejected += 1
            return

        if job["state"] == "failed" and job["attempts"] >= job["max_attempts"]:
            # Died between `_fail` and `_reject` last run: its retries are
            # already spent, so finish the transition instead of buying a
            # further attempt the budget doesn't cover.
            self._reject(job)
            return

        if self._tm_shortcut(job):
            return

        lost: list[str] = []
        while True:
            # Waited outside the semaphore: a throttled run should not hold
            # slots idle, and the semaphore re-bounds the herd on release.
            await self.gate.wait()

            # The semaphore is held across the request only. Claiming inside it
            # keeps the on-disk `in_flight` set equal to the set of requests
            # actually in flight, rather than to every job that wants a slot.
            async with semaphore:
                self._claim(job)
                failure, translation = await self._attempt(job, lost)

            if failure is None:
                self._succeed(job, translation)
                return

            self._fail(job, failure)
            if not failure.retryable or job["attempts"] >= job["max_attempts"]:
                self._reject(job)
                return

            lost = (
                lost_placeholders(job["source"], translation, job["placeholders"])
                if failure.kind == "placeholder_lost"
                else []
            )
            delay = self.backoff(job["attempts"], failure.retry_after)
            if failure.kind == "rate_limit":
                # Everyone waits, not just the job that took the hit; the
                # gate at the top of the loop is what this job then sleeps on.
                self.gate.trip(delay)
            else:
                await self._sleep(delay)

    async def _attempt(self, job: dict, lost: list[str]) -> tuple[Failure | None, str]:
        prompt = build_prompt(job, lost)
        try:
            self.summary.api_calls += 1
            coro = self.translator.translate(prompt)
            translation = (
                await asyncio.wait_for(coro, self.request_timeout)
                if self.request_timeout
                else await coro
            )
        except Exception as exc:  # noqa: BLE001 — classified, never swallowed
            return self.classify(exc), ""
        return (
            placeholder_gate(job["source"], translation, job["placeholders"]),
            translation,
        )

    # -- transitions -------------------------------------------------------

    def _tm_shortcut(self, job: dict) -> bool:
        """`pending → done` without an API call (spec §4).

        Only an entry made with the *current* prompt version counts. This is
        what collapses the first-time/incremental distinction, deduplicates a
        hash shared by two documents, and makes a restart cheap.
        """
        entry = self._tm(job["lang"]).get(job["unit_hash"])
        if not entry or entry.get("prompt_version") != prompt_mod.PROMPT_VERSION:
            return False
        job["state"] = "done"
        job["error"] = None
        job["finished_at"] = utc_now()  # started_at stays null: never in flight
        self._flush()
        self.summary.done += 1
        self.summary.tm_hits += 1
        self._on_event("tm_hit", job)
        return True

    def _claim(self, job: dict) -> None:
        """`pending|failed → in_flight`, flushed **before** the request."""
        job["state"] = "in_flight"
        job["started_at"] = job["started_at"] or utc_now()
        self._flush()

    def _succeed(self, job: dict, translation: str) -> None:
        """`in_flight → done` — TM entry first, queue state second."""
        tm = self._tm(job["lang"])
        tm.upsert(
            job["unit_hash"],
            source=job["source"],
            translation=translation,
            model=self.model,
            prompt_version=prompt_mod.PROMPT_VERSION,
            action=job["action"],
        )
        tm.save()

        job["state"] = "done"
        job["error"] = None
        job["finished_at"] = utc_now()
        self._flush()
        self.summary.done += 1
        self._on_event("done", job)

    def _fail(self, job: dict, failure: Failure) -> None:
        """`in_flight → failed` — non-terminal; `attempts` is per job, not per run."""
        job["attempts"] += 1
        job["error"] = failure.as_error()
        job["state"] = "failed"
        self._flush()
        self.summary.errors[failure.kind] = self.summary.errors.get(failure.kind, 0) + 1
        self._on_event("failed", job)

    def _reject(self, job: dict) -> None:
        """`failed → rejected` — terminal; the renderer falls back to source."""
        job["state"] = "rejected"
        job["finished_at"] = utc_now()
        self._flush()
        self.summary.rejected += 1
        self._on_event("rejected", job)

    # -- dry run -----------------------------------------------------------

    def dry_run(self) -> dict:
        """What a real run would do, without contacting the provider (AC6).

        Reads the TM to resolve the shortcut so the call estimate is the real
        one, and writes nothing at all — not even the restart rule, which would
        otherwise make `--dry-run` mutate the file it is reporting on.
        """
        would_call, would_skip, already = [], [], []
        for job in self.queue["jobs"]:
            if job["state"] in TERMINAL_STATES:
                already.append(job["id"])
                continue
            entry = self._tm(job["lang"]).get(job["unit_hash"])
            if entry and entry.get("prompt_version") == prompt_mod.PROMPT_VERSION:
                would_skip.append(job["id"])
            else:
                would_call.append(job["id"])

        by_lang: dict[str, int] = {}
        for job in self.queue["jobs"]:
            if job["id"] in set(would_call):
                by_lang[job["lang"]] = by_lang.get(job["lang"], 0) + 1
        return {
            "run_id": self.queue["run_id"],
            "jobs": len(self.queue["jobs"]),
            "already_terminal": already,
            "tm_hits": would_skip,
            "would_call": would_call,
            "estimated_api_calls": len(would_call),
            "estimated_calls_by_lang": by_lang,
            "concurrency": self.concurrency,
        }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

from providers import (  # noqa: E402
    build_translator,
    get_classify,
    load_creds_file,
    load_registry,
    resolve_route,
)


def build_arg_parser(registry=None) -> argparse.ArgumentParser:
    """The runner's CLI surface.

    `--provider` and a `--model` of the form `provider:model` select the
    connector (CLN-1 AC1); a bare `--model` resolves against the selected
    provider's default. `registry` (loaded from `providers.toml`) feeds the
    help text so the choices are discoverable; tests pass `None` and the
    parser still works (the help just lists no concrete values).
    """
    declared = registry.names() if registry else []
    default = registry.default if registry else "groq"
    parser = argparse.ArgumentParser(
        description="Execute a translation queue against a configured provider, resumably."
    )
    parser.add_argument("queue", help="path to l10n/queue/queue.json")
    parser.add_argument("--tm-dir", default=DEFAULT_TM_DIR,
                        help=f"translation memory directory (default: {DEFAULT_TM_DIR})")
    parser.add_argument("-c", "--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                        help=f"in-flight requests (default: {DEFAULT_CONCURRENCY})")
    parser.add_argument("--request-timeout", type=float, default=DEFAULT_REQUEST_TIMEOUT,
                        help="per-request timeout in seconds; 0 disables")
    parser.add_argument("--provider", default=None,
                        choices=declared or None,
                        help=f"select the connector (default: {default}); "
                             f"declared: {', '.join(declared or [default])}")
    parser.add_argument("--model", default=None,
                        help="model id, optionally 'provider:model' to also route "
                             "(bare resolves against the selected provider's default)")
    parser.add_argument("-n", "--dry-run", action="store_true",
                        help="report what would be called; contacts nothing")
    parser.add_argument("--creds-file", default=None,
                        help="file to read the active provider's API key from when "
                             "the env var is unset (overrides providers.toml)")
    parser.add_argument("--providers", default=None,
                        help="path to providers.toml (default: cl10n/providers.toml)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    return parser


def main(argv: list[str] | None = None) -> int:
    # Load the registry early so a bad providers.toml fails before parsing the
    # rest, and so help text can name the declared providers. The model prefix
    # is part of --model, so argparse needs the registry to present choices.
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--providers", default=None)
    pre.add_argument("--dry-run", action="store_true")
    pre.add_argument("--provider", default=None)
    pre.add_argument("--model", default=None)
    known, rest = pre.parse_known_args(argv)
    registry = load_registry(known.providers)
    args = build_arg_parser(registry).parse_args(argv)

    route = resolve_route(registry, model=args.model, provider=args.provider)
    cfg = registry.get(route.provider)

    if args.dry_run:
        runner = QueueRunner(args.queue, translator=None, tm_dir=args.tm_dir,
                             concurrency=args.concurrency, model=route.model)
        report = runner.dry_run()
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(f"DRY RUN {report['run_id']} — {report['jobs']} job(s)")
            print(f"  already terminal : {len(report['already_terminal'])}")
            print(f"  translation memory hits : {len(report['tm_hits'])}")
            print(f"  API calls that would be made : {report['estimated_api_calls']}"
                  f"  {report['estimated_calls_by_lang'] or ''}")
            for job_id in report["would_call"]:
                print(f"    CALL {job_id}")
        return 0

    # Load the active provider's key: an explicit --creds-file wins, else the
    # provider's declared creds file (gitignored), else nothing (CI sets env).
    creds_path = args.creds_file or cfg.api_key_creds_file
    load_creds_file(creds_path, cfg.api_key_env)
    if not os.environ.get(cfg.api_key_env):
        print(f"{cfg.api_key_env} is not set (and no creds file found) — use --dry-run "
              "to plan without a provider.", file=sys.stderr)
        return 2

    translator = build_translator(cfg, route.model)
    runner = QueueRunner(
        args.queue,
        translator=translator,
        tm_dir=args.tm_dir,
        concurrency=args.concurrency,
        request_timeout=args.request_timeout or None,
        model=route.model,
        classify=get_classify(cfg),
        on_event=lambda kind, job: print(f"  {kind:9} {job['id']}", file=sys.stderr),
    )
    summary = asyncio.run(runner.run())
    print(summary.as_text())
    # Rejected jobs are a reportable outcome, not a crash: the queue file
    # records each one and the renderer falls back to source for it (spec §5).
    return 1 if summary.rejected else 0


if __name__ == "__main__":
    raise SystemExit(main())
