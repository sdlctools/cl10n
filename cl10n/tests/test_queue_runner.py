"""Tests for the resumable parallel translation queue runner.

Organised by the acceptance criteria on JST-263; the units the ACs rest on
(placeholder gate, error classification, backoff, envelope extraction) are
tested at the bottom. Every test stubs the provider — none needs an API key.
"""

from __future__ import annotations

import asyncio
import json
import random
import time

import groq
import jsonschema
import pytest
from conftest import ScriptedTranslator, StubTranslator, connection_error, status_error

import groq_api
import queue_runner
from queue_runner import QueueRunner


def run(runner):
    return asyncio.run(runner.run())


def make_runner(workspace, translator, **kwargs):
    kwargs.setdefault("tm_dir", workspace.tm_dir)
    kwargs.setdefault("request_timeout", None)
    # A fake sleep must bring its own clock, or the rate-limit gate's
    # wait-until-deadline loop never terminates.
    clock = getattr(kwargs.get("sleep"), "clock", None)
    if clock is not None:
        kwargs.setdefault("clock", clock)
    return QueueRunner(workspace.queue_path, translator, **kwargs)


# ==========================================================================
# AC1 — bounded, configurable concurrency; faster than the sequential loop
# ==========================================================================


def test_jobs_run_concurrently_and_beat_the_sequential_loop(workspace, job_factory):
    jobs = [job_factory(source=f"Sentence {i}.") for i in range(12)]
    workspace.write_queue(jobs)

    serial = StubTranslator(latency=0.02)
    started = time.monotonic()
    run(make_runner(workspace, serial, concurrency=1))
    serial_elapsed = time.monotonic() - started

    # Same queue again, from scratch, with a wider pipe.
    workspace.write_queue([job_factory(source=f"Sentence {i}.") for i in range(12)])
    parallel = StubTranslator(latency=0.02)
    started = time.monotonic()
    run(make_runner(workspace, parallel, concurrency=6))
    parallel_elapsed = time.monotonic() - started

    assert serial.calls == parallel.calls == 12
    assert parallel_elapsed < serial_elapsed / 2, (
        f"concurrency=6 took {parallel_elapsed:.3f}s vs {serial_elapsed:.3f}s serial"
    )


def test_concurrency_is_bounded_by_the_configured_limit(workspace, job_factory):
    workspace.write_queue([job_factory(source=f"S{i}.") for i in range(20)])
    stub = StubTranslator(latency=0.01)
    run(make_runner(workspace, stub, concurrency=4))
    assert stub.max_concurrent <= 4
    assert stub.max_concurrent > 1, "the limit should actually be used, not just respected"


def hang_on_stuck(stub):
    """Provider that never answers the `STUCK.` job and answers the rest at once."""

    async def translate(prompt):
        stub.prompts.append(prompt)
        if "STUCK." in prompt:
            await asyncio.sleep(30)
        return "<translated>"

    return translate


def test_a_stuck_job_does_not_block_unrelated_jobs(workspace, job_factory):
    """The per-request timeout is what keeps one hung call from eating a slot."""
    stuck = job_factory(source="STUCK.")
    others = [job_factory(source=f"Fine {i}.") for i in range(4)]
    workspace.write_queue([stuck] + others)

    stub = StubTranslator()
    stub.translate = hang_on_stuck(stub)

    started = time.monotonic()
    summary = run(make_runner(workspace, stub, concurrency=2, request_timeout=0.05))
    elapsed = time.monotonic() - started

    assert elapsed < 5, "the healthy jobs waited on the stuck one"
    jobs = workspace.jobs_by_id()
    assert [j["state"] for j in jobs.values() if j["id"] != stuck["id"]] == ["done"] * 4
    assert jobs[stuck["id"]]["state"] == "rejected"
    assert jobs[stuck["id"]]["error"]["kind"] == "network"
    assert summary.done == 4


# ==========================================================================
# AC2 — kill mid-run, restart, finish the rest, re-issue nothing terminal
# ==========================================================================


