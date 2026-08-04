"""Build a queue file from the corpus — **development scaffolding**.

This is *not* step 6 of `.claude/rules/l10n-pipeline-spec.md`. The real enqueue
step reads the manifest, recovers each document's previous revision through
`git show <blob>`, applies the no-API actions and keeps the manifest in step;
it belongs to its own sub-task. What this does is the narrow slice
`queue_runner` needs to be exercised and benchmarked against the real corpus
today: plan every document against the empty document (spec §1's degenerate
first-time case), keep the two actions that become jobs, and write a queue file
that validates against `app/schemas/queue.schema.json`.

    venv/bin/python3 cl10n/build_queue.py --langs he,ru -o l10n/queue/queue.json

Nothing here writes to the translation memory or the manifest.
"""

from __future__ import annotations

import argparse
import glob
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [_HERE, os.path.join(os.path.dirname(_HERE), "app")]

import tree_diff  # noqa: E402  (app/ — the planner this scaffolding drives)
from l10n_store import new_job, new_queue, save_queue  # noqa: E402

REPO = os.path.dirname(_HERE)
ENQUEUEABLE = {"TRANSLATE", "REVISE"}


def source_commit() -> str:
    """`HEAD` of this checkout — the schema wants a full 40-char sha."""
    try:
        out = subprocess.run(
            ["git", "-C", REPO, "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        )
        return out.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "0" * 40


def build(paths: list[str], langs: list[str], max_attempts: int = 3) -> list[dict]:
    """One job per `(lang, unit_hash)`, deduplicated across documents.

    Dedup is spec §2's rule and not an optimisation detail: the same hash means
    the same canonical source, so one translation serves every occurrence. The
    first occurrence's heading trail wins when contexts differ.
    """
    jobs: dict[str, dict] = {}
    for path in paths:
        with open(path, "r", encoding="utf-8") as fh:
            new_md = fh.read()
        for item in tree_diff.plan("", new_md):
            if item.action not in ENQUEUEABLE:
                continue
            for lang in langs:
                job_id = f"{lang}:{item.unit_hash}"
                if job_id in jobs:
                    continue
                jobs[job_id] = new_job(
                    unit_hash=item.unit_hash,
                    lang=lang,
                    action=item.action,
                    source=item.new_source,
                    context=item.context,
                    placeholders=item.placeholders,
                    old_source=item.old_source or None,
                    prior_translation=None,
                    max_attempts=max_attempts,
                )
    return list(jobs.values())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("sources", nargs="*", default=None,
                        help="markdown files (default: md/**/*.md)")
    parser.add_argument("--langs", default="he,ru", help="comma-separated target languages")
    parser.add_argument("-o", "--out", default="l10n/queue/queue.json")
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--limit", type=int, default=0,
                        help="keep only the first N jobs (for cheap experiments)")
    args = parser.parse_args(argv)

    paths = args.sources or sorted(
        glob.glob(os.path.join(REPO, "md", "**", "*.md"), recursive=True)
    )
    if not paths:
        print("no source markdown found", file=sys.stderr)
        return 2

    langs = [lang.strip() for lang in args.langs.split(",") if lang.strip()]
    jobs = build(paths, langs, args.max_attempts)
    if args.limit:
        jobs = jobs[: args.limit]

    save_queue(args.out, new_queue(source_commit(), jobs))
    print(f"{len(jobs)} job(s) from {len(paths)} document(s) "
          f"× {len(langs)} language(s) → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
