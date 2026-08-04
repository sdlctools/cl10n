"""The orchestrator: `cl10n/cli.py` plan / run / render / status.

These tests run against a **real git repository** built in `tmp_path`, because
the plan step's whole job is recovering the previous revision through
`git cat-file blob` — a fake manifest pointing at nothing would test the
degenerate path only. The corpus is the project's own `md/` tree, so the
segmentation, the hashes and the placeholder set are the ones production uses.

No test needs an API key or touches the network: every run goes through
`CorpusStub`, which resolves a prompt back to its job by looking for the
longest job source contained in it. Matching on the source rather than on the
prompt's layout keeps these tests from breaking the next time the prompt gains
a section.

The acceptance criteria they stand for:

- **AC1** — a first-time full translation is the same `plan → run → render`
  sequence as an incremental update (`test_first_time_and_incremental_are_one_sequence`).
- **AC4** — an interrupted run loses no work and the next run resumes rather
  than restarting; demonstrated by killing a run mid-flight and counting the
  provider calls of the run that follows
  (`test_interrupted_run_resumes_without_retranslating`).
- **AC7** — changing one sentence sends only the affected segment
  (`test_one_sentence_edit_enqueues_only_that_unit`).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys

import jsonschema
import pytest

CL10N = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(CL10N)
sys.path[:0] = [CL10N, os.path.join(REPO, "app")]

import cli  # noqa: E402
import manifest as manifest_mod  # noqa: E402
import tree_diff  # noqa: E402
from queue_runner import QueueRunner  # noqa: E402

CORPUS = os.path.join(REPO, "md")


# --------------------------------------------------------------------------
# Provider stub
# --------------------------------------------------------------------------


class CorpusStub:
    """Translates by wrapping the job's own source in guillemets.

    Every placeholder therefore survives verbatim and the placeholder gate
    passes — which is what we want, because these tests are about *which* units
    get sent, not about what the model says. `fail_on` names sources that raise
    a terminal provider error instead, so the rejected → English-fallback path
    can be exercised.

    The prompt is resolved back to its job by cutting the job's source out of
    the prompt *exactly*: `build_prompt` places it between the template's
    final `Translate the following English text into <lang>:` line and the
    first appended section. Exact extraction rather than substring search,
    because the corpus is full of one-word table cells (`File`, `yes`) that
    also occur in the template's own rules — a containment match resolves
    those to the wrong job and then fails the placeholder gate.
    """

    model = "stub/model"

    MARK = "Translate the following English text into "
    # Every section `build_prompt` may append after the source, in order. The
    # output contract is unconditional, so a cut always happens.
    SECTIONS = (
        "\n\nSECTION CONTEXT",
        "\n\nThis is a REVISION",
        "\n\nCORRECTION —",
        "\n\nReturn exactly this JSON",
    )

    def __init__(self, jobs, fail_on=()):
        self.sources = {job["source"] for job in jobs}
        self.fail_on = set(fail_on)
        self.translated: list[str] = []
        self.limit: int | None = None

    @classmethod
    def _job_source(cls, prompt: str) -> str:
        cut = min(found for found in (prompt.find(m) for m in cls.SECTIONS) if found != -1)
        tail = prompt[:cut].split(cls.MARK, 1)[1]
        return tail.split(":\n\n", 1)[1].removesuffix("\n")

    async def translate(self, prompt: str) -> str:
        source = self._job_source(prompt)
        if source not in self.sources:  # pragma: no cover — a bug here
            raise AssertionError(f"prompt names an unknown source: {source[:80]!r}")
        if source in self.fail_on:
            raise ValueError("provider says no")
        if self.limit is not None and len(self.translated) >= self.limit:
            # The truest stand-in for a cancelled CI job: a BaseException the
            # runner does not catch, leaving whatever reached disk on disk.
            raise KeyboardInterrupt("interrupted")
        self.translated.append(source)
        return f"»{source}«"

    @property
    def calls(self) -> int:
        return len(self.translated)


# --------------------------------------------------------------------------
# A git-backed project
# --------------------------------------------------------------------------


class Project:
    """A throwaway repo plus the CLI invocations that act on it."""

    md_root = "md"
    tm_dir = "l10n/tm"
    manifest = "l10n/manifest.json"
    queue = "l10n/queue/queue.json"
    out_dir = "locales"

    def __init__(self, root):
        self.root = root

    # -- repository ----------------------------------------------------

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.root), *args],
            capture_output=True, text=True, check=True,
        ).stdout

    def write(self, rel: str, text: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def read(self, rel: str) -> str:
        return (self.root / rel).read_text(encoding="utf-8")

    def commit(self, message: str = "corpus") -> None:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)

    # -- commands ------------------------------------------------------

    def plan(self, *extra, langs="he", sources=()) -> dict:
        report = str(self.root / "plan-report.json")
        code = cli.main([
            "plan", *sources, "--langs", langs,
            "--md-root", self.md_root, "--tm-dir", self.tm_dir,
            "--manifest", self.manifest, "-o", self.queue,
            "--report", report, *extra,
        ])
        assert code == 0
        with open(report, encoding="utf-8") as fh:
            return json.load(fh)

    def render(self, *extra, langs="he", sources=(), expect=0) -> dict | None:
        report = str(self.root / "render-report.json")
        code = cli.main([
            "render", *sources, "--langs", langs,
            "--md-root", self.md_root, "--tm-dir", self.tm_dir,
            "--manifest", self.manifest, "--out-dir", self.out_dir,
            "--queue", self.queue, "--report", report, *extra,
        ])
        assert code == expect
        if not os.path.exists(report):
            return None
        with open(report, encoding="utf-8") as fh:
            return json.load(fh)

    def status(self, *extra, langs="he") -> int:
        return cli.main([
            "status", "--langs", langs, "--md-root", self.md_root,
            "--tm-dir", self.tm_dir, "--manifest", self.manifest,
            "--out-dir", self.out_dir, *extra,
        ])

    def run_queue(self, stub=None, *, concurrency=4, kill_after=None) -> CorpusStub:
        """Execute the current queue against a stub, optionally killing it."""
        queue = self.read_json(self.queue)
        stub = stub or CorpusStub(queue["jobs"])
        stub.limit = kill_after
        runner = QueueRunner(
            str(self.root / self.queue), translator=stub,
            tm_dir=str(self.root / self.tm_dir), concurrency=concurrency,
        )
        if kill_after is None:
            asyncio.run(runner.run())
        else:
            with pytest.raises(KeyboardInterrupt):
                asyncio.run(runner.run())
        return stub

    # -- state ---------------------------------------------------------

    def read_json(self, rel: str):
        with open(self.root / rel, encoding="utf-8") as fh:
            return json.load(fh)

    def tm(self, lang="he"):
        return self.read_json(f"{self.tm_dir}/{lang}.json")["entries"]

    def locale(self, rel: str, lang="he") -> str:
        return self.read(f"{self.out_dir}/{lang}/{rel}")


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "-C", str(root), "init", "-q", "-b", "main"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "t"], check=True)
    monkeypatch.chdir(root)
    return Project(root)


@pytest.fixture
def corpus_project(project):
    """The project's own `md/` tree, committed — the real segmentation."""
    shutil.copytree(CORPUS, project.root / "md")
    project.commit("corpus")
    return project