def test_restart_completes_the_remainder_without_recalling_terminal_jobs(
    workspace, job_factory
):
    jobs = [job_factory(source=f"Sentence {i}.") for i in range(10)]
    workspace.write_queue(jobs)

    # --- first run: cancelled mid-flight, exactly as SIGKILL would leave it.
    first = StubTranslator(latency=0.01)

    async def killed_run():
        runner = make_runner(workspace, first, concurrency=2)
        task = asyncio.ensure_future(runner.run())
        while first.calls < 4:
            await asyncio.sleep(0.001)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(killed_run())

    mid = workspace.read_queue()["jobs"]
    states = {j["state"] for j in mid}
    assert "done" in states, "the interrupted run persisted nothing"
    assert states - {"done", "in_flight", "pending", "failed"} == set()
    done_ids = {j["id"] for j in mid if j["state"] == "done"}

    # --- second run: same file, fresh runner.
    second = StubTranslator()
    summary = run(make_runner(workspace, second, concurrency=2))

    final = workspace.jobs_by_id()
    assert all(j["state"] == "done" for j in final.values())

    # No prompt in the second run mentions a job that was already done — the
    # heart of AC2: terminal work is never re-billed.
    for job_id in done_ids:
        source = final[job_id]["source"]
        assert not any(source in p for p in second.prompts), f"{job_id} was re-called"
    assert second.calls == 10 - len(done_ids)
    assert summary.skipped_terminal == len(done_ids)


def test_restart_rule_rewrites_in_flight_to_pending_and_nothing_else(
    workspace, job_factory
):
    flying = job_factory(source="A.")
    flying["state"] = "in_flight"
    flying["started_at"] = "2026-08-04T12:00:00Z"
    finished = job_factory(source="B.")
    finished.update(state="done", finished_at="2026-08-04T12:00:01Z")
    dead = job_factory(source="C.")
    dead.update(state="rejected", attempts=3, finished_at="2026-08-04T12:00:02Z")
    workspace.write_queue([flying, finished, dead])

    runner = make_runner(workspace, StubTranslator())
    assert runner.reset_in_flight() == 1

    on_disk = workspace.jobs_by_id()
    assert on_disk[flying["id"]]["state"] == "pending"
    assert on_disk[flying["id"]]["started_at"] == "2026-08-04T12:00:00Z"
    assert on_disk[finished["id"]]["state"] == "done"
    assert on_disk[dead["id"]]["state"] == "rejected"


def test_translation_memory_shortcut_skips_the_api_entirely(workspace, job_factory):
    job = job_factory(source="Already known.")
    workspace.write_queue([job])

    tm = queue_runner.TranslationMemory.load(workspace.tm_dir, "he")
    tm.upsert(
        job["unit_hash"],
        source=job["source"],
        translation="ידוע",
        model="stub/model",
        prompt_version=groq_api.PROMPT_VERSION,
        action="TRANSLATE",
    )
    tm.save()

    stub = StubTranslator()
    summary = run(make_runner(workspace, stub))

    assert stub.calls == 0
    assert summary.tm_hits == 1
    stored = workspace.jobs_by_id()[job["id"]]
    assert stored["state"] == "done"
    assert stored["started_at"] is None, "a shortcut job was never in flight"
    assert stored["finished_at"] is not None


def test_shortcut_does_not_fire_for_a_stale_prompt_version(workspace, job_factory):
    job = job_factory(source="Known, but older prompt.")
    workspace.write_queue([job])

    tm = queue_runner.TranslationMemory.load(workspace.tm_dir, "he")
    tm.upsert(job["unit_hash"], source=job["source"], translation="ישן",
              model="stub/model", prompt_version="v0", action="TRANSLATE")
    tm.save()

    stub = StubTranslator(default="חדש")
    run(make_runner(workspace, stub))

    assert stub.calls == 1
    assert workspace.read_tm("he")["entries"][job["unit_hash"]]["translation"] == "חדש"


def test_a_failed_job_with_no_retries_left_is_rejected_without_another_call(
    workspace, job_factory
):
    job = job_factory(source="Spent.")
    job.update(state="failed", attempts=3, max_attempts=3,
               error={"kind": "network", "detail": "x", "at": "2026-08-04T12:00:00Z"})
    workspace.write_queue([job])

    stub = StubTranslator()
    run(make_runner(workspace, stub))

    assert stub.calls == 0
    assert workspace.jobs_by_id()[job["id"]]["state"] == "rejected"


# ==========================================================================
# AC3 — 429 backs off and succeeds; auth failure fails one job only
# ==========================================================================


