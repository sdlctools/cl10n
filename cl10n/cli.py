"""`cl10n` — the pipeline's command line, and the orchestrator behind it.

One entry point with four subcommands, so a human and a CI job drive the
*same* code path rather than two implementations that agree until they don't:

    venv/bin/python3 cl10n/cli.py plan   --langs he,ru
    venv/bin/python3 cl10n/cli.py run    l10n/queue/queue.json -c 8
    venv/bin/python3 cl10n/cli.py render --langs he,ru
    venv/bin/python3 cl10n/cli.py status --langs he,ru

`plan` is step 3-6 of `.claude/rules/l10n-pipeline-spec.md` — the real enqueue
step: recover each document's last-localized revision through the manifest and
`git cat-file blob`, diff it against the working tree, apply the actions that
need no API call, and write a queue file. `run` is `queue_runner`, unchanged.
`render` is `reassemble` plus the manifest bookkeeping and RETIRE garbage
collection that the reassembly component deliberately leaves to an
orchestrator. `status` answers "how much of the corpus is translated" without
running anything.

**There is no bootstrap mode, and adding one would be a bug.** A first-time
full translation is `plan → run → render` — the identical sequence an
incremental update uses. It differs only in what step 3 recovers: nothing. A
document with no manifest entry, or whose blob is unreachable in a shallow
clone, is planned against the empty document (spec §1's degenerate case) and
every unit comes back `TRANSLATE`.

## What decides that a unit becomes a job

The diff is not the only input, and this is the one place this orchestrator is
deliberately stricter than a naive reading of the spec:

    enqueue (lang, unit) ⟺ the new document contains the unit
                            AND l10n/tm/<lang>.json has no entry for it
                                at the current PROMPT_VERSION

The tree diff supplies the *action* — `REVISE` (with the old source and the
prior translation) where it found a fuzzy predecessor, `TRANSLATE` otherwise —
but never the decision to skip. That falls out of the translation memory, which
is `tree-diff-spec.md`'s "you may not need the diff at all": what is
untranslated is the set difference `tm_keys(new) − tm.keys()`.

Two properties follow, and both are load-bearing for CI:

- **Nothing is translated twice.** A unit whose translation is already in the
  memory never becomes a job — whether it landed a minute ago in an
  interrupted run, or a year ago in another document that happens to contain
  the same paragraph. Hashes are content-addressed, so this is exact.
- **Nothing stays lost.** A unit the diff calls `REUSE` but whose entry is
  missing — the classic case being a unit that ended `rejected` and rendered as
  an English fallback — is enqueued again on the next run. Without this the
  manifest would record the document as localized at that revision, the next
  diff would report `REUSE`, and the fallback would be permanent.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
# Bare scripts in a directory rather than an installed package — the repo's
# existing convention, see `cl10n/queue_runner.py` and `cl10n/reassemble.py`.
sys.path[:0] = [_HERE, os.path.join(os.path.dirname(_HERE), "app")]

from markdown_it.tree import SyntaxTreeNode  # noqa: E402

import groq_api  # noqa: E402  (app/ — PROMPT_VERSION only; its client is lazy)
import manifest as manifest_mod  # noqa: E402
import queue_runner  # noqa: E402
import reassemble  # noqa: E402
import tree_diff  # noqa: E402
from l10n_store import (  # noqa: E402
    TranslationMemory,
    atomic_write_text,
    new_job,
    new_queue,
    new_run_id,
    save_queue,
    utc_now,
)
from utils import markdown_to_ast  # noqa: E402

DEFAULT_MD_ROOT = "md"
DEFAULT_TM_DIR = "l10n/tm"
DEFAULT_LOCALES_ROOT = "locales"
DEFAULT_QUEUE = "l10n/queue/queue.json"
DEFAULT_MANIFEST = manifest_mod.DEFAULT_MANIFEST
DEFAULT_LANGS = "he,ru"

# `tree_diff` actions in the order a reader wants them: what cost money, what
# was recovered, what was carried through untouched.
ACTION_ORDER = ("TRANSLATE", "REVISE", "RECHECK", "REUSE", "COPY", "RETIRE")


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------


def repo_root(start: str | None = None) -> str:
    """The git worktree root, falling back to this file's parent directory."""
    out = manifest_mod._git(start or os.getcwd(), "rev-parse", "--show-toplevel")
    root = (out or "").strip()
    return root or os.path.dirname(_HERE)