DOC = """# Guide

Read `config.json` before you start.

This paragraph explains the second step in some detail.

```bash
echo hello
```
"""


@pytest.fixture
def small_project(project):
    project.write("md/guide.md", DOC)
    project.commit("corpus")
    return project


# --------------------------------------------------------------------------
# plan
# --------------------------------------------------------------------------


def test_plan_against_no_previous_revision_translates_everything(small_project, schemas):
    report = small_project.plan()

    assert report["actions"]["TRANSLATE"] == 3  # heading + two paragraphs
    assert report["actions"]["COPY"] == 1  # the fence
    assert report["jobs"] == 3
    assert report["jobs_by_action"] == {"TRANSLATE": 3, "REVISE": 0}
    assert report["render_required"] is True
    assert report["files"][0]["previous_revision"] is False

    queue = small_project.read_json(small_project.queue)
    jsonschema.validate(queue, schemas["queue"])
    assert queue["source_commit"] == small_project.git("rev-parse", "HEAD").strip()
    assert all(job["state"] == "pending" for job in queue["jobs"])


def test_plan_writes_an_empty_queue_when_the_memory_is_complete(small_project, schemas):
    small_project.plan()
    small_project.run_queue()
    small_project.render()

    report = small_project.plan()
    assert report["jobs"] == 0
    assert report["actions"]["REUSE"] == 3
    assert report["translation_memory_hits"] == 3
    assert report["render_required"] is False
    # Still a schema-valid queue: `run` has something to be a no-op over.
    jsonschema.validate(small_project.read_json(small_project.queue), schemas["queue"])


