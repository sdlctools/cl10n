"""Resumable, bounded-concurrency runner for the translation queue.

Step 7 of `.claude/rules/l10n-pipeline-spec.md`: take a queue file the planner
produced, drive every job in it to a terminal state against the provider, and
keep the file on disk an accurate picture of progress at all times. It does not
decide *what* to translate (that is `tree_diff.plan`) and does not turn
translations back into Markdown (that is reassembly).

    venv/bin/python3 app/queue_runner.py l10n/queue/queue.json
    venv/bin/python3 app/queue_runner.py l10n/queue/queue.json --dry-run
    venv/bin/python3 app/queue_runner.py l10n/queue/queue.json --concurrency 8

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
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Protocol

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import groq  # noqa: E402  (imported for its exception taxonomy — see classify)
import groq_api  # noqa: E402  (sibling module, path fixed up above)
from l10n_store import TranslationMemory, load_queue, save_queue, utc_now  # noqa: E402

TERMINAL_STATES = {"done", "rejected"}

DEFAULT_CONCURRENCY = 4  # conservative enough not to trip a cold-run rate limit
DEFAULT_REQUEST_TIMEOUT = 120.0  # seconds; a stuck job must free its slot
DEFAULT_TM_DIR = "l10n/tm"

BACKOFF_BASE = 1.0  # seconds
BACKOFF_CAP = 60.0


# --------------------------------------------------------------------------
# Failure classification
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Failure:
    """One attempt's outcome when it wasn't a usable translation.

    `kind` is the schema's error kind; `retryable` decides which edge out of
    `failed` the job takes.
    """

    kind: str  # rate_limit | network | api_error | placeholder_lost
    detail: str
    retryable: bool
    retry_after: float | None = None  # provider-stated wait, seconds

    def as_error(self) -> dict:
        return {"kind": self.kind, "detail": self.detail, "at": utc_now()}


def _retry_after(exc) -> float | None:
    """Seconds from a `Retry-After` header, when the provider sent one.

    Only the delta-seconds form is honoured; the HTTP-date form is rare from
    JSON APIs and a bad parse is worse than falling back to our own backoff.
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


def classify(exc: BaseException) -> Failure:
    """Map a provider exception onto the schema's error kinds (spec §4/§5).

    Retryable: 429, 5xx, request timeouts, connection loss — the failures that
    say "not now" rather than "not ever". Terminal: authentication, malformed
    request, unsupported model or language, and anything unrecognised. An
    unrecognised exception is usually a bug in *this* code, and retrying a bug
    three times only bills for it three times.
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

    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return Failure("network", "request timed out", True)
    if isinstance(exc, (ConnectionError, OSError)):
        return Failure("network", _message(exc), True)
    return Failure("api_error", f"{type(exc).__name__}: {_message(exc)}", False)


def _message(exc: BaseException) -> str:
    return " ".join(str(exc).split()) or type(exc).__name__


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

    The rule: every placeholder must occur in the translation at least as many
    times as in the source, verbatim. "At least" rather than "exactly" because
    a target language may legitimately repeat a term the source states once;
    losing one is the failure this gate exists to catch.
    """
    lost = []
    for ph in dict.fromkeys(p for p in placeholders if p):  # dedup, keep order
        need = source.count(ph)
        got = translation.count(ph)
        if got < need:
            lost.append(f"placeholder {ph!r} occurs {need}x in source, {got}x in translation")
    if not lost:
        return None
    return Failure("placeholder_lost", "; ".join(lost), True)


def lost_placeholders(source: str, translation: str, placeholders) -> list[str]:
    """The placeholders the gate would reject — named back to the model on retry."""
    return [
        ph
        for ph in dict.fromkeys(p for p in placeholders if p)
        if translation.count(ph) < source.count(ph)
    ]


# --------------------------------------------------------------------------
# Provider
# --------------------------------------------------------------------------


class Translator(Protocol):
    """What the runner needs from a provider, and nothing else.

    Returning the translated *text* (not a raw completion) is what keeps the
    JSON-envelope handling below a Groq detail and lets a test stub be three
    lines long — AC7's "no test requires a live API key" falls out of the seam
    being this narrow.
    """

    async def translate(self, prompt: str) -> str: ...


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")


