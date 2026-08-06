---
description: >-
  cl10n/cli.py and the cl10n GitHub Actions workflow — the pipeline
  orchestrator: the real enqueue step (manifest + git blob recovery), the
  TM-decides-enqueue rule, RETIRE garbage collection, and how CI survives an
  interrupted run without losing or re-billing work. Read before changing the
  CLI surface, the manifest, the workflow's recovery/concurrency/secrets
  design, or anything that decides what becomes a job.
paths:
  - cl10n/cli.py
  - cl10n/manifest.py
  - cl10n/ci_report.py
  - .github/workflows/cl10n.yml
  - l10n/**
---

# `cl10n/cli.py` + `.github/workflows/cl10n.yml` — orchestrator and CI

The pipeline's entry point (steps 3–6 and 10–12 of
[`l10n-pipeline-spec.md`](l10n-pipeline-spec.md), which wins on any
disagreement) and the workflow that drives it on every push. A human and CI
run the same four subcommands — there is no CI-only code path.

## Use it

```bash
venv/bin/python3 cl10n/cli.py plan   --langs he,ru        # manifest + git → queue
venv/bin/python3 cl10n/cli.py run    l10n/queue/queue.json -c 8
venv/bin/python3 cl10n/cli.py run    QUEUE --provider nvidia          # pass-through
venv/bin/python3 cl10n/cli.py render --langs he,ru        # TM → locales/ + manifest
venv/bin/python3 cl10n/cli.py status --langs he,ru        # coverage per language
```

First-time translation **is** an incremental update whose step 3 recovers
nothing — same three commands, `plan` just finds more to do. There is no
bootstrap mode; adding one would be a bug (spec §1).

| File | What it is |
| --- | --- |
| `cl10n/cli.py` | the four subcommands; `run` delegates verbatim to `queue_runner` |
| `cl10n/manifest.py` | the ledger: blob recovery, document inventory, RETIRE GC |
| `cl10n/ci_report.py` | plan/queue/render reports → the PR body (no secrets, no network) |
| `.github/workflows/cl10n.yml` | push-to-default → plan → run → render → PR |

## What decides that a unit becomes a job

The diff supplies the *action* (`REVISE` with old source + prior translation,
else `TRANSLATE`); the **translation memory supplies the decision**:

    enqueue (lang, unit) ⟺ unit is in the new document
                            AND no usable TM entry at the current PROMPT_VERSION

Two properties follow, both load-bearing for CI:

- **Nothing is translated twice** — an entry in the TM never becomes a job,
  whether it landed a minute ago in a killed run or in another document
  sharing the same hash.
- **Nothing stays lost** — a `rejected` unit has no TM entry, so the next
  plan re-enqueues it even though the diff says `REUSE`. Without this an
  English fallback would be permanent. Same for hand-emptied translations.

`plan` always writes a queue (possibly with zero jobs) and reports
`render_required` separately, because `COPY` changes the output with no job
(spec §2) — a pipeline that renders only when the queue is non-empty ships
stale code fences.

## How CI survives an incomplete run (the decided design)

**The resume state is the translation memory, not the queue.** The queue is
derived (from TM + manifest + corpus) and gitignored; TM entries are the paid
work. A run killed at any point resumes because the next `plan` simply finds
fewer misses. Three recovery layers, folded in with `--restore-tm`
(repeatable; committed entries win, then earlier directories):

1. `l10n/tm/` committed on the default branch — survives everything;
2. the open `cl10n/translations` PR branch — finished-but-unmerged runs;
3. the `cl10n-tm` build artifact, uploaded `if: always()` — what a dying run
   flushed before it stopped. Reading it back needs `actions: read` in the
   workflow's `permissions` block: declaring that block zeroes every scope it
   omits, and without the scope the step 403s and reports "no artifact" —
   identical to the ordinary first-run case, so the layer looks alive while
   contributing nothing. The upload keeps working either way.

The Execute step is time-boxed and `continue-on-error`: when it stops early,
whatever completed still renders and ships in the PR (untranslated units as
counted English fallbacks), and the body says how many jobs the next run will
resume. Invariant: **no work lost, no segment billed twice** — demonstrated in
`cl10n/tests/test_cli.py` by killing a run mid-flight with `os._exit`-grade
interruption and counting the second run's provider calls.

## The workflow's other four decisions

- **Trigger**: `push` to the default branch, `paths: md/**` — no run when no
  watched Markdown changed. `workflow_dispatch` resumes without a dummy push.
- **Output**: always a PR from the fixed branch `cl10n/translations`
  (force-updated, so successive runs update one PR), never a direct push.
  Body comes from `ci_report.py`: segment counts by action, execution state,
  fallbacks.
- **Concurrency**: one `cl10n` group, `cancel-in-progress: false` — rapid
  pushes serialize instead of racing for the TM files.
- **Secrets**: every declared provider key (`GROQ_API_KEY`, `NVIDIA_NIM_API_KEY`)
  is env of **exactly one step** — Execute — and of no other; no
  `pull_request`/`pull_request_target` trigger exists, so fork code never
  executes where the secrets are. The runner reads only the active connector's
  `api_key_env` (`cl10n/providers.toml`), so a key bound to the step but not
  selected this run sits unread. `PROVIDER`/`MODEL` (workflow env, overridable
  by dispatch inputs) choose the connector; empty means the default, Groq.

## RETIRE garbage collection

`render` (full-corpus, clean, `--gc` default) deletes a TM entry only when no
manifest file's `unit_hashes` still lists it. A single-file render never
collects — its ledger view is partial and collecting against it would delete
the other documents' translations.

## Don't break

1. `run` stays a verbatim delegation to `queue_runner.main` — no second
   argument surface. `main` routes it *before* argparse: `nargs=REMAINDER`
   only starts collecting at the first non-option token, so a subparser would
   reject `run --dry-run` while the queue path is documented as optional.
   The provider flags (`--provider`, `--model`, `--providers`) are the
   runner's and pass straight through — `cli.py` must not learn about
   providers, and does not import a connector.
2. Segmentation comes from `tree_diff`'s private helpers (as in
   `reassemble.py`) — no second definition of "translation unit".
3. `plan` may write only the TM (RECHECK flags, restores); queue via
   `save_queue`; manifest and locales belong to `render`.
4. The manifest is written only after files actually rendered — never for a
   refused (`StructureMismatch`) file, never on `--dry-run`. **One refused
   language withholds the whole document's revision**, because `source_blob`
   and `unit_hashes` are per-document: advancing them for the languages that
   did render strands the refused one (its next diff sees pure REUSE, needs no
   job, and is already listed under `localized`, so `render_required` is False
   for ever while `status` calls it up to date).
5. Empty TMs are not written; `manifest.py`'s git helpers never raise —
   an unreadable blob means "plan against the empty document", not a crash.
