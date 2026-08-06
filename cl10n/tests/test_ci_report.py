"""`cl10n/ci_report.py` — the pull-request body CI hands to a human (AC3).

The body is the reviewer's only view of a run they did not watch, so what
these tests pin down is the reporting contract: segment counts by action, an
interrupted run saying so instead of pretending, and a missing report being
named rather than crashing the step that most needs to produce a body.
"""

from __future__ import annotations

from cl10n import ci_report  # noqa: E402
from cl10n import l10n_store  # noqa: E402


def _plan(**overrides):
    plan = {
        "langs": ["he", "ru"],
        "documents": 3,
        "source_commit": "a" * 40,
        "actions": {"TRANSLATE": 5, "REVISE": 2, "RECHECK": 1,
                    "REUSE": 90, "COPY": 4, "RETIRE": 1},
        "jobs": 14,
        "translation_memory_hits": 180,
        "restored_entries": {},
    }
    plan.update(overrides)
    return plan


def _queue(states):
    jobs = []
    for i, state in enumerate(states):
        job = l10n_store.new_job(
            unit_hash=f"{i:016x}", lang="he", action="TRANSLATE", source="x"
        )
        job["state"] = state
        jobs.append(job)
    return l10n_store.new_queue("a" * 40, jobs)


def test_body_reports_segment_counts_by_action():
    body = ci_report.build_body(_plan(), _queue(["done"] * 14), {
        "summary": {"files_rendered": 6, "units": 200, "translated": 200,
                    "opaque": 40, "fallbacks": 0, "violations": 0,
                    "retired_entries": []},
    })
    assert "| new (`TRANSLATE`) | 5 |" in body
    assert "| revised (`REVISE`) | 2 |" in body
    assert "| reused (`REUSE` + `RECHECK`) | 91 |" in body
    assert "| copied verbatim (`COPY`) | 4 |" in body
    assert "14/14 done" in body
    assert "200/200 unit(s) translated" in body
    assert "fallback" not in body.lower()


def test_an_interrupted_run_says_so():
    body = ci_report.build_body(
        _plan(),
        _queue(["done"] * 6 + ["pending"] * 5 + ["failed", "in_flight"] + ["rejected"]),
        {"summary": {"files_rendered": 6, "units": 200, "translated": 150,
                     "opaque": 40, "fallbacks": 50, "violations": 0,
                     "retired_entries": []}},
    )
    assert "6/14 done" in body
    assert "1 rejected" in body
    assert "7 not finished — the next run resumes them; nothing is re-billed" in body
    assert "50 unit(s) rendered as **English fallback**" in body


def test_recovered_entries_are_credited():
    body = ci_report.build_body(_plan(restored_entries={"he": 12}), None, None)
    assert "without re-billing: he: 12" in body


def test_missing_reports_are_named_not_fatal(tmp_path, capsys):
    # The run that died before rendering is exactly the one that needs a body.
    assert ci_report.main(["--plan", str(tmp_path / "absent.json")]) == 0
    out = capsys.readouterr().out
    assert "_No plan report found._" in out
    assert "_No render report found" in out


def test_writes_the_body_file(tmp_path):
    plan_path = tmp_path / "plan.json"
    ci_report.json.dump(_plan(), open(plan_path, "w"))
    out = tmp_path / "body.md"
    assert ci_report.main(["--plan", str(plan_path), "-o", str(out)]) == 0
    assert "## Continuous localization run" in out.read_text()