def test_plan_deduplicates_a_unit_shared_by_two_documents(project):
    shared = "The same sentence appears in both documents."
    project.write("md/a.md", f"# A\n\n{shared}\n")
    project.write("md/b.md", f"# B\n\n{shared}\n")
    project.commit()

    report = project.plan()
    # Four unit occurrences, three distinct: the shared paragraph is one job.
    assert report["actions"]["TRANSLATE"] == 4
    assert report["jobs"] == 3


def test_plan_enqueues_per_language(small_project):
    report = small_project.plan(langs="he,ru")
    assert report["jobs_by_lang"] == {"he": 3, "ru": 3}
    assert report["jobs"] == 6


# --------------------------------------------------------------------------
# AC1 — one sequence for first-time and incremental
# --------------------------------------------------------------------------


def test_first_time_and_incremental_are_one_sequence(small_project):
    """The same three commands, twice. The second time nothing is billed."""
    small_project.plan()
    first = small_project.run_queue()
    small_project.render()
    rendered = small_project.locale("guide.md")

    small_project.plan()
    second = small_project.run_queue()
    small_project.render()

    assert first.calls == 3
    assert second.calls == 0
    assert small_project.locale("guide.md") == rendered


def test_a_new_language_needs_no_special_command(small_project):
    small_project.plan()
    small_project.run_queue()
    small_project.render()

    # `ru` has never been seen; it is planned against the same manifest entry
    # and comes back as a full translation, with no bootstrap flag anywhere.
    report = small_project.plan(langs="he,ru")
    assert report["jobs_by_lang"] == {"he": 0, "ru": 3}
    assert report["render_required"] is True


# --------------------------------------------------------------------------
# AC4 — interruption loses no work, and nothing is translated twice
# --------------------------------------------------------------------------


def test_interrupted_run_resumes_without_retranslating(corpus_project):
    total = corpus_project.plan()["jobs"]
    assert total > 50  # the real corpus, so "resume" means something

    killed = corpus_project.run_queue(concurrency=1, kill_after=20)
    assert killed.calls == 20

    # What reached disk before the kill is what the next run inherits.
    queue = corpus_project.read_json(corpus_project.queue)
    states = [job["state"] for job in queue["jobs"]]
    assert states.count("done") == 20
    assert len(corpus_project.tm()) == 20

    # The next run re-plans from scratch — no queue hand-off, no resume flag.
    resumed = corpus_project.plan()
    assert resumed["jobs"] == total - 20
    assert resumed["translation_memory_hits"] == 20

    second = corpus_project.run_queue()
    assert second.calls == total - 20
    # No segment translated twice, and none lost.
    assert not set(killed.translated) & set(second.translated)
    assert len(set(killed.translated) | set(second.translated)) == total
    assert len(corpus_project.tm()) == total


def test_resume_is_free_even_when_the_queue_file_is_lost(corpus_project):
    """A CI job that dies takes its `l10n/queue/` with it — that is fine.

    Resumption reads the translation memory, not the queue: the queue is a run
    artifact (spec §6) and is never committed.
    """
    total = corpus_project.plan()["jobs"]
    corpus_project.run_queue(concurrency=1, kill_after=15)
    os.remove(corpus_project.root / corpus_project.queue)

    resumed = corpus_project.plan()
    assert resumed["jobs"] == total - 15


def test_restore_tm_recovers_translations_that_were_never_committed(small_project, tmp_path):
    small_project.plan()
    small_project.run_queue()

    # Stand in for a crashed job's artifact: the memory it wrote, off to the
    # side, with the working copy wound back to empty.
    salvage = tmp_path / "salvage"
    salvage.mkdir()
    shutil.copy(small_project.root / small_project.tm_dir / "he.json", salvage / "he.json")
    shutil.rmtree(small_project.root / small_project.tm_dir)

    report = small_project.plan("--restore-tm", str(salvage))
    assert report["restored_entries"] == {"he": 3}
    assert report["jobs"] == 0