def extract_translation(content: str) -> str:
    """Pull the translation out of the model's reply.

    `TRANSLATION_PROMPT` rule 4 asks for a JSON object, and the model obliges —
    `{"translation": "..."}`. Handing that raw string to the TM (which is what
    `groq_api.translate_text` does today) stores the envelope as if it were the
    translation. Tolerant by design: a bare string, a fenced block, or an object
    under any of the plausible keys all resolve, because a hard parse failure
    here would reject a translation that was actually fine.
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


class GroqTranslator:
    """`Translator` backed by the Groq async client."""

    def __init__(self, model: str | None = None, client=None, max_tokens: int = 4096):
        self.model = model or groq_api.DEFAULT_MODEL
        self._client = client
        self.max_tokens = max_tokens

    @property
    def client(self):
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
    """
    lang_name = groq_api.LANG_NAMES.get(job["lang"], job["lang"])
    parts = [
        groq_api.TRANSLATION_PROMPT.format(
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
    ):
        self.queue_path = queue_path
        self.translator = translator
        self.tm_dir = tm_dir
        self.concurrency = max(1, concurrency)
        self.request_timeout = request_timeout
        self.model = model or getattr(translator, "model", groq_api.DEFAULT_MODEL)
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
            return classify(exc), ""
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
        if not entry or entry.get("prompt_version") != groq_api.PROMPT_VERSION:
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
            prompt_version=groq_api.PROMPT_VERSION,
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
            if entry and entry.get("prompt_version") == groq_api.PROMPT_VERSION:
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


def _load_key_file(path: str) -> None:
    """Read `GROQ_API_KEY="..."` out of a creds file into the environment.

    Convenience for local runs only — CI passes the variable directly, and the
    file this reads is gitignored.
    """
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            match = re.match(r'\s*(?:export\s+)?GROQ_API_KEY\s*=\s*["\']?([^"\'\s]+)', line)
            if match:
                os.environ.setdefault("GROQ_API_KEY", match.group(1))


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Execute a translation queue against the Groq API, resumably."
    )
    parser.add_argument("queue", help="path to l10n/queue/queue.json")
    parser.add_argument("--tm-dir", default=DEFAULT_TM_DIR,
                        help=f"translation memory directory (default: {DEFAULT_TM_DIR})")
    parser.add_argument("-c", "--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                        help=f"in-flight requests (default: {DEFAULT_CONCURRENCY})")
    parser.add_argument("--request-timeout", type=float, default=DEFAULT_REQUEST_TIMEOUT,
                        help="per-request timeout in seconds; 0 disables")
    parser.add_argument("--model", default=None,
                        help=f"provider model (default: {groq_api.DEFAULT_MODEL})")
    parser.add_argument("-n", "--dry-run", action="store_true",
                        help="report what would be called; contacts nothing")
    parser.add_argument("--creds-file", default="groq_creds.txt",
                        help="file to read GROQ_API_KEY from when it is unset")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    if args.dry_run:
        runner = QueueRunner(args.queue, translator=None, tm_dir=args.tm_dir,
                             concurrency=args.concurrency, model=args.model)
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

    _load_key_file(args.creds_file)
    if not os.environ.get("GROQ_API_KEY"):
        print("GROQ_API_KEY is not set (and no creds file found) — use --dry-run "
              "to plan without a provider.", file=sys.stderr)
        return 2

    runner = QueueRunner(
        args.queue,
        translator=GroqTranslator(model=args.model),
        tm_dir=args.tm_dir,
        concurrency=args.concurrency,
        request_timeout=args.request_timeout or None,
        model=args.model,
        on_event=lambda kind, job: print(f"  {kind:9} {job['id']}", file=sys.stderr),
    )
    summary = asyncio.run(runner.run())
    print(summary.as_text())
    # Rejected jobs are a reportable outcome, not a crash: the queue file
    # records each one and the renderer falls back to source for it (spec §5).
    return 1 if summary.rejected else 0


if __name__ == "__main__":
    raise SystemExit(main())