def corpus(sources, md_root: str) -> list[str]:
    """Explicit files, or every Markdown file under `md_root` in a stable order."""
    if sources:
        return list(sources)
    return sorted(glob.glob(os.path.join(md_root, "**", "*.md"), recursive=True))


def rel_to_repo(path: str, repo: str) -> str:
    """Manifest key form: repo-relative, forward slashes (`md/a/b.md`)."""
    return os.path.relpath(os.path.abspath(path), repo).replace(os.sep, "/")


def split_langs(value: str) -> list[str]:
    return [lang.strip() for lang in value.split(",") if lang.strip()]


def unit_index(md: str) -> dict[str, dict]:
    """`{unit_hash: {source, context, placeholders}}` for one document.

    Everything the queue needs about a unit, taken from the same segmentation
    the planner diffs and the renderer splices — `tree_diff`'s, via its private
    helpers exactly as `reassemble` does, so a rename there breaks this import
    instead of letting a second definition of "translation unit" drift in.
    """
    root = SyntaxTreeNode(markdown_to_ast(manifest_mod.canonicalise(md)))
    tree_diff.hash_tree(root)
    out: dict[str, dict] = {}
    for unit in tree_diff._units_under(root):
        # First occurrence wins on context — spec §2's dedup rule, applied
        # within a document as well as across them.
        out.setdefault(unit.h, {
            "source": tree_diff._unit_source(unit),
            "context": tree_diff._heading_trail(unit),
            "placeholders": tree_diff._placeholders(unit),
        })
    return out


def has_usable_entry(tm: TranslationMemory, unit_hash: str) -> bool:
    """Is `unit_hash` already translated at the current prompt version?

    An entry with an empty translation is not one: the renderer refuses to
    splice it and falls back to English, so leaving it here would make the
    fallback permanent.
    """
    entry = tm.get(unit_hash)
    return bool(
        entry
        and entry.get("prompt_version") == groq_api.PROMPT_VERSION
        and (entry.get("translation") or "").strip()
    )


def write_json(path: str, payload) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


# --------------------------------------------------------------------------
# plan
# --------------------------------------------------------------------------


def restore_memory(tms: dict[str, TranslationMemory], restore_dirs) -> dict[str, int]:
    """Fold recovered translation memories into the working one.

    CI's crash recovery: a run killed between its API calls and its commit has
    translations that exist only in an unmerged PR branch or a build artifact.
    Restoring them is what turns "no work lost" from "re-translated at cost"
    into "not re-translated".

    **The committed entry always wins**, and earlier directories win over later
    ones. Entries are keyed by a hash of their source, so two entries under one
    key are translations of identical text and either would render correctly —
    but the committed one may carry `review_status: "approved"`, which is a
    human's work and the only thing here that cannot be regenerated.
    """
    added: dict[str, int] = {}
    for restore_dir in restore_dirs:
        for lang, tm in tms.items():
            recovered = TranslationMemory.load(restore_dir, lang)
            count = 0
            for unit_hash, entry in recovered.entries.items():
                if unit_hash not in tm.entries:
                    tm.entries[unit_hash] = entry
                    count += 1
            if count:
                added[lang] = added.get(lang, 0) + count
    return added