def test_rate_limit_backs_off_then_succeeds(workspace, job_factory, no_sleep):
    job = job_factory(source="Throttled.")
    workspace.write_queue([job])

    scripted = ScriptedTranslator({
        "Throttled.": [
            status_error(groq.RateLimitError, 429),
            status_error(groq.RateLimitError, 429),
            "בסדר",
        ]
    })
    summary = run(make_runner(workspace, scripted, sleep=no_sleep,
                              rng=random.Random(0)))

    assert scripted.calls == 3
    assert summary.done == 1 and summary.rejected == 0
    assert summary.errors == {"rate_limit": 2}
    assert len(no_sleep.waits) == 2
    assert no_sleep.waits[1] > no_sleep.waits[0], "backoff did not grow"

    stored = workspace.jobs_by_id()[job["id"]]
    assert stored["state"] == "done"
    assert stored["attempts"] == 2
    assert stored["error"] is None, "a succeeded job must not keep its last error"
    assert workspace.read_tm("he")["entries"][job["unit_hash"]]["translation"] == "בסדר"


def test_retry_after_header_is_respected(workspace, job_factory, no_sleep):
    job = job_factory(source="Slow down.")
    workspace.write_queue([job])

    scripted = ScriptedTranslator({
        "Slow down.": [
            status_error(groq.RateLimitError, 429, headers={"retry-after": "37"}),
            "ok",
        ]
    })
    run(make_runner(workspace, scripted, sleep=no_sleep, rng=random.Random(0)))

    assert no_sleep.waits == [37.0], "the provider's own wait was ignored"


def test_authentication_failure_rejects_one_job_without_stalling_the_queue(
    workspace, job_factory, no_sleep
):
    bad = job_factory(source="Rejected outright.")
    good = [job_factory(source=f"Fine {i}.") for i in range(3)]
    workspace.write_queue([bad] + good)

    stub = StubTranslator(
        responses={"Rejected outright.": status_error(groq.AuthenticationError, 401)}
    )
    summary = run(make_runner(workspace, stub, sleep=no_sleep))

    stored = workspace.jobs_by_id()
    assert stored[bad["id"]]["state"] == "rejected"
    assert stored[bad["id"]]["error"]["kind"] == "api_error"
    assert stored[bad["id"]]["attempts"] == 1, "a terminal error must not burn retries"
    assert no_sleep.waits == [], "a terminal error must not back off"
    assert all(stored[j["id"]]["state"] == "done" for j in good)
    assert summary.done == 3 and summary.rejected == 1
    assert bad["unit_hash"] not in workspace.read_tm("he")["entries"]


def test_retries_are_exhausted_into_rejected(workspace, job_factory, no_sleep):
    job = job_factory(source="Never works.")
    job["max_attempts"] = 3
    workspace.write_queue([job])

    stub = StubTranslator(
        responses={"Never works.": status_error(groq.InternalServerError, 503)}
    )
    summary = run(make_runner(workspace, stub, sleep=no_sleep))

    stored = workspace.jobs_by_id()[job["id"]]
    assert stub.calls == 3
    assert stored["state"] == "rejected"
    assert stored["attempts"] == 3
    assert stored["finished_at"] is not None
    assert stored["error"]["kind"] == "api_error"
    assert summary.rejected == 1


def test_one_429_pauses_every_worker_not_just_the_job_that_hit_it(
    workspace, job_factory, no_sleep
):
    """The limit is account-wide, so the backoff has to be too — otherwise the
    other workers keep firing into the same wall and spend their budgets on it."""
    throttled = job_factory(source="Throttled.")
    others = [job_factory(source=f"Other {i}.") for i in range(4)]
    workspace.write_queue([throttled] + others)

    calls_before_gate_opened = []
    gate_holder = {}

    scripted = ScriptedTranslator({
        "Throttled.": [status_error(groq.RateLimitError, 429,
                                    headers={"retry-after": "20"}), "בסדר"],
    })
    real_translate = scripted.translate

    async def translate(prompt):
        if gate_holder.get("gate") and gate_holder["gate"].remaining > 0:
            calls_before_gate_opened.append(prompt)
        return await real_translate(prompt)

    scripted.translate = translate
    runner = make_runner(workspace, scripted, concurrency=5, sleep=no_sleep)
    gate_holder["gate"] = runner.gate
    summary = run(runner)

    assert runner.gate.trips == 1
    assert calls_before_gate_opened == [], "a worker ignored the shared rate-limit gate"
    assert summary.done == 5 and summary.rejected == 0
    assert 20.0 in no_sleep.waits