def test_restore_earlier_directories_win(small_project, tmp_path):
    """CI folds the PR branch in before the artifact — the fresher source first."""
    small_project.plan()
    small_project.run_queue()

    salvage_a, salvage_b = tmp_path / "a", tmp_path / "b"
    for salvage, marker in ((salvage_a, "from-pr-branch"), (salvage_b, "from-artifact")):
        salvage.mkdir()
        data = small_project.read_json(f"{small_project.tm_dir}/he.json")
        for entry in data["entries"].values():
            entry["translation"] = marker
        (salvage / "he.json").write_text(json.dumps(data), encoding="utf-8")
    shutil.rmtree(small_project.root / small_project.tm_dir)

    small_project.plan("--restore-tm", str(salvage_a), "--restore-tm", str(salvage_b))
    assert all(e["translation"] == "from-pr-branch" for e in small_project.tm().values())


def test_restore_never_overwrites_a_committed_entry(small_project, tmp_path):
    small_project.plan()
    small_project.run_queue()

    salvage = tmp_path / "salvage"
    salvage.mkdir()
    committed = small_project.read_json(f"{small_project.tm_dir}/he.json")
    stale = json.loads(json.dumps(committed))
    for entry in stale["entries"].values():
        entry["translation"] = "salvaged"
    (salvage / "he.json").write_text(json.dumps(stale), encoding="utf-8")

    # A human may have approved the committed one; the restored copy is only
    # ever a *replacement for something missing*.
    for entry in committed["entries"].values():
        entry["review_status"] = "approved"
    (small_project.root / small_project.tm_dir / "he.json").write_text(
        json.dumps(committed), encoding="utf-8"
    )

    small_project.plan("--restore-tm", str(salvage))
    assert all(e["translation"] != "salvaged" for e in small_project.tm().values())


# --------------------------------------------------------------------------
# Fallbacks come back
# --------------------------------------------------------------------------


def test_a_rejected_unit_is_enqueued_again_by_the_next_plan(small_project):
    queue = small_project.plan() and small_project.read_json(small_project.queue)
    doomed = queue["jobs"][1]["source"]
    stub = CorpusStub(queue["jobs"], fail_on=[doomed])
    small_project.run_queue(stub)

    report = small_project.render()
    assert report["summary"]["fallbacks"] == 1

    # The diff sees an unchanged document — every unit is REUSE — and the unit
    # is enqueued anyway, because the memory has no entry for it. Without this
    # the English fallback would be permanent.
    again = small_project.plan()
    assert again["actions"]["REUSE"] == 3
    assert again["jobs"] == 1
    assert again["render_required"] is True
    assert small_project.read_json(small_project.queue)["jobs"][0]["source"] == doomed


def test_an_empty_translation_is_treated_as_missing(small_project):
    small_project.plan()
    small_project.run_queue()

    tm_path = small_project.root / small_project.tm_dir / "he.json"
    data = json.loads(tm_path.read_text(encoding="utf-8"))
    victim = sorted(data["entries"])[0]
    data["entries"][victim]["translation"] = "   "
    tm_path.write_text(json.dumps(data), encoding="utf-8")

    report = small_project.plan()
    assert report["jobs"] == 1
    assert small_project.read_json(small_project.queue)["jobs"][0]["unit_hash"] == victim


# --------------------------------------------------------------------------
# AC7 — one sentence changed, one segment sent
# --------------------------------------------------------------------------


def test_one_sentence_edit_enqueues_only_that_unit(corpus_project):
    doc = "md/skills/_shared/project-config.md"
    corpus_project.plan()
    corpus_project.run_queue()
    corpus_project.render()
    assert corpus_project.plan()["jobs"] == 0

    before = corpus_project.read(doc)
    anchor = "a leftover copy at the root itself\nis ignored."
    assert anchor in before, "corpus fixture drifted — pick another sentence"
    corpus_project.write(
        doc, before.replace(anchor, anchor[:-1] + ", without a warning.")
    )
    corpus_project.commit("edit one sentence")

    report = corpus_project.plan()
    assert report["jobs"] == 1
    assert report["jobs_by_action"] == {"TRANSLATE": 0, "REVISE": 1}
    assert report["actions"]["REVISE"] == 1
    assert report["actions"]["TRANSLATE"] == 0
    # Only the edited document is dirty.
    dirty = [f["path"] for f in report["files"] if f["render_required"]]
    assert dirty == [doc]

    job = corpus_project.read_json(corpus_project.queue)["jobs"][0]
    assert job["action"] == "REVISE"
    assert "is ignored, without a warning." in job["source"]
    assert "is ignored." in job["old_source"]
    # The prior translation is what makes this cheaper and more consistent than
    # a fresh translation — and it is recovered from the *old* revision's hash.
    assert job["prior_translation"] == f"»{job['old_source']}«"

    # And that is what actually reaches the provider: one call, the edited
    # segment — not the paragraph's section, not the document.
    stub = corpus_project.run_queue()
    assert stub.calls == 1
    assert stub.translated == [job["source"]]