def plan_corpus(
    paths: list[str],
    langs: list[str],
    *,
    repo: str,
    tm_dir: str,
    manifest_path: str,
    max_attempts: int = 3,
    restore_tm: list[str] | None = None,
) -> tuple[list[dict], dict]:
    """Plan the corpus into `(jobs, report)`. Writes only the translation memory.

    The memory is written because `RECHECK` and a restore are *state changes
    with no API call* (spec §2's "apply no-API actions"): they must survive
    whether or not anything is enqueued afterwards. The queue, the manifest and
    the locales belong to the caller.
    """
    man = manifest_mod.load(manifest_path, langs)
    tms = {lang: TranslationMemory.load(tm_dir, lang) for lang in langs}
    restored = restore_memory(tms, restore_tm or [])

    jobs: dict[str, dict] = {}
    totals = {action: 0 for action in ACTION_ORDER}
    files: list[dict] = []
    rechecked = 0
    tm_hits = 0
    dirty = False

    for path in paths:
        rel = rel_to_repo(path, repo)
        with open(path, "r", encoding="utf-8") as fh:
            new_md = fh.read()
        old_md = manifest_mod.previous_source(man, rel, repo)

        items = tree_diff.plan(
            manifest_mod.canonicalise(old_md), manifest_mod.canonicalise(new_md)
        )
        index = unit_index(new_md)

        # The diff contributes the action, not the decision to enqueue.
        revise_of: dict[str, str] = {}
        recheck: set[str] = set()
        tally = {action: 0 for action in ACTION_ORDER}
        for item in items:
            tally[item.action] = tally.get(item.action, 0) + 1
            if item.action == "REVISE":
                revise_of[item.unit_hash] = item.old_source
            elif item.action == "RECHECK":
                recheck.add(item.unit_hash)
        for action, count in tally.items():
            totals[action] = totals.get(action, 0) + count

        # `prior_translation` needs the *old* unit's hash, which the work item
        # does not carry — but its `old_source` is exactly the string the old
        # revision's `tm_keys` is keyed by, so one inversion recovers it.
        old_hash_of: dict[str, str] = {}
        if old_md and revise_of:
            for unit_hash, source in tree_diff.tm_keys(
                manifest_mod.canonicalise(old_md)
            ).items():
                old_hash_of.setdefault(source, unit_hash)

        file_jobs = 0
        for lang in langs:
            tm = tms[lang]
            for unit_hash, unit in index.items():
                if unit_hash in recheck:
                    # Content identical, position changed: keep the translation,
                    # flag it for a human (spec §2). Not an API call.
                    entry = tm.get(unit_hash)
                    if entry and entry.get("review_status") == "machine":
                        entry["review_status"] = "recheck"
                        rechecked += 1
                if has_usable_entry(tm, unit_hash):
                    tm_hits += 1
                    continue
                job_id = f"{lang}:{unit_hash}"
                if job_id in jobs:
                    continue
                old_source = revise_of.get(unit_hash)
                prior = None
                if old_source:
                    prior_entry = tm.get(old_hash_of.get(old_source, ""))
                    prior = (prior_entry or {}).get("translation")
                jobs[job_id] = new_job(
                    unit_hash=unit_hash,
                    lang=lang,
                    action="REVISE" if old_source else "TRANSLATE",
                    source=unit["source"],
                    context=unit["context"],
                    placeholders=unit["placeholders"],
                    old_source=old_source or None,
                    prior_translation=prior,
                    max_attempts=max_attempts,
                )
                file_jobs += 1

        # Spec §2: render whenever the plan emitted anything but pure REUSE —
        # a changed code fence (COPY) has no job and still changes the output.
        # Two more cases the diff cannot see: a job enqueued against an
        # otherwise-unchanged document (an English fallback being retried), and
        # a language that has never completed this file at all.
        file_dirty = (
            file_jobs > 0
            or any(count for action, count in tally.items() if action != "REUSE")
            or any(
                lang not in man["files"].get(rel, {}).get("localized", {})
                for lang in langs
            )
        )
        dirty = dirty or file_dirty
        files.append({
            "path": rel,
            "units": len(index),
            "actions": {a: c for a, c in tally.items() if c},
            "jobs": file_jobs,
            "previous_revision": bool(old_md),
            "render_required": file_dirty,
        })

    for tm in tms.values():
        # An empty memory is not written: a first-ever plan should not commit
        # `l10n/tm/<lang>.json` files containing nothing.
        if tm.entries:
            tm.save()

    job_list = list(jobs.values())
    report = {
        "langs": langs,
        "documents": len(paths),
        "actions": totals,
        "jobs": len(job_list),
        "jobs_by_lang": {
            lang: sum(1 for j in job_list if j["lang"] == lang) for lang in langs
        },
        "jobs_by_action": {
            action: sum(1 for j in job_list if j["action"] == action)
            for action in ("TRANSLATE", "REVISE")
        },
        "translation_memory_hits": tm_hits,
        "rechecked": rechecked,
        "restored_entries": restored,
        "render_required": dirty,
        "files": files,
    }
    return job_list, report