def test_rate_limit_backoff_does_not_double_wait(workspace, job_factory, no_sleep):
    """A 429 parks the run on the gate; sleeping privately as well would
    charge the wait twice."""
    workspace.write_queue([job_factory(source="Throttled.")])
    scripted = ScriptedTranslator({
        "Throttled.": [status_error(groq.RateLimitError, 429,
                                    headers={"retry-after": "9"}), "ok"]
    })
    run(make_runner(workspace, scripted, sleep=no_sleep))
    assert no_sleep.waits == [9.0]


def test_connection_loss_is_retried(workspace, job_factory, no_sleep):
    job = job_factory(source="Flaky.")
    workspace.write_queue([job])
    scripted = ScriptedTranslator({
        "Flaky.": [connection_error(groq.APIConnectionError), "hi"]
    })
    summary = run(make_runner(workspace, scripted, sleep=no_sleep))
    assert summary.done == 1
    assert summary.errors == {"network": 1}


# ==========================================================================
# AC4 — the queue file is schema-valid at every point during the run
# ==========================================================================


def test_queue_file_is_schema_valid_at_every_transition(workspace, job_factory, schemas):
    """Validated from inside the provider call — i.e. with jobs mid-flight —
    and again on every state-change event, not merely once at the end."""
    checked = {"n": 0}

    def validate():
        jsonschema.validate(workspace.read_queue(), schemas["queue"])
        checked["n"] += 1

    async def translate(prompt):
        validate()  # queue is on disk with this job in_flight right now
        if "BAD." in prompt:
            raise status_error(groq.RateLimitError, 429)
        return "<translated>"

    stub = StubTranslator()
    stub.translate = translate

    jobs = [job_factory(source=f"Sentence {i}.", placeholders=["`x`"]) for i in range(6)]
    jobs.append(job_factory(source="BAD.", max_attempts=2))
    workspace.write_queue(jobs)

    runner = make_runner(workspace, stub, concurrency=3,
                         sleep=lambda _s: asyncio.sleep(0))
    runner._on_event = lambda kind, job: validate()
    summary = run(runner)

    assert checked["n"] > 10, "the file was barely inspected during the run"
    jsonschema.validate(workspace.read_queue(), schemas["queue"])
    assert summary.rejected == 1


def test_in_flight_is_on_disk_before_the_request_is_sent(workspace, job_factory):
    """Spec §4: a crash mid-request must leave the truth on disk."""
    seen = {}

    async def translate(prompt):
        job = workspace.jobs_by_id()[list(workspace.jobs_by_id())[0]]
        seen["state"] = job["state"]
        seen["started_at"] = job["started_at"]
        return "ok"

    stub = StubTranslator()
    stub.translate = translate
    workspace.write_queue([job_factory(source="Only one.")])
    run(make_runner(workspace, stub, concurrency=1))

    assert seen["state"] == "in_flight"
    assert seen["started_at"] is not None


def test_translation_memory_is_written_before_the_queue_says_done(
    workspace, job_factory
):
    """Reverse this ordering and a crash between the two writes loses a
    translation that was already paid for."""
    order = []
    job = job_factory(source="Ordering.")
    workspace.write_queue([job])

    runner = make_runner(workspace, StubTranslator(default="שלום"))
    tm = runner._tm("he")
    real_flush, real_save = runner._flush, tm.save

    def flush():
        real_flush()
        order.append(f"queue:{workspace.read_queue()['jobs'][0]['state']}")

    def save():
        real_save()
        order.append("tm")

    runner._flush = flush
    tm.save = save
    run(runner)

    assert order == ["queue:in_flight", "tm", "queue:done"]


def test_atomic_write_leaves_no_partial_file(workspace, job_factory, monkeypatch):
    workspace.write_queue([job_factory()])
    original = workspace.read_queue()

    import l10n_store

    def explode(_src, _dst):
        raise OSError("disk full")

    monkeypatch.setattr(l10n_store.os, "replace", explode)
    with pytest.raises(OSError):
        l10n_store.save_queue(workspace.queue_path, {"schema": "queue/v1"})

    assert workspace.read_queue() == original, "a failed write damaged the good file"
    leftovers = [p for p in (workspace.root / "l10n" / "queue").iterdir()
                 if p.name.startswith(".tmp-")]
    assert leftovers == []


# ==========================================================================
# AC5 — successful translations land in the TM with provenance
# ==========================================================================