def test_reflowing_a_paragraph_costs_nothing(corpus_project):
    doc = "md/skills/_shared/project-config.md"
    corpus_project.plan()
    corpus_project.run_queue()
    corpus_project.render()

    text = corpus_project.read(doc)
    target = "All project-specific values live in these two files"
    assert target in text, "corpus fixture drifted — pick another sentence"
    corpus_project.write(doc, text.replace(target, target.replace(" ", "\n", 3)))
    corpus_project.commit("reflow")

    # Canonicalisation plus whitespace-insensitive hashing: a no-op (the
    # property `tree-diff-spec.md` calls load-bearing, asserted end to end).
    assert corpus_project.plan()["jobs"] == 0


# --------------------------------------------------------------------------
# render and the manifest
# --------------------------------------------------------------------------


def test_render_writes_locales_and_a_valid_manifest(corpus_project, schemas):
    corpus_project.plan(langs="he,ru")
    corpus_project.run_queue()
    report = corpus_project.render(langs="he,ru")

    manifest = corpus_project.read_json(corpus_project.manifest)
    jsonschema.validate(manifest, schemas["manifest"])
    assert sorted(manifest["languages"]) == ["he", "ru"]
    assert set(manifest["files"]) == {
        f"md/{p}" for p in (
            "skills/_shared/jira-api-reference.md",
            "skills/_shared/project-config.md",
            "skills/jira-task-assigner/SKILL.md",
        )
    }
    entry = manifest["files"]["md/skills/_shared/project-config.md"]
    assert entry["source_blob"] == corpus_project.git(
        "rev-parse", "HEAD:md/skills/_shared/project-config.md"
    ).strip()
    assert entry["localized"]["he"]["run_id"] == report["summary"]["run_id"]
    assert entry["localized"]["he"]["fallbacks"] == []
    assert entry["unit_hashes"] and entry["opaque_hashes"]

    text = corpus_project.locale("skills/_shared/project-config.md", "ru")
    assert "»" in text  # spliced, not copied


def test_render_records_fallback_hashes_in_the_manifest(small_project):
    queue = small_project.plan() and small_project.read_json(small_project.queue)
    doomed = queue["jobs"][1]
    small_project.run_queue(CorpusStub(queue["jobs"], fail_on=[doomed["source"]]))
    small_project.render()

    entry = small_project.read_json(small_project.manifest)["files"]["md/guide.md"]
    assert entry["localized"]["he"]["fallbacks"] == [doomed["unit_hash"]]
    # The fallback renders as English rather than as nothing.
    assert doomed["source"] in small_project.locale("guide.md")


def test_render_dry_run_writes_nothing(small_project):
    small_project.plan()
    small_project.run_queue()
    small_project.render("--dry-run")

    assert not os.path.exists(small_project.root / small_project.out_dir)
    assert not os.path.exists(small_project.root / small_project.manifest)


def test_render_carries_a_changed_code_fence_with_no_api_call(small_project):
    small_project.plan()
    small_project.run_queue()
    small_project.render()

    small_project.write("md/guide.md", DOC.replace("echo hello", "echo goodbye"))
    small_project.commit("change the fence")

    report = small_project.plan()
    assert report["jobs"] == 0
    assert report["actions"]["COPY"] == 1
    # Spec §2: COPY changes the output without touching the queue, so a
    # render-only-when-the-queue-is-non-empty pipeline would ship a stale fence.
    assert report["render_required"] is True
    small_project.render()
    assert "echo goodbye" in small_project.locale("guide.md")


# --------------------------------------------------------------------------
# RETIRE garbage collection
# --------------------------------------------------------------------------