def cmd_plan(args) -> int:
    repo = repo_root()
    paths = corpus(args.sources, args.md_root)
    if not paths:
        print(f"no source markdown found under {args.md_root}", file=sys.stderr)
        return 2

    langs = split_langs(args.langs)
    jobs, report = plan_corpus(
        paths, langs,
        repo=repo,
        tm_dir=args.tm_dir,
        manifest_path=args.manifest,
        max_attempts=args.max_attempts,
        restore_tm=args.restore_tm,
    )

    queue = new_queue(manifest_mod.head_commit(repo), jobs, run_id=args.run_id)
    report["run_id"] = queue["run_id"]
    report["source_commit"] = queue["source_commit"]
    report["queue"] = args.out
    # Written even when empty: `run` then has a queue to be a no-op over, and
    # CI does not need a branch for "nothing to translate".
    save_queue(args.out, queue)
    if args.report:
        write_json(args.report, report)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"PLAN {queue['run_id']} — {report['documents']} document(s), "
              f"{len(langs)} language(s)")
        print("  " + "  ".join(
            f"{action}={report['actions'][action]}" for action in ACTION_ORDER
        ))
        print(f"  {report['jobs']} job(s) → {args.out}"
              f"  {report['jobs_by_lang'] if report['jobs'] else ''}")
        print(f"  {report['translation_memory_hits']} unit(s) already in the "
              f"translation memory, {report['rechecked']} flagged for recheck")
        if report["restored_entries"]:
            print(f"  restored from {args.restore_tm}: {report['restored_entries']}")
        print(f"  render required: {'yes' if report['render_required'] else 'no'}")
    return 0


# --------------------------------------------------------------------------
# run — the queue runner, unchanged
# --------------------------------------------------------------------------


def cmd_run(args) -> int:
    """Delegate verbatim to `queue_runner`.

    Not reimplemented and not wrapped: the runner owns concurrency, retries,
    the rate-limit gate and the placeholder gate, and a second copy of its
    argument handling here is a second thing to keep in step.
    """
    argv = list(args.runner_args)
    if not argv or argv[0].startswith("-"):
        argv.insert(0, DEFAULT_QUEUE)
    return queue_runner.main(argv)


# --------------------------------------------------------------------------
# render
# --------------------------------------------------------------------------


def cmd_render(args) -> int:
    repo = repo_root()
    paths = corpus(args.sources, args.md_root)
    if not paths:
        print(f"no source markdown found under {args.md_root}", file=sys.stderr)
        return 2

    langs = split_langs(args.langs)
    run_id = args.run_id or _run_id_from_queue(args.queue) or new_run_id()
    man = manifest_mod.load(args.manifest, langs)
    tms = {lang: TranslationMemory.load(args.tm_dir, lang) for lang in langs}

    reports: list[reassemble.RenderReport] = []
    broken = 0
    completed_at = utc_now()

    for path in paths:
        rel = rel_to_repo(path, repo)
        with open(path, "r", encoding="utf-8") as fh:
            source_md = fh.read()
        doc_hash, unit_hashes, opaque_hashes = manifest_mod.inventory(source_md)

        rendered_langs: list[tuple[str, list[str]]] = []
        for lang in langs:
            out = reassemble.locale_path(path, lang, args.md_root, args.out_dir)
            try:
                report = reassemble.render_file(
                    path, tms[lang].entries, out, lang=lang,
                    write=not args.dry_run, verify=args.verify,
                )
            except reassemble.StructureMismatch as exc:
                # Not written, not recorded: a manifest entry claiming a
                # localization whose file was refused is worse than no entry.
                broken += 1
                print(f"STRUCTURE MISMATCH {exc}", file=sys.stderr)
                continue
            reports.append(report)
            rendered_langs.append((lang, report.fallback_hashes))
            print(report.as_text())
            for violation in report.violations:
                print(f"  placeholder violation #{violation.unit_hash}: {violation.detail}",
                      file=sys.stderr)

        if args.dry_run or not rendered_langs:
            continue
        manifest_mod.record_revision(
            man, rel,
            source_blob=manifest_mod.blob_sha(repo, path),
            doc_hash=doc_hash,
            unit_hashes=unit_hashes,
            opaque_hashes=opaque_hashes,
        )
        for lang, fallbacks in rendered_langs:
            manifest_mod.record_localized(
                man, rel, lang,
                run_id=run_id, completed_at=completed_at, fallbacks=fallbacks,
            )

    retired: list[str] = []
    if not args.dry_run:
        manifest_mod.save(args.manifest, man)
        if args.gc and not args.sources and not broken:
            # Only after a clean full-corpus render: `referenced` has to be the
            # union over the whole ledger, and a partial render leaves the
            # ledger describing documents this run never inventoried.
            referenced = manifest_mod.referenced_hashes(man)
            for lang, tm in tms.items():
                dead = manifest_mod.collect_garbage(tm, referenced)
                if dead:
                    tm.save()
                    retired.extend(f"{lang}:{h}" for h in dead)

    fallbacks = sum(len(r.fallbacks) for r in reports)
    violations = sum(len(r.violations) for r in reports)
    summary = {
        "run_id": run_id,
        "langs": langs,
        "files_rendered": len(reports),
        "units": sum(r.units for r in reports),
        "translated": sum(r.translated for r in reports),
        "opaque": sum(r.opaque for r in reports),
        "fallbacks": fallbacks,
        "violations": violations,
        "structure_mismatches": broken,
        "retired_entries": retired,
    }
    print(f"\n{len(reports)} file(s) rendered across {len(langs)} language(s); "
          f"{fallbacks} English fallback(s), {violations} placeholder violation(s)"
          + (f", {len(retired)} retired memory entr(y|ies)" if retired else "")
          + (f", {broken} NOT WRITTEN (structure mismatch)" if broken else "")
          + (" [dry run — nothing written]" if args.dry_run else ""))

    if args.report and not args.dry_run:
        write_json(args.report, {
            "summary": summary,
            "files": [r.as_dict() for r in reports],
        })
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))

    if broken or violations:
        return 1
    if args.fail_on_fallback and fallbacks:
        return 1
    return 0


