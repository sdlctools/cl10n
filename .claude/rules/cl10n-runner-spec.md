---
description: >-
  How the cl10n/ queue runner works — the durable job state machine, write
  orderings, resumption, retry classification, the account-wide rate-limit
  gate, the placeholder gate, and the provider seam. Read before changing
  concurrency, retries, persistence, prompts, or anything that decides whether
  a job costs an API call.
paths:
  - cl10n/**
  - l10n/**
---

# `cl10n/` — the continuous-localization runtime

`.claude/rules/l10n-pipeline-spec.md` specifies the whole pipeline and defines
no runtime. This document covers the part that exists: **step 7, execution**.
It is the "why" behind `cl10n/`, and the spec above is binding background — where
the two disagree, the spec wins and this file is the bug.

| File | What it is |
| --- | --- |
| `cl10n/queue_runner.py` | The runner. Reads a queue file, drives every job to a terminal state, writes the translation memory. |
| `cl10n/l10n_store.py` | Persistence primitives: atomic writes, the queue file, the per-language TM. No provider, no asyncio. |
| `cl10n/providers.toml` | The provider registry (§7): who is declared, their keys, defaults and connectors. |
| `cl10n/providers/` | The registry loader + one connector module per provider (§7). |
| `cl10n/build_queue.py` | **Dev scaffolding**, not a pipeline component — see "What this is not". |
| `cl10n/placeholders.py` | The placeholder-integrity rule (§6), shared with reassembly so the two enforcement points cannot drift. |
| `app/prompt.py` | The provider-agnostic prompt, `PROMPT_VERSION` and `LANG_NAMES` (§7). |
| `cl10n/tests/` | Provider stubbed throughout; none needs an API key or touches the network. |

Reassembly and rendering — the step that consumes this runner's output — is
`cl10n/reassemble.py`, specified in
[`cl10n-reassembly-spec.md`](cl10n-reassembly-spec.md).

Everything the runner reads and writes is defined by `app/schemas/*.schema.json`.
Those schemas are the contract; a state file that fails validation is a bug in
its writer.

```bash
venv/bin/python3 cl10n/queue_runner.py l10n/queue/queue.json --dry-run
venv/bin/python3 cl10n/queue_runner.py l10n/queue/queue.json -c 8
venv/bin/python3 cl10n/queue_runner.py QUEUE --provider nvidia          # §7
venv/bin/python3 cl10n/queue_runner.py QUEUE --model nvidia:some/model  # §7
venv/bin/python3 -m pytest                                       # full suite
venv/bin/python3 -m pytest cl10n/tests/test_queue_runner.py -k NAME   # one test
```

## What the runner is responsible for

Calling the translation API and recording the result. It does **not** decide
what to translate (`app/tree_diff.py` does) and does **not** turn translations
back into Markdown (reassembly does, and does not exist yet). If you find
yourself parsing Markdown in `cl10n/`, you are in the wrong component.

## 1. The three write orderings

These are the whole of crash-safety, and each one is chosen against a specific
way of losing work. None of them is stylistic.

```
       ┌──────────────────────────────────────────────────────────┐
       │ 1. claim: pending → in_flight, FLUSHED, then request out  │
       │ 2. response validated                                     │
       │ 3. TM entry written and fsynced                           │
       │ 4. queue: in_flight → done, FLUSHED                       │
       └──────────────────────────────────────────────────────────┘
                 kill at any point between any two steps
```

- **Claim before you call.** The queue says `in_flight` before the request is
  sent, so a process killed mid-request leaves the truth on disk. Reverse it
  and a killed run looks like it never started a job it may already have been
  billed for.
- **TM before `done`.** The translation is persisted first, the job is marked
  `done` second. Reverse it and a crash between the two marks a job complete
  whose translation was never written — the one failure mode no restart can
  detect or repair.
- **Restart rewrites `in_flight → pending`, and nothing else.** `done` and
  `rejected` are never touched.

Together with the TM shortcut (§3) this gives **at-least-once execution with
exactly-once effect**: re-running a job is safe because a TM upsert is
idempotent under `(lang, unit_hash)`, and the shortcut means the re-run usually
costs nothing.

## 2. Durability: atomic, synchronous, unlocked

Every state transition rewrites the whole file: serialize to a temp file **in
the same directory**, `fsync`, then `rename(2)` over the target. Same directory
because `rename` is only atomic within one filesystem. Whole file because
resumption is allowed to read nothing but this file.

**Every write in `l10n_store.py` is synchronous, and there is not a single
lock in the codebase.** This is deliberate and load-bearing: the runner is
single-threaded asyncio, so a function containing no `await` cannot be
interleaved with another coroutine. Serialize-then-rename is therefore already
atomic against every concurrent job. Making the store `async` would open
exactly the window that locks would then have to close — so if you are about
to add `async def` to `l10n_store.py`, you are adding a race and its mitigation
at the same time.

The cost is real and accepted: the event loop blocks for the length of one
`json.dumps` plus one `rename` per transition, which against a network call is
noise.

## 3. Job lifecycle

Legal transitions are `l10n-pipeline-spec.md` §4's, and nothing else is legal:

```
pending ──► in_flight ──► done
   │  ▲         │
   │  │         ├──► failed ──► in_flight     (retryable, attempts < max)
   │  │         │        │
   │  │(restart)│        └────► rejected      (exhausted, or terminal error)
   │  └─────────┘
   └────────────────────────► done            (TM shortcut, no API call)
```

**The TM shortcut** is checked before any request: if the TM already holds an
entry for `(lang, unit_hash)` **at the current `PROMPT_VERSION`**, the job goes
straight to `done`. This is what makes restarts cheap, deduplicates a unit hash
shared by two documents, and collapses the first-time/incremental distinction.
A shortcut job keeps `started_at: null` — it was never in flight.

An entry at an *older* prompt version does **not** shortcut, per spec §4. That
is intentionally stricter than §3's "normal runs do not re-translate on prompt
bumps": in a normal run a translated unit is a `REUSE` and never becomes a job
at all, so a stale-version entry reaching the runner is the unusual case, and
re-translating is the safe answer.

### Terminal vs. exhausted — a deliberate reading of the spec

The spec annotates `failed → rejected` with `attempts == max_attempts`, which
covers retry exhaustion. A provider error that can never succeed — bad
credentials, malformed request, unsupported language — takes the same edge
**on its first failure**, with `attempts` left at its true value.

The alternative, inflating `attempts` to `max_attempts` so the annotation reads
literally, was rejected: it writes a lie into the state file (three billed
calls where one was made), and `attempts` is the field an operator reads to
find out what a run cost.

## 4. Retry classification

The question is always "not now" or "not ever":

| Condition | `error.kind` | Retryable |
| --- | --- | --- |
| HTTP 429 | `rate_limit` | yes |
| HTTP 5xx, 408, 409 | `api_error` | yes |
| Connection loss, request timeout | `network` | yes |
| HTTP 401 / 403 / 400 / 404 / 422 | `api_error` | **no** |
| Placeholder gate failure (§6) | `placeholder_lost` | yes |
| Anything unrecognised | `api_error` | **no** |

Unrecognised exceptions are terminal because they are usually a bug in this
code, and retrying a bug three times only bills for it three times.

Backoff is exponential with **equal jitter** — half fixed, half random — capped
at 60s, and floored by any `Retry-After` the provider sends. Equal rather than
full jitter because full jitter can return a near-zero wait, which on a 429 is
how a client turns one rate limit into a stampede of them.

## 5. `RateLimitGate` — the part the spec does not prescribe

**A rate limit is a property of the account, not of the job that discovered
it.** Groq's is tokens-per-minute across the organisation, and every provider
worth adding meters something similar. With per-job backoff alone, every worker
retries privately into the same wall, so one throttle multiplies by the
concurrency level and jobs exhaust a retry budget against a condition that was
never theirs. The gate is therefore provider-independent — it lives in the
runner, not in a connector.

The first job to see a 429 therefore parks the **whole run** for the window the
provider asked for. `trip()` only ever extends the window, never shortens it: a
second 429 arriving mid-wait means the first estimate was low. The gate is
awaited *outside* the semaphore, so a throttled run does not hold slots idle.

Measured on 30 real corpus units at `-c 8`, free-tier account at 8000 TPM:

| | wall time | rate-limit failures | jobs lost | API calls |
| --- | --- | --- | --- | --- |
| serial (`-c 1`) | 232s | 0 | 0 | 30 |
| `-c 8`, no gate | 150s | 36 | **4** | 62 |
| `-c 8`, with gate | 156s | 16 | **0** | 46 |

Read that table before "optimising" the gate away. It buys correctness, not
speed — on a TPM-bound account the run is rate-limit bound either way, and the
gate is the difference between finishing and silently shipping four English
paragraphs as fallbacks.

**If a run rejects jobs with `error.kind = rate_limit`, the gate is not the
problem** — `max_attempts` in the queue file is too low for the account's tier.

## 6. The placeholder gate

Every string in a job's `placeholders` must occur in the translation **at least
as many times** as in the source, verbatim. "At least" rather than "exactly"
because a target language may legitimately repeat a term the source states
once; losing one is what this catches.

The rule itself lives in `cl10n/placeholders.py`, not here: `reassemble` runs
the identical check against every TM entry it is about to splice, because the
memory is a committed, hand-editable file and the renderer is the last thing
between a broken command and a published document. `placeholder_gate` below is
only this runner's retryable-`Failure` shape wrapped around that rule.

A failure is retryable: the next attempt appends a corrective instruction
naming the lost placeholders. Exhausting attempts leaves the job `rejected`,
nothing enters the TM, and the renderer falls back to the English source — a
readable fallback beats a translation with a broken command or a dead link.

Note that a placeholder absent from the source cannot be lost, so it never
fails the gate. That is correct, and it is also the shape of the most likely
bad test: assert against a placeholder the source actually contains.

## 7. The provider seam — pluggable, config-driven

`Translator` is one method returning **translated text**, not a raw completion:

```python
class Translator(Protocol):
    async def translate(self, prompt: str) -> str: ...
```

Everything provider-shaped lives behind it in a **connector**. That narrowness
is why every test stubs the provider in three lines and why no test needs a
key — a streaming connector aggregates to a final string before returning, so
this is always one complete translation, never an iterator.

### Adding a provider is config plus a module — never a runner change

The step-by-step, with the checklist and the traps, is
[`cl10n/PROVIDERS.md`](../../cl10n/PROVIDERS.md). What follows is the design.

```
cl10n/providers.toml     default = "groq"; one [providers.<name>] table each
cl10n/providers/
    base.py              Failure, Translator, extract_translation, _retry_after,
                         the generic classify tail. No provider library.
    __init__.py          Registry, load_registry (stdlib tomllib), resolve_route,
                         build_translator, get_classify, load_creds_file
    groq.py              GroqTranslator + classify (the groq taxonomy)
    nvidia.py            NvidiaTranslator + classify (the openai taxonomy)
    mistral.py           MistralTranslator + classify (the mistralai SDK)
```

A provider declares: `connector` (`module:Class`), `default_model`,
`api_key_env`, optional `api_key_creds_file`, optional `base_url`. The registry
**lazy-imports** the connector only when the route resolves to it, so a Groq
run never imports `openai`. `main()` resolves the route, loads that provider's
key, builds its translator and pairs it with **its own `classify`**, which the
runner holds as `self.classify`. A third provider is a table plus a module;
`queue_runner.py` does not change.

**Routing** (`--provider`, and an optional `provider:model` prefix on
`--model`), in priority order:

1. `--model nvidia:some/model` → that provider, that model (prefix wins);
2. `--provider nvidia` → that provider; a bare `--model` resolves against it,
   no `--model` uses its `default_model`;
3. neither → the default provider, its default model — **identical to
   pre-CLN-1 behavior**.

`queue_runner` re-exports `Failure`, `Translator`, `extract_translation`,
`GroqTranslator` and `classify` (the default provider's) for backward
compatibility; they are re-exports, not the implementation.

**Three things that will bite you if you undo them:**

- **Each connector's client is built lazily.** `AsyncGroq`/`AsyncOpenAI` at
  module scope raises (or silently misconfigures) when the key is unset, which
  makes the module unimportable on any machine without credentials — including
  CI. Constructing a translator must make no network call and need no key.
- **`cl10n/providers/groq.py` and the `groq` PyPI package share a top-level
  name**, and this bites in two directions. The connector must **not** put
  `cl10n/providers/` on `sys.path` (prepend it and `import groq` inside the
  connector finds *itself*, a circular import at first use); and the registry
  must **not** resolve a bare connector name with `import_module` (the library
  is normally already in `sys.modules`, so it returns the *library* and the
  lookup dies with `module 'groq' has no attribute 'GroqTranslator'`). Both are
  solved the same way: **connector modules and `base` are loaded from an
  explicit file path**, under a `_cl10n_providers_*` module key. A dotted
  connector name is still imported normally, for a connector living outside
  this directory. `test_providers.py` pins the regression.
- **The JSON envelope.** `TRANSLATION_PROMPT` rule 4 asks for a JSON object and
  the model obliges with `{"translation": "…"}`; storing that raw puts the
  wrapper in the TM. `base.extract_translation` unwraps it tolerantly (bare
  string, fenced block, any plausible key). Groq additionally sends
  `response_format={"type": "json_object"}` so the shape is guaranteed rather
  than lucky; NVIDIA does not, because its NIM model zoo is wider and not every
  model accepts JSON mode — there the prompt plus the tolerant extractor carry
  it.

Classification is per connector but the **kinds are not negotiable**: every
connector maps its library's exceptions onto §4's table and delegates its
generic tail (asyncio/OS timeouts, the unknown case) to `base.classify`, so
retries and the rate-limit gate behave identically across providers.

### The prompt is provider-agnostic

`app/prompt.py` owns `TRANSLATION_PROMPT`, `PROMPT_VERSION` and `LANG_NAMES`;
`app/groq_api.py` re-exports them for backward compatibility. Every connector
sends identical rules, so a Groq translation and an NVIDIA translation at the
same `PROMPT_VERSION` are interchangeable and **the TM shortcut fires across
providers** — switching provider does not re-bill the corpus.

### `PROMPT_VERSION` — when to bump it

`PROMPT_VERSION` versions `TRANSLATION_PROMPT`'s **rules** only. The runner
wraps that template with per-job payload — heading-trail context, the `REVISE`
old-source/prior-translation pair, the corrective retry instruction — and none
of that bumps the version. Neither does the connector a translation came
through.

Bump it when the CRITICAL RULES themselves change. Understand what that costs
first: every existing TM entry becomes older-than-current, which stops the TM
shortcut from firing for it and makes it eligible for a refresh run.

## 8. What this is not

**`cl10n/build_queue.py` is development scaffolding, not step 6.** It plans
every document against the empty document (spec §1's degenerate first-time
case) and writes a schema-valid queue, which is enough to exercise and
benchmark the runner against the real corpus. The pipeline's real enqueue step
reads the manifest, recovers each document's previous revision through
`git show <blob>`, applies the no-API actions and keeps the manifest in step.
That belongs to its own sub-task — do not grow this file into it.

**Reassembly and rendering are not here.** Splicing translated `inline` content
back into the new tree and rendering through `app/utils.py`'s `ast_to_markdown`
is `cl10n/reassemble.py`, a separate component with its own spec. The runner's
output — the TM plus a queue whose `rejected` jobs name the units needing
English fallback — is that component's input.

**The manifest is not written here.** Per-document bookkeeping, `RETIRE`
garbage collection and the `fallbacks` list belong to the orchestrator.

## 9. Conventions any change must preserve

1. JSON, UTF-8, `ensure_ascii=False`; TM `entries` serialized with sorted keys.
2. Hashes are `tree_diff.hash_tree`'s 16-char lowercase hex, never re-hashed or
   truncated differently.
3. Timestamps are ISO 8601 UTC with `Z`, second precision.
4. Job states and error kinds lowercase; actions uppercase as `tree_diff`
   emits them.
5. All state-file writes atomic, in the target directory, synchronous.
6. `queue.schema.json` sets `additionalProperties: false` and requires all
   fifteen job fields, nullable ones included — a job is constructed complete
   and only ever has values replaced. There is nowhere to stash extra
   bookkeeping, so anything per-run and transient (backoff schedules, the
   corrective-retry placeholder list) stays in memory.
7. `--dry-run` writes nothing at all — not even the restart rule, which would
   otherwise mutate the file it is reporting on. It also needs no key and
   resolves a route without building a client.
8. Adding a provider is a `providers.toml` table plus a connector module (§7).
   If a change makes the runner branch on provider identity, the seam has been
   broken — put the difference in the connector instead.
9. Every connector: lazy client, its own `classify` mapping onto §4's kinds,
   and a `translate` returning the finished string. No test may need a key or
   touch the network.