def test_retired_units_leave_the_memory_after_a_full_render(small_project):
    small_project.plan()
    small_project.run_queue()
    small_project.render()
    assert len(small_project.tm()) == 3

    dropped = [
        h for h, entry in small_project.tm().items()
        if entry["source"].startswith("This paragraph explains")
    ]
    assert len(dropped) == 1
    small_project.write(
        "md/guide.md",
        DOC.replace("This paragraph explains the second step in some detail.\n\n", ""),
    )
    small_project.commit("drop a paragraph")

    report = small_project.plan()
    assert report["actions"]["RETIRE"] == 1
    small_project.render()
    assert dropped[0] not in small_project.tm()
    assert len(small_project.tm()) == 2


def test_a_unit_two_documents_share_survives_one_of_them_dropping_it(project):
    shared = "The same sentence appears in both documents.\n"
    project.write("md/a.md", f"# A\n\n{shared}")
    project.write("md/b.md", f"# B\n\n{shared}")
    project.commit()
    project.plan()
    project.run_queue()
    project.render()

    shared_hash = next(
        h for h, entry in project.tm().items() if entry["source"].startswith("The same")
    )
    project.write("md/a.md", "# A\n")
    project.commit("a drops the shared paragraph")

    project.plan()
    project.render()
    # Content-addressed hashes mean one entry with two references; retiring on
    # the first RETIRE would break `b.md` (spec §2's GC rule).
    assert shared_hash in project.tm()
    assert "The same sentence" in project.locale("b.md")


def test_a_single_file_render_never_collects_garbage(project):
    project.write("md/a.md", "# A\n\nOnly in a.\n")
    project.write("md/b.md", "# B\n\nOnly in b.\n")
    project.commit()
    project.plan()
    project.run_queue()

    # Rendering one file leaves the ledger describing documents this run never
    # inventoried — collecting against it would delete the other file's work.
    project.render(sources=["md/a.md"])
    assert len(project.tm()) == 4


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------


def test_status_reports_coverage_and_staleness(small_project, capsys):
    assert small_project.status() == 0
    assert small_project.status("--fail-on-incomplete") == 1

    small_project.plan()
    small_project.run_queue()
    small_project.render()
    capsys.readouterr()

    assert small_project.status("--fail-on-incomplete") == 0
    capsys.readouterr()
    assert small_project.status("--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["totals"]["he"] == {
        "units": 3, "translated": 3, "missing": 0, "fallbacks": 0, "stale_documents": 0,
    }
    assert payload["documents"][0]["langs"]["he"]["rendered"] is True

    small_project.write("md/guide.md", DOC.replace("# Guide", "# Guide, revised"))
    small_project.commit("retitle")
    assert small_project.status("--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["totals"]["he"]["stale_documents"] == 1
    assert payload["totals"]["he"]["missing"] == 1


# --------------------------------------------------------------------------
# Degenerate revisions
# --------------------------------------------------------------------------


def test_an_unreachable_blob_plans_against_the_empty_document(small_project):
    small_project.plan()
    small_project.run_queue()
    small_project.render()

    # A shallow clone, or a manifest carried across a rewritten history.
    manifest = small_project.read_json(small_project.manifest)
    manifest["files"]["md/guide.md"]["source_blob"] = "0" * 39 + "1"
    (small_project.root / small_project.manifest).write_text(
        json.dumps(manifest), encoding="utf-8"
    )

    report = small_project.plan()
    # Everything looks new to the diff — and every unit is still a memory hit,
    # so the degenerate case costs nothing (spec §1).
    assert report["actions"]["TRANSLATE"] == 3
    assert report["jobs"] == 0


def test_previous_source_is_empty_when_git_cannot_answer(small_project):
    manifest = manifest_mod.load(str(small_project.root / small_project.manifest), ["he"])
    manifest["files"]["md/guide.md"] = {"source_blob": "f" * 40, "localized": {}}
    assert manifest_mod.previous_source(manifest, "md/guide.md", str(small_project.root)) == ""


def test_inventory_matches_tree_diff_segmentation(small_project):
    source = small_project.read("md/guide.md")
    doc_hash, unit_hashes, opaque_hashes = manifest_mod.inventory(source)

    assert len(doc_hash) == 16
    assert set(unit_hashes) == set(tree_diff.tm_keys(manifest_mod.canonicalise(source)))
    assert len(opaque_hashes) == 1