def _run_id_from_queue(path: str | None) -> str | None:
    """Reuse the queue's run id so the manifest points at the run that paid."""
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh).get("run_id")
    except (json.JSONDecodeError, OSError):
        return None


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------


def cmd_status(args) -> int:
    repo = repo_root()
    paths = corpus(args.sources, args.md_root)
    if not paths:
        print(f"no source markdown found under {args.md_root}", file=sys.stderr)
        return 2

    langs = split_langs(args.langs)
    man = manifest_mod.load(args.manifest, langs)
    tms = {lang: TranslationMemory.load(args.tm_dir, lang) for lang in langs}

    documents = []
    for path in paths:
        rel = rel_to_repo(path, repo)
        with open(path, "r", encoding="utf-8") as fh:
            source_md = fh.read()
        doc_hash, unit_hashes, _ = manifest_mod.inventory(source_md)
        entry = man["files"].get(rel, {})
        distinct = list(dict.fromkeys(unit_hashes))

        by_lang = {}
        for lang in langs:
            tm = tms[lang]
            translated = [h for h in distinct if has_usable_entry(tm, h)]
            localized = entry.get("localized", {}).get(lang)
            by_lang[lang] = {
                "translated": len(translated),
                "missing": len(distinct) - len(translated),
                "review": _review_counts(tm, translated),
                "rendered": os.path.exists(
                    reassemble.locale_path(path, lang, args.md_root, args.out_dir)
                ),
                # The manifest records the revision that was localized; a
                # different doc_hash today means the source moved since.
                "up_to_date": bool(localized) and entry.get("doc_hash") == doc_hash,
                "fallbacks": len((localized or {}).get("fallbacks", [])),
            }
        documents.append({
            "path": rel,
            "units": len(distinct),
            "localized_revision": bool(entry.get("source_blob")),
            "langs": by_lang,
        })

    totals = {
        lang: {
            "units": sum(d["units"] for d in documents),
            "translated": sum(d["langs"][lang]["translated"] for d in documents),
            "missing": sum(d["langs"][lang]["missing"] for d in documents),
            "fallbacks": sum(d["langs"][lang]["fallbacks"] for d in documents),
            "stale_documents": sum(
                0 if d["langs"][lang]["up_to_date"] else 1 for d in documents
            ),
        }
        for lang in langs
    }
    payload = {"documents": documents, "totals": totals}

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"{len(documents)} document(s) under {args.md_root}, "
              f"{totals[langs[0]]['units']} translation unit(s)\n")
        for lang in langs:
            t = totals[lang]
            pct = 100.0 * t["translated"] / t["units"] if t["units"] else 100.0
            print(f"{lang}: {t['translated']}/{t['units']} units translated "
                  f"({pct:.1f}%), {t['fallbacks']} fallback(s), "
                  f"{t['stale_documents']} document(s) needing a render")
            for doc in documents:
                d = doc["langs"][lang]
                flags = "".join((
                    " " if d["up_to_date"] else "*",
                    "" if d["rendered"] else " [not rendered]",
                ))
                print(f"   {flags} {doc['path']}: {d['translated']}/{doc['units']}"
                      + (f", {d['fallbacks']} fallback(s)" if d["fallbacks"] else ""))
        print("\n('*' = the source changed since the last render)")

    if args.fail_on_incomplete and any(t["missing"] for t in totals.values()):
        return 1
    return 0