def test_successful_translation_is_written_to_the_tm_with_provenance(
    workspace, job_factory, schemas
):
    fresh = job_factory(source="A fresh unit.")
    revised = job_factory(
        source="A revised unit.", action="REVISE",
        old_source="An older unit.", prior_translation="ישן",
    )
    russian = job_factory(source="A fresh unit.", lang="ru")
    workspace.write_queue([fresh, revised, russian])

    run(make_runner(workspace, StubTranslator(default="תרגום"), model="stub/model"))

    he = workspace.read_tm("he")
    jsonschema.validate(he, schemas["translation-memory"])
    assert he["schema"] == "tm/v1" and he["language"] == "he"

    entry = he["entries"][fresh["unit_hash"]]
    assert entry["source"] == "A fresh unit."
    assert entry["translation"] == "תרגום"
    assert entry["model"] == "stub/model"
    assert entry["prompt_version"] == groq_api.PROMPT_VERSION
    assert entry["review_status"] == "machine"
    assert entry["action"] == "TRANSLATE"
    assert entry["translated_at"].endswith("Z")

    assert he["entries"][revised["unit_hash"]]["action"] == "REVISE"

    ru = workspace.read_tm("ru")
    jsonschema.validate(ru, schemas["translation-memory"])
    assert ru["language"] == "ru"
    assert list(ru["entries"]) == [russian["unit_hash"]], "languages must not mix"


def test_tm_entries_are_serialized_with_sorted_keys(workspace, job_factory):
    jobs = [job_factory(source=f"S{i}.", unit_hash=h)
            for i, h in enumerate(["ffff000000000001", "0000000000000002",
                                   "aaaa000000000003"])]
    workspace.write_queue(jobs)
    run(make_runner(workspace, StubTranslator()))

    keys = list(workspace.read_tm("he")["entries"])
    assert keys == sorted(keys) != []


def test_a_rejected_job_never_enters_the_tm(workspace, job_factory, no_sleep):
    job = job_factory(source="Doomed: pass `--role executor`.",
                      placeholders=["`--role executor`"], max_attempts=2)
    workspace.write_queue([job])

    run(make_runner(workspace, StubTranslator(default="no placeholder here"),
                    sleep=no_sleep))

    assert workspace.jobs_by_id()[job["id"]]["state"] == "rejected"
    assert workspace.read_tm("he") is None, "a run with no success wrote a TM file"


# ==========================================================================
# AC6 — dry run
# ==========================================================================


def test_dry_run_reports_call_count_without_contacting_the_provider(
    workspace, job_factory
):
    known = job_factory(source="Cached.")
    fresh = job_factory(source="New.")
    russian = job_factory(source="New.", lang="ru")
    already = job_factory(source="Old news.")
    already.update(state="done", finished_at="2026-08-04T12:00:00Z")
    workspace.write_queue([known, fresh, russian, already])

    tm = queue_runner.TranslationMemory.load(workspace.tm_dir, "he")
    tm.upsert(known["unit_hash"], source=known["source"], translation="שמור",
              model="stub/model", prompt_version=groq_api.PROMPT_VERSION,
              action="TRANSLATE")
    tm.save()

    before = workspace.read_queue()
    runner = QueueRunner(workspace.queue_path, translator=None,
                         tm_dir=workspace.tm_dir, concurrency=5)
    report = runner.dry_run()

    assert report["estimated_api_calls"] == 2
    assert set(report["would_call"]) == {fresh["id"], russian["id"]}
    assert report["tm_hits"] == [known["id"]]
    assert report["already_terminal"] == [already["id"]]
    assert report["estimated_calls_by_lang"] == {"he": 1, "ru": 1}
    assert workspace.read_queue() == before, "--dry-run modified the queue file"


def test_dry_run_does_not_apply_the_restart_rule(workspace, job_factory):
    job = job_factory(source="Mid-flight.")
    job["state"] = "in_flight"
    workspace.write_queue([job])

    QueueRunner(workspace.queue_path, translator=None,
                tm_dir=workspace.tm_dir).dry_run()

    assert workspace.jobs_by_id()[job["id"]]["state"] == "in_flight"


def test_cli_dry_run_needs_no_api_key(workspace, job_factory, monkeypatch, capsys):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    workspace.write_queue([job_factory(source="X.")])

    code = queue_runner.main([workspace.queue_path, "--tm-dir", workspace.tm_dir,
                              "--dry-run", "--json"])
    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["estimated_api_calls"] == 1


