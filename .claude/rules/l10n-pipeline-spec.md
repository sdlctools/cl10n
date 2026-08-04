---
description: >-
  Architecture specification and JSON data contracts for the Markdown
  Continuous Localization pipeline — canonicalise, segment, hash, TM lookup,
  queue, execute, validate, reassemble, render, commit. The interface the
  runtime sub-tasks (JST-263/264/265) implement against. Read before writing
  or changing any pipeline component, state file, or schema.
paths:
  - app/**
  - l10n/**
  - locales/**
---

# Markdown Continuous Localization — pipeline specification

The contract for a pipeline that keeps translated mirrors of a Markdown corpus
continuously up to date. Change detection is **already solved** by
[`app/tree_diff.py`](../../app/tree_diff.py) (design rationale:
[`tree-diff-spec.md`](tree-diff-spec.md), which is binding background); this
document consumes that engine and specifies everything around it. It defines
**no runtime component** — the JSON Schemas in
[`app/schemas/`](../../app/schemas/) plus this prose are the interface the
implementation sub-tasks build against.

**Corpus under test**: `md/skills/jira-task-assigner/SKILL.md`,
`md/skills/_shared/jira-api-reference.md`,
`md/skills/_shared/project-config.md`.
**Target languages**: Hebrew (`he`), Russian (`ru`).
**Output**: mirrors the source tree under `locales/<lang>/`, e.g.
`locales/he/skills/jira-task-assigner/SKILL.md`.

## 1. Pipeline architecture

```
                      md/**.md  (committed source corpus)
                          │
              1. canonicalise (mdformat round-trip — utils.markdown_to_ast
                          │      + ast_to_markdown; identical to tree_diff's)
                          ▼
              2. segment + Merkle-hash        tree_diff.hash_tree / tm_keys
                          │
              3. recover previous revision    manifest.files[path].source_blob
                          │                   → `git show <blob>`; missing → ""
                          ▼
              4. plan                         tree_diff.plan(old, new)
                          │                   REUSE RECHECK REVISE TRANSLATE
                          │                   RETIRE COPY   (see §2)
                          ▼
              5. apply no-API actions         TM review-status updates,
                          │                   manifest bookkeeping, RETIRE GC
                          ▼
              6. enqueue                      one job per (lang, unit_hash)
                          │                   needing the LLM → l10n/queue/
                          ▼
              7. execute                      async Groq runner (app/groq_api.py
                          │                   prompt), per-job state machine §4
                          ▼
              8. validate                     placeholder gate §5
                          │
                          ▼
              9. update TM                    l10n/tm/<lang>.json  (write TM
                          │                   BEFORE marking the job done)
                          ▼
             10. reassemble                   splice translations into the NEW
                          │                   tree's inline nodes by unit hash;
                          │                   COPY opaque blocks verbatim
                          ▼
             11. render                       utils.ast_to_markdown
                          │                   → locales/<lang>/<mirrored path>
                          ▼
             12. commit                       locales/ + l10n/tm/ + manifest in
                                              one commit; queue never committed
```

### First-time translation is the degenerate case

There is **one code path**. First-time translation of a document is exactly an
incremental update in which step 3 recovers nothing: `plan("", new)` emits
`TRANSLATE` for every unit and `COPY` for every opaque block, and an empty TM
turns none of them into no-ops. The pipeline must not have a separate
"initial import" branch — a document whose manifest entry is missing, whose
`source_blob` is unreachable (shallow clone), or which was never localized is
simply planned against the empty document. This degrades safely: even with no
old revision, the executor's TM check (§4, `pending → done` shortcut) turns
every already-translated unit back into the equivalent of a `REUSE`, which is
the `tm_keys(new) − tm.keys()` set-difference fast path realized at execution
time. The only thing an unreachable old revision loses is the upgrade of
misses into `REVISE` jobs.

### Failure modes the queue design answers

The queue exists because step 7 talks to a metered, unreliable network
service. The design is dictated by four concrete failures:

1. **Provider rate limits** (HTTP 429) — a job failure must not lose the rest
   of the queue; the job records the error, backs off, retries. Attempts are
   counted per job, not per run.
2. **Transient connectivity loss** — same handling as 429; `failed` is a
   *non-terminal* state (§4).
3. **Partial completion** — a run that translates 80 of 119 units must
   persist exactly which 80, so the next run does 39, not 119. Hence per-job
   state, flushed to disk on every transition.
4. **A run interrupted mid-flight** (SIGKILL, OOM, laptop lid) — the queue
   file alone must reconstruct reality after restart: `done` stays done,
   `in_flight` is re-run (at-least-once; safe because a TM write is an
   idempotent upsert keyed by `(lang, unit_hash)`), `pending` runs. See the
   restart rule in §4.

### Where first-time and incremental share code

Everything from step 4 (plan) onward is shared verbatim. Steps 1–2 are shared
with `tree_diff` itself (same canonicalisation, same hashes — this is what
makes TM keys stable). Step 3 is the *only* place the two cases differ, and
the difference is the value of one string (`old_md` is `""`).

## 2. What each tree_diff action means to the pipeline

`tree_diff.plan` emits six actions. Every one of them is mapped; none is
ignored:

| Action | API call | TM effect | Queue effect | Rendered output effect |
| --- | --- | --- | --- | --- |
| `REUSE` | no | none | no job | renderer reads existing TM entry |
| `RECHECK` | no | `review_status` → `"recheck"` (translation kept) | no job | renderer reads existing TM entry |
| `REVISE` | **yes** | entry **replaced** on success; provenance updated | job with `old_source` + `prior_translation` | renderer reads new TM entry |
| `TRANSLATE` | **yes** | entry **created** on success | plain job | renderer reads new TM entry; **falls back to English source** if the job ended `rejected` (§5) |
| `RETIRE` | no | entry deleted **only when no manifest file still references the hash** (see GC below) | no job | none — the unit no longer exists in the new tree |
| `COPY` | no | none — opaque blocks never enter the TM | no job | **reassembly emits the changed opaque block (fence / raw HTML / front matter) verbatim into the target** — no API call, but the rendered file still changes |

`COPY` deserves the emphasis: it is the one action that changes the rendered
output without touching either the TM or the queue. A localized document whose
source's code fence changed must receive the new fence on the next render even
though nothing was translated. An implementation that renders only when the
queue is non-empty is therefore **wrong**: render whenever `plan` emitted
anything but pure `REUSE`.

**RETIRE garbage collection.** Unit hashes are content-addressed, so the same
hash can be referenced by several documents (identical paragraph in two
files). `RETIRE` therefore only removes the hash from the *retiring
document's* `unit_hashes` list in the manifest; the TM entry itself is deleted
at the end of the run iff no file's `unit_hashes` still contains it.

**Enqueue dedup.** For the same reason, jobs are deduplicated by
`(lang, unit_hash)` across documents: same hash ⇒ same canonical source text
⇒ one translation serves all occurrences. When contexts differ across
occurrences, the first occurrence's heading trail is used.

## 3. Translation memory — `l10n/tm/<lang>.json`

Schema: [`app/schemas/translation-memory.schema.json`](../../app/schemas/translation-memory.schema.json).
Worked example (real corpus data): [`app/schemas/examples/tm.he.json`](../../app/schemas/examples/tm.he.json).

One file **per language**, keyed by translation-unit hash exactly as produced
by `tree_diff.hash_tree` (16 lowercase hex chars). Each entry stores:

- `source` — the canonical source text the translation was made from. This is
  what answers "is this entry stale": if the hash is still referenced by a
  manifest file, the entry is current *by construction* (the hash **is** a
  hash of the source); if no file references it, it is garbage.
- `translation` — the translated text, placeholders intact.
- `model` + `prompt_version` — which model and which revision of the
  translation prompt (`app/groq_api.py` `TRANSLATION_PROMPT`; version bumps
  whenever that prompt's rules change) produced it. An entry whose
  `prompt_version` is older than the current one is *eligible* for
  re-translation in a dedicated refresh run; normal incremental runs do not
  re-translate on prompt bumps.
- `translated_at` — ISO 8601 UTC timestamp.
- `review_status` — `"machine"` (fresh from the model, unreviewed),
  `"recheck"` (content moved; context may have changed — flagged by the
  RECHECK action), `"approved"` (human-reviewed). Together these answer *why
  a given translation exists and whether to trust it*.
- `action` — `"TRANSLATE"` or `"REVISE"`: whether it was produced fresh or as
  a revision of a prior translation (provenance).

### Why one file per language, not one file total

CI runs languages in parallel (one job per language is the natural unit of
parallelism, and the Groq rate limit is per-account, so serializing languages
inside one process buys nothing). Two parallel jobs writing one shared
`tm.json` would conflict on *every* merge, because both add entries to the
same JSON object — adjacent-line insertions are exactly the case git cannot
auto-merge. Per-language files make concurrent language runs write **disjoint
files**: merge conflicts become impossible between languages, and within one
language a conflict can only arise from two concurrent runs of the *same*
language, which the run design (one queue, one runner per language) already
excludes. Secondary benefits: a Hebrew reviewer's diff touches only
`tm/he.json`, and a bad language can be reverted without touching the others.
Entries are serialized with **sorted keys** so diffs are deterministic and
insertions stay local.

## 4. Queue and jobs — `l10n/queue/queue.json`

Schema: [`app/schemas/queue.schema.json`](../../app/schemas/queue.schema.json).
Worked example (real corpus data): [`app/schemas/examples/queue.json`](../../app/schemas/examples/queue.json).

A queue file is an ordered list of jobs plus run metadata (`run_id`,
`created_at`, `source_commit` — the commit of `md/` the plan was computed
from). Only actions that need the LLM become jobs: `TRANSLATE` and `REVISE`.
Each job carries everything the runner needs **without consulting any other
file**: `unit_hash`, `lang`, `action`, `source`, heading-trail `context`,
`placeholders`, and — for `REVISE` — `old_source` and `prior_translation`.
Job `id` is `"<lang>:<unit_hash>"`, unique within a queue by the dedup rule
in §2.

### Job states

| State | Terminal? | Meaning |
| --- | --- | --- |
| `pending` | no | never started (or reset from `in_flight` by a restart) |
| `in_flight` | no | claimed by the runner; the API request may or may not have been sent |
| `failed` | no | last attempt errored (`error` records kind + detail); retryable while `attempts < max_attempts` |
| `done` | **yes** | translation validated (§5) and written to the TM |
| `rejected` | **yes** | attempts exhausted — no translation entered the TM; renderer falls back to source for this unit |

### Legal transitions — nothing else is legal

```
pending ──► in_flight ──► done
   ▲            │
   │            ├──► failed ──► in_flight        (retry: attempts < max_attempts)
   │            │       │
   │  (restart) │       └─────► rejected         (attempts == max_attempts)
   └────────────┘
```

- `pending → in_flight` — the runner claims the job and **writes the queue
  file before sending the request**, so a crash mid-request leaves the truth
  on disk.
- `pending → done` (shortcut, no API call) — before sending anything, the
  runner checks the TM: if an entry for `(lang, unit_hash)` already exists
  with the current `prompt_version`, the job is marked `done` immediately.
  This is what makes restarts and cross-document duplicates cheap, and what
  collapses the first-time/incremental distinction (§1).
- `in_flight → done` — response passed the placeholder gate; the TM entry is
  written **first**, then the queue state. (Crash between the two writes ⇒
  restart re-runs the job ⇒ idempotent TM upsert. The reverse order would
  mark a job done whose translation was lost.)
- `in_flight → failed` — API error, rate limit, connectivity loss, or
  placeholder validation failure; `attempts` is incremented and `error`
  recorded.
- `failed → in_flight` — retry with exponential backoff while
  `attempts < max_attempts`. A `placeholder_lost` retry appends a corrective
  instruction naming the lost placeholder(s) to the prompt.
- `failed → rejected` — attempts exhausted. Terminal.
- **Restart rule**: on startup the runner rewrites every `in_flight` job to
  `pending` before doing anything else. `done` and `rejected` are never
  touched. This plus the TM shortcut gives at-least-once execution with
  exactly-once effect.

### Durability

Every state transition is flushed with an **atomic write**: serialize the
whole queue to a temp file in the same directory, then `rename(2)` over
`queue.json`. A reader (including a restarted runner) therefore always sees a
consistent snapshot, and resumption needs *only this file*: `done` jobs are
done, `in_flight` were in flight at the moment of death, `pending`/`failed`
never completed. Two independently written runners following this section
produce interchangeable queue files — that is the compatibility bar (§7).

## 5. The placeholder-validation gate

`tree_diff` attaches to each unit the inline spans that must survive
translation byte-for-byte (`code_inline`, link `href`s, image `src`s — the
XLIFF `<ph>` idea). The gate, applied to every API response while the job is
`in_flight`:

> Every string in the job's `placeholders` list must occur in the translated
> text **at least as many times** as it occurs in the source text, verbatim.

A translation that loses a placeholder **never enters the TM**. The job goes
`in_flight → failed` with `error.kind = "placeholder_lost"` and the offending
placeholder(s) in `error.detail`; retries re-prompt with a corrective
instruction; if `max_attempts` is exhausted the job ends `rejected`, the
renderer emits the **English source text** for that unit (a readable fallback
beats a translation with a broken command or dead link), and the manifest
records the unit hash under `fallbacks` for that language so CI can report
"localized with N fallbacks" instead of silently shipping them.

## 6. Manifest and file layout — `l10n/manifest.json`

Schema: [`app/schemas/manifest.schema.json`](../../app/schemas/manifest.schema.json).
Worked example (real corpus data): [`app/schemas/examples/manifest.json`](../../app/schemas/examples/manifest.json).

The manifest is the pipeline's per-document ledger. Per source file it
records: `source_blob` (git blob SHA of the last-localized revision — step 3
recovers the old markdown via `git show <blob>`, so the corpus is never
duplicated on disk), `doc_hash` (Merkle root of the canonicalized document —
a one-string "did anything change at all" check), the ordered `unit_hashes`
and `opaque_hashes` lists (reference counts for RETIRE GC, coverage
accounting), and per-language completion state (`run_id`, `completed_at`,
`fallbacks`).

### Layout and git policy

| Path | Contents | Committed? |
| --- | --- | --- |
| `md/**` | source corpus | **yes** (this is what triggers the pipeline) |
| `locales/<lang>/<mirrored path>` | rendered translations | **yes** — the product |
| `l10n/tm/<lang>.json` | translation memory, one per language | **yes** — losing it means retranslating the world; reviewing it is reviewing the translations |
| `l10n/manifest.json` | per-document ledger | **yes** — step 3 depends on it |
| `l10n/queue/` | active queue files | **no** — run artifact, gitignored. A queue is meaningful only to the run (or resumed run) that owns it; committing one would ship transient state and cause exactly the merge conflicts §3 avoids |

Step 12 commits `locales/`, `l10n/tm/` and `l10n/manifest.json` together in
one commit per run, so the repo never holds a manifest that claims a
completion the committed locales don't show.

## 7. Conformance rules (the compatibility bar)

Two people implementing the runner independently must produce state files the
other can resume. Normative requirements:

1. JSON, UTF-8, `ensure_ascii=False`; object keys sorted where the schema
   says so (TM `entries`, manifest `files`).
2. Hashes: exactly the 16-char lowercase-hex strings `tree_diff.hash_tree`
   produces. Never re-hash or truncate differently.
3. Timestamps: ISO 8601 UTC with `Z` suffix, second precision
   (`2026-08-04T12:00:00Z`).
4. Enums: job states and error kinds lowercase as in §4/§5; actions uppercase
   exactly as `tree_diff` emits them.
5. All state-file writes are atomic temp-file-plus-rename in the target
   directory.
6. Write ordering: TM entry before queue `done`; queue `in_flight` before the
   API request.
7. Restart rule: `in_flight → pending`, then the TM shortcut — no other
   startup mutation.
8. The three schemas in `app/schemas/` are the contract; a state file that
   fails schema validation is a bug in its writer, not in its reader.

## What this spec deliberately does not cover

- **Reassembly internals** (splicing translated inline content into the tree
  and rendering via `ast_to_markdown`) — its *contract* is fixed here (§1
  steps 10–11, COPY semantics in §2, fallback in §5), but its implementation
  belongs to the runtime sub-tasks.
- `demos/localize.py` is a discarded prototype of the queue/lockfile/render
  trio — intent only; its parser and renderer are both wrong by this spec
  (naive block parsing, fabricated structure instead of AST round-trip).