def _review_counts(tm: TranslationMemory, hashes) -> dict:
    counts: dict[str, int] = {}
    for unit_hash in hashes:
        status = (tm.get(unit_hash) or {}).get("review_status", "unknown")
        counts[status] = counts.get(status, 0) + 1
    return counts


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cl10n",
        description="Continuous localization pipeline: plan, run, render, status.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("sources", nargs="*", help="markdown files (default: <md-root>/**/*.md)")
        p.add_argument("--langs", default=DEFAULT_LANGS, help="comma-separated target languages")
        p.add_argument("--md-root", default=DEFAULT_MD_ROOT)
        p.add_argument("--tm-dir", default=DEFAULT_TM_DIR)
        p.add_argument("--manifest", default=DEFAULT_MANIFEST)
        p.add_argument("--json", action="store_true", help="machine-readable output")

    p_plan = sub.add_parser("plan", help="diff the corpus against the manifest and write a queue")
    common(p_plan)
    p_plan.add_argument("-o", "--out", default=DEFAULT_QUEUE, help="queue file to write")
    p_plan.add_argument("--report", default=None, help="write the JSON plan report here")
    p_plan.add_argument("--max-attempts", type=int, default=3)
    p_plan.add_argument("--run-id", default=None, help="reuse a run id (resuming a run)")
    p_plan.add_argument("--restore-tm", action="append", default=None, metavar="DIR",
                        help="fold a recovered translation memory in before planning "
                             "(repeatable; earlier directories win)")
    p_plan.set_defaults(func=cmd_plan)

    p_run = sub.add_parser(
        "run",
        help="execute a queue against the provider (arguments pass through to queue_runner)",
    )
    p_run.add_argument("runner_args", nargs=argparse.REMAINDER,
                       help=f"queue path (default: {DEFAULT_QUEUE}) and queue_runner flags")
    p_run.set_defaults(func=cmd_run)

    p_render = sub.add_parser("render", help="splice the memory into locales/ and update the manifest")
    common(p_render)
    p_render.add_argument("--out-dir", default=DEFAULT_LOCALES_ROOT)
    p_render.add_argument("--queue", default=DEFAULT_QUEUE,
                          help="queue to take the run id from")
    p_render.add_argument("--run-id", default=None)
    p_render.add_argument("--report", default=None, help="write the JSON render report here")
    p_render.add_argument("-n", "--dry-run", action="store_true",
                          help="render and report, write nothing")
    p_render.add_argument("--fail-on-fallback", action="store_true",
                          help="exit non-zero if any unit rendered as English (CI gate)")
    p_render.add_argument("--no-verify", dest="verify", action="store_false",
                          help="skip the block-structure check (do not)")
    p_render.add_argument("--no-gc", dest="gc", action="store_false",
                          help="keep translation-memory entries no document references")
    p_render.set_defaults(func=cmd_render)

    p_status = sub.add_parser("status", help="report translation coverage per language")
    common(p_status)
    p_status.add_argument("--out-dir", default=DEFAULT_LOCALES_ROOT)
    p_status.add_argument("--fail-on-incomplete", action="store_true",
                          help="exit non-zero while any unit is untranslated (CI gate)")
    p_status.set_defaults(func=cmd_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