# ==========================================================================
# The placeholder gate (spec §5)
# ==========================================================================


def test_placeholder_gate_passes_when_every_placeholder_survives():
    assert queue_runner.placeholder_gate(
        "Run `jira.sh` with <KEY>.", "הרץ `jira.sh` עם <KEY>.", ["`jira.sh`", "<KEY>"]
    ) is None


def test_placeholder_gate_names_what_was_lost():
    failure = queue_runner.placeholder_gate(
        "Use <PROJECT-KEY> here.", "השתמש כאן.", ["<PROJECT-KEY>"]
    )
    assert failure.kind == "placeholder_lost"
    assert "'<PROJECT-KEY>' occurs 1x in source, 0x in translation" in failure.detail
    assert failure.retryable


def test_placeholder_gate_counts_occurrences_not_just_presence():
    failure = queue_runner.placeholder_gate("`x` and `x`", "רק `x`", ["`x`"])
    assert failure is not None
    assert "2x in source, 1x in translation" in failure.detail


def test_extra_occurrences_are_allowed():
    assert queue_runner.placeholder_gate("`x`", "`x` וגם `x`", ["`x`"]) is None


def test_placeholder_failure_retries_with_a_corrective_prompt(
    workspace, job_factory, no_sleep
):
    job = job_factory(source="Pass --role executor to it.",
                      placeholders=["--role executor"], max_attempts=3)
    workspace.write_queue([job])

    scripted = ScriptedTranslator({
        "Pass --role executor": ["העבר לו את הדגל", "העבר לו --role executor"]
    })
    summary = run(make_runner(workspace, scripted, sleep=no_sleep))

    assert scripted.calls == 2
    assert summary.done == 1
    assert summary.errors == {"placeholder_lost": 1}
    assert "CORRECTION" in scripted.prompts[1]
    assert "'--role executor'" in scripted.prompts[1]
    assert "CORRECTION" not in scripted.prompts[0]


def test_placeholder_failure_exhausts_into_rejected(workspace, job_factory, no_sleep):
    job = job_factory(source="Keep <KEY>.", placeholders=["<KEY>"], max_attempts=2)
    workspace.write_queue([job])

    run(make_runner(workspace, StubTranslator(default="שמור על המפתח"),
                    sleep=no_sleep))

    stored = workspace.jobs_by_id()[job["id"]]
    assert stored["state"] == "rejected"
    assert stored["error"]["kind"] == "placeholder_lost"
    assert stored["attempts"] == 2


# ==========================================================================
# Error classification, backoff, prompt assembly, envelope extraction
# ==========================================================================


@pytest.mark.parametrize("exc,kind,retryable", [
    (status_error(groq.RateLimitError, 429), "rate_limit", True),
    (status_error(groq.InternalServerError, 500), "api_error", True),
    (status_error(groq.InternalServerError, 503), "api_error", True),
    (status_error(groq.AuthenticationError, 401), "api_error", False),
    (status_error(groq.PermissionDeniedError, 403), "api_error", False),
    (status_error(groq.BadRequestError, 400), "api_error", False),
    (status_error(groq.NotFoundError, 404), "api_error", False),
    (status_error(groq.UnprocessableEntityError, 422), "api_error", False),
    (connection_error(groq.APIConnectionError), "network", True),
    (connection_error(groq.APITimeoutError), "network", True),
    (asyncio.TimeoutError(), "network", True),
    (ConnectionResetError("reset by peer"), "network", True),
    (ValueError("bug in our own code"), "api_error", False),
])
def test_error_classification(exc, kind, retryable):
    failure = queue_runner.classify(exc)
    assert failure.kind == kind
    assert failure.retryable is retryable


def test_retry_after_is_read_from_the_response_headers():
    exc = status_error(groq.RateLimitError, 429, headers={"retry-after": "12.5"})
    assert queue_runner.classify(exc).retry_after == 12.5


def test_unparseable_retry_after_falls_back_to_our_own_backoff():
    exc = status_error(groq.RateLimitError, 429,
                       headers={"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"})
    assert queue_runner.classify(exc).retry_after is None


def test_backoff_grows_exponentially_is_jittered_and_is_capped(workspace, job_factory):
    workspace.write_queue([job_factory()])
    runner = make_runner(workspace, StubTranslator(), rng=random.Random(1))

    assert 0.5 <= runner.backoff(1) <= 1.0
    assert 1.0 <= runner.backoff(2) <= 2.0
    assert 2.0 <= runner.backoff(3) <= 4.0
    assert runner.backoff(50) <= queue_runner.BACKOFF_CAP

    spread = {round(runner.backoff(3), 6) for _ in range(20)}
    assert len(spread) > 1, "backoff is not jittered — retries will synchronise"


def test_backoff_never_undercuts_retry_after(workspace, job_factory):
    workspace.write_queue([job_factory()])
    runner = make_runner(workspace, StubTranslator(), rng=random.Random(1))
    assert runner.backoff(1, retry_after=30.0) == 30.0


def test_prompt_carries_context_and_the_revision_pair(job_factory):
    job = job_factory(
        source="New text.", action="REVISE", context="Guide › Setup",
        old_source="Old text.", prior_translation="טקסט ישן",
    )
    prompt = queue_runner.build_prompt(job)
    assert "New text." in prompt
    assert "Guide › Setup" in prompt
    assert "Do NOT translate it" in prompt
    assert "Old text." in prompt and "טקסט ישן" in prompt
    assert "REVISION" in prompt
    assert "Hebrew" in prompt


def test_prompt_omits_the_revision_block_for_a_plain_translate(job_factory):
    prompt = queue_runner.build_prompt(job_factory(source="Just this."))
    assert "REVISION" not in prompt
    assert "SECTION CONTEXT" not in prompt
    assert "CORRECTION" not in prompt


@pytest.mark.parametrize("raw,expected", [
    ('{"translation": "שלום"}', "שלום"),
    ('```json\n{"translation": "שלום"}\n```', "שלום"),
    ('{"translated_text": "שלום"}', "שלום"),
    ('{"anything_else": "שלום"}', "שלום"),
    ('"שלום"', "שלום"),
    ("plain text reply", "plain text reply"),
    ('  {"translation": "שלום"}  ', "שלום"),
])
def test_translation_envelope_extraction(raw, expected):
    assert queue_runner.extract_translation(raw) == expected


def test_extraction_keeps_json_looking_prose_intact():
    """A translation that merely starts with a brace must not be eaten."""
    assert queue_runner.extract_translation("{not json after all") == "{not json after all"


async def test_groq_translator_unwraps_the_envelope_and_asks_for_json():
    """The provider adapter is where the envelope is opened — `groq_api`'s
    prompt asks for JSON, and handing that raw to the TM stores the wrapper."""
    sent = {}

    class FakeCompletions:
        async def create(self, **kwargs):
            sent.update(kwargs)
            message = type("M", (), {"content": '{"translation": "{a: 1} תחביר"}'})()
            return type("C", (), {"choices": [type("Ch", (), {"message": message})()]})()

    client = type("Client", (), {
        "chat": type("Chat", (), {"completions": FakeCompletions()})()
    })()

    translator = queue_runner.GroqTranslator(client=client)
    assert await translator.translate("prompt") == "{a: 1} תחביר"
    assert sent["response_format"] == {"type": "json_object"}
    assert sent["model"] == groq_api.DEFAULT_MODEL


# ==========================================================================
# Queue construction scaffolding
# ==========================================================================


def test_build_queue_emits_schema_valid_deduplicated_jobs(schemas, tmp_path):
    import build_queue

    doc = tmp_path / "doc.md"
    doc.write_text(
        "# Title\n\nRun `jira.sh` with <KEY>.\n\n"
        "```bash\necho untouched\n```\n\nRun `jira.sh` with <KEY>.\n",
        encoding="utf-8",
    )
    jobs = build_queue.build([str(doc)], ["he", "ru"])
    queue = build_queue.new_queue("b" * 40, jobs)
    jsonschema.validate(queue, schemas["queue"])

    assert len({j["id"] for j in jobs}) == len(jobs), "ids are not unique"
    # The duplicated paragraph is one unit hash, so it is one job per language.
    per_lang = [j for j in jobs if j["lang"] == "he"]
    hashes = {j["unit_hash"] for j in per_lang}
    assert len(hashes) == len(per_lang), "the same unit was enqueued twice"
    assert all(j["state"] == "pending" and j["attempts"] == 0 for j in jobs)
    assert not any("echo untouched" in j["source"] for j in jobs), (
        "an opaque block became a translation job"
    )
