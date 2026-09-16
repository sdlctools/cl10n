# `cl10n` user guide

A practical guide to the `cl10n` command, which drives the whole
continuous-localization pipeline. This is the *how-to*: worked examples, real
output, and the flows you will actually run. For the *why* behind the design
decisions, read [`.claude/rules/cl10n-cli-spec.md`](../.claude/rules/cl10n-cli-spec.md);
for the data contracts, [`.claude/rules/l10n-pipeline-spec.md`](../.claude/rules/l10n-pipeline-spec.md).
To add a provider the pipeline does not yet speak to, see
[`PROVIDERS.md`](PROVIDERS.md).

Every command and every block of output below was run against this repository.

**Contents**

1. [The mental model](#1-the-mental-model)
2. [Prerequisites](#2-prerequisites)
3. [The four subcommands](#3-the-four-subcommands)
4. [Five-minute tour](#4-five-minute-tour)
5. [Command reference](#5-command-reference)
6. [Reading the numbers](#6-reading-the-numbers)
7. [The files the pipeline owns](#7-the-files-the-pipeline-owns)
8. [Flow: adding a language](#8-flow-adding-a-language)
9. [Flow: day-to-day continuous localization](#9-flow-day-to-day-continuous-localization)
10. [Flow: integrating a new project](#10-flow-integrating-a-new-project)
11. [Cookbook](#11-cookbook)
12. [Troubleshooting](#12-troubleshooting)
13. [Appendix: exit codes, env, JSON shapes](#13-appendix)

______________________________________________________________________

## 1. The mental model

Four commands, run in order, over and over:

```
    plan  ──►  run  ──►  render  ──►  (commit)        status  (read-only, any time)
     │          │          │
  what needs  pay for    turn the memory
  translating  it        into locale files
```

Three ideas explain almost everything the CLI does.

**The translation memory is the product.** `l10n/tm/<lang>.json` maps a hash of
a chunk of English to its translation. Everything else — the queue, the
manifest, the rendered files under `locales/` — is derived from it and can be
rebuilt. When you review a localization run, the memory is what you are
reviewing.

**Units are content-addressed.** A "translation unit" is roughly a paragraph, a
heading, or a table cell. Its key is a hash of its canonical text, so the same
sentence in two documents is *one* entry, and rewrapping a paragraph across
different line lengths changes nothing. This is why the pipeline never pays
twice for the same text.

**There is no "initialize" step.** A first-ever translation is an incremental
update that happens to find everything missing. The same three commands do
both. If you ever find yourself looking for a `--bootstrap` or `--init` flag,
the answer is that there deliberately isn't one.

______________________________________________________________________

## 2. Prerequisites

Install the package. It is on PyPI as `markdown-localization` (the import
name and the command stay `cl10n`), and the provider SDKs are extras —
take the one you route to:

```bash
python3 -m venv venv
venv/bin/pip install "markdown-localization[groq]"     # or [nvidia], [mistral], [all-providers]
```

That puts a **`cl10n` console script** in `venv/bin/`, which is what every
command below uses. The other runnable modules are reached with `python -m`
(`python3 -m cl10n.compat_check`, `python3 -m cl10n.core.tree_diff`). There
are no script paths: `venv/bin/python3 cl10n/cli.py …` no longer works, by
design.

Working *on* this repository rather than with it is the same install pointed
at the checkout, with the tests and every provider:

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt   # = -e .[dev]
```

The interpreter is `venv/bin/python3` throughout — the virtualenv is `venv/`,
not `.venv/`.

A provider key is needed **only by `run`**. `plan`, `render` and `status` never
touch the network, so you can explore the whole pipeline without one.

Which key depends on which provider you run. Providers are declared in
[`cl10n/providers.toml`](providers.toml), and each one names the environment
variable its key comes from plus an optional creds file:

| Provider | Key variable | Default creds file | Selected by |
| --- | --- | --- | --- |
| `groq` (default) | `GROQ_API_KEY` | `groq_creds.txt` | nothing — it is the default |
| `nvidia` | `NVIDIA_NIM_API_KEY` | `nvidia-nim-creds.txt` | `--provider nvidia` |
| `mistral` | `MISTRAL_API_KEY` | `mistral-creds.txt` | `--provider mistral` |

### Mistral models

All three `-latest` aliases are verified working. Note the model id carries **no
`mistral/` prefix** — that is the provider's own naming; our routing prefix is a
colon, and the connector strips it before the call:

```bash
venv/bin/cl10n run --model mistral:mistral-large-latest
venv/bin/cl10n run --model mistral:mistral-medium-latest
venv/bin/cl10n run --provider mistral    # = mistral-large-latest
```

| Model | Simple prose | A unit with 5 mixed placeholders |
| --- | --- | --- |
| `mistral-large-latest` (default) | clean | clean, but needed one corrective retry |
| `mistral-medium-latest` | clean | clean, one corrective retry — the best value here |
| `mistral-small-latest` | clean | **unreliable** — 9 placeholder failures in 10 calls, one unit still rejected after 5 attempts |

Measured on a deliberately hostile unit mixing inline code, a relative markdown
link, `<ANGLE_KEYS>` and `$ARGUMENTS`. The lesson is not the ranking but the
shape: **placeholder-dense technical prose is what separates these models**, and
plain paragraphs do not. A rejected unit is not lost work — it renders as
English and is re-enqueued next run — but a model that rejects often turns
`--fail-on-fallback` into a permanent red build.

Prefer `medium` or `large` for documentation like this repo's. If you see
`placeholder_lost` dominating `run`'s failure summary, that is the model, not
the pipeline — raise `plan --max-attempts` or move up a size.

```bash
export GROQ_API_KEY=gsk_...
# or put it in a file, gitignored, which `run` reads automatically:
echo 'GROQ_API_KEY="gsk_..."' > groq_creds.txt

# For another provider instead:
export NVIDIA_NIM_API_KEY=nvapi-...
export MISTRAL_API_KEY=...
```

`run` reads the creds file belonging to whichever provider it resolved, so you
never have to say which file goes with which key. Point elsewhere with
`run --creds-file path/to/file`. An environment variable that is already set
always wins over the file. You only need a key for the provider you actually
run — an unset key for a provider you are not using is never consulted.

Adding a further provider is a config entry plus a connector module; see
[`PROVIDERS.md`](PROVIDERS.md).

Run the commands from the repository root — `plan` and `render` locate the repo
with `git rev-parse --show-toplevel` and record paths relative to it.

______________________________________________________________________

## 3. The four subcommands

| Command | Reads | Writes | Network | Typical duration |
| --- | --- | --- | --- | --- |
| `plan` | `md/**`, manifest, git blobs, TM | queue, TM | no | seconds |
| `run` | queue, TM | queue, TM | **yes** | minutes to hours |
| `render` | `md/**`, TM | `locales/**`, manifest, TM | no | seconds |
| `status` | `md/**`, manifest, TM, `locales/**` | nothing | no | seconds |

Only `run` costs money. Only `render` produces the files you ship. `status` is
safe to run at any moment, including while `run` is in flight.

______________________________________________________________________

## 4. Five-minute tour

A complete first localization of a throwaway project. Copy-paste the whole
block.

```bash
mkdir /tmp/tour && cd /tmp/tour
git init -q
mkdir -p md
cat > md/guide.md <<'EOF'
# Deployment guide

Run `make deploy` before you start, and read the [handbook](https://example.com/hb).

The rollback procedure is documented separately.
EOF
git add -A && git commit -qm "corpus"
```

Commit the corpus. `plan` is happy either way — it compares against the last
*localized* revision in the manifest, not against your working tree — but
`render` records `git hash-object` of the file as it sits on disk, and if that
content was never committed the blob is not in the object database for the next
run to read. That degrades safely and costs nothing (the memory still covers
every unit); what it loses is the `REVISE` upgrade on the following edit. See
[`INTEGRATION.md` §9](INTEGRATION.md#9-why-committing-matters-precisely).

Now plan. `cl10n` is installed, so it is just on your path — there is no
checkout to point at:

```bash
pip install "markdown-localization[groq]"

cl10n plan --langs he
```

```
PLAN 20260804T142152Z-b38471 — 1 document(s), 1 language(s)
  TRANSLATE=3  REVISE=0  RECHECK=0  REUSE=0  COPY=0  RETIRE=0
  3 job(s) → l10n/queue/queue.json  {'he': 3}
  0 unit(s) already in the translation memory, 0 flagged for recheck
  render required: yes
```

Three units: the heading and the two paragraphs. See what it would cost before
paying:

```bash
cl10n run --dry-run
```

Then pay for it, and render:

```bash
cl10n run --creds-file groq_creds.txt
cl10n render --langs he
cat locales/he/guide.md
```

```markdown
# מדריך פריסה

הפעל `make deploy` לפני שאתה מתחיל, וקרא את [מדריך](https://example.com/hb).

הליך השחזור מתועד בנפרד.
```

Note what survived: `` `make deploy` `` and the link URL are byte-for-byte
intact. That is the placeholder gate — a translation that drops one is rejected
rather than shipped.

Now the part that matters. Change **one sentence**:

```bash
sed -i 's/documented separately/documented in the appendix/' md/guide.md
git commit -qam "edit one sentence"
cl10n plan --langs he
```

```
PLAN 20260804T142206Z-a1e742 — 1 document(s), 1 language(s)
  TRANSLATE=0  REVISE=1  RECHECK=0  REUSE=2  COPY=0  RETIRE=0
  1 job(s) → l10n/queue/queue.json  {'he': 1}
  2 unit(s) already in the translation memory, 0 flagged for recheck
  render required: yes
```

One job, not three. And it is a `REVISE` — the job carries the old English, the
old Hebrew, and the new English, so the model edits rather than retranslates.
`run` makes exactly one API call, `render` rewrites the file, and only that one
sentence changes.

Run `plan` a third time with nothing edited and you get `0 job(s)` and
`render required: no`. That is the steady state.

______________________________________________________________________

## 5. Command reference

All four subcommands share these:

| Flag | Default | Meaning |
| --- | --- | --- |
| `sources...` | `<md-root>/**/*.md` | explicit files to act on, positional |
| `--langs` | `he,ru` | comma-separated target languages |
| `--md-root` | `md` | where the source corpus lives |
| `--tm-dir` | `l10n/tm` | translation memory directory |
| `--manifest` | `l10n/manifest.json` | the per-document ledger |
| `--json` | off | machine-readable output on stdout |

### 5.1 `plan`

Works out what needs translating and writes a queue file.

```bash
venv/bin/cl10n plan --langs he,ru
venv/bin/cl10n plan --langs he md/skills/guide.md   # one document
venv/bin/cl10n plan --langs he,ru --json            # for scripts
```

| Flag | Default | Meaning |
| --- | --- | --- |
| `-o`, `--out` | `l10n/queue/queue.json` | queue file to write |
| `--report` | none | also write the full JSON plan report here |
| `--max-attempts` | `3` | retry budget stamped into each job |
| `--run-id` | generated | reuse a run id instead of minting one |
| `--restore-tm DIR` | none | fold a recovered memory in first (repeatable) |

Against the real corpus with an empty memory:

```
PLAN 20260804T144118Z-da60d6 — 3 document(s), 2 language(s)
  TRANSLATE=416  REVISE=0  RECHECK=0  REUSE=0  COPY=40  RETIRE=0
  794 job(s) → l10n/queue/queue.json  {'he': 397, 'ru': 397}
  0 unit(s) already in the translation memory, 0 flagged for recheck
  render required: yes
```

Line by line:

- **`PLAN <run-id> — N document(s), M language(s)`.** The run id is a UTC
  timestamp plus random suffix. It is stamped into the queue and, after
  `render`, into the manifest, so you can trace a locale file back to the run
  that paid for it.
- **The action tally.** Counts of `tree_diff`'s six verdicts over *unit
  occurrences*, summed across documents. `TRANSLATE` is new text, `REVISE` is
  changed text with a recoverable predecessor, `REUSE` is unchanged, `RECHECK`
  is unchanged text that moved to a new position, `RETIRE` is text that is gone,
  and `COPY` is a changed opaque block (a code fence, raw HTML, front matter) —
  never translated, but it still changes the rendered file.
- **`N job(s) → <path>`.** Jobs are deduplicated by `(lang, unit_hash)`, so this
  is normally *smaller* than the action tally. See [§6](#6-reading-the-numbers).
- **`N unit(s) already in the translation memory`.** What the memory saved you.
- **`render required: yes|no`.** Whether the rendered output would change. This
  is reported separately from the job count on purpose: a changed code fence
  produces zero jobs and still changes the file, so "the queue is empty" is not
  the same as "nothing to do".

The queue is written **even when it contains zero jobs**, so `run` always has a
file to be a no-op over.

**What actually becomes a job.** The diff supplies the *action*; the
translation memory supplies the *decision*:

> enqueue `(lang, unit)` ⟺ the unit is in the new document **and** the memory
> has no usable entry for it at the current prompt version.

"Usable" means present, non-empty, and stamped with the current
`PROMPT_VERSION`. Two consequences worth internalising:

- Nothing is translated twice, whether the entry landed a minute ago in a killed
  run or last year in a different document sharing the same sentence.
- Nothing stays lost. A unit whose job was rejected has no entry, so the next
  `plan` re-enqueues it even though the diff calls it `REUSE`. Without this an
  English fallback would be permanent.

### 5.2 `run`

Executes the queue against the provider. This is the only command that spends
money and the only one that takes minutes rather than seconds.

`run` is a **verbatim delegation** to `cl10n.queue_runner` — every argument
after `run` is the runner's, not the CLI's. That means the shared flags in the
table above (`--langs`, `--md-root`, `--manifest`) do **not** apply here, and
`run --help` answers with the runner's own flags rather than the CLI's, which is
the accurate help for what you are actually configuring.

```bash
venv/bin/cl10n run                              # default queue
venv/bin/cl10n run l10n/queue/queue.json -c 8
venv/bin/cl10n run --dry-run                    # no network
venv/bin/cl10n run -c 8 --dry-run               # flags without a path
venv/bin/cl10n run --provider nvidia            # a different provider
venv/bin/cl10n run --model nvidia:some/model    # prefix routes too
venv/bin/cl10n run --help                       # the runner's flags
```

The queue path is optional in every position — omit it and the default is used.

| Flag | Default | Meaning |
| --- | --- | --- |
| `queue` | `l10n/queue/queue.json` | positional; may be omitted |
| `--tm-dir` | `l10n/tm` | where translations are written |
| `-c`, `--concurrency` | `4` | in-flight requests |
| `--request-timeout` | `120.0` | seconds per request; `0` disables |
| `--provider` | the registry default (`groq`) | which connector to use |
| `--model` | the selected provider's `default_model` | model id, optionally `provider:model` |
| `-n`, `--dry-run` | off | report what would be called, contact nothing |
| `--creds-file` | the provider's `api_key_creds_file` | read its key from here if the env var is unset |
| `--providers` | `cl10n/providers.toml` | path to the provider registry |
| `--json` | off | machine-readable output |

#### Choosing a provider

Three ways to say which provider runs, in priority order:

1. **A prefix on `--model`** — `--model nvidia:nvidia/nemotron-3-ultra-550b-a55b`
   selects the provider *and* the model. The prefix wins over `--provider`.
2. **`--provider nvidia`** — selects the connector; a bare `--model` is then
   interpreted as that provider's model, and no `--model` uses its default.
3. **Neither** — the registry default, `groq`, with its default model. This is
   exactly the behaviour the pipeline had before providers became pluggable.

The model id is passed to the provider untouched once any prefix is stripped, so
`--model groq:openai/gpt-oss-120b` and `--model openai/gpt-oss-120b` send the
same thing.

Translations are **shared across providers**. The translation memory is keyed by
content hash and prompt version, not by provider, so a unit translated by one
provider is reused by a run routed to another — switching provider does not
re-translate the corpus. Only `model` in the entry's provenance records which
one produced it.

`--dry-run` is the cost estimate:

```
DRY RUN 20260804T144118Z-da60d6 — 794 job(s)
  already terminal : 0
  translation memory hits : 0
  API calls that would be made : 794  {'he': 397, 'ru': 397}
    CALL he:b291259171df15f2
    CALL he:b471696f63c09623
    ...
```

`run` is **resumable, and this is the single most important property to trust.**
Kill it at any point — Ctrl-C, SIGKILL, laptop lid, CI timeout — and re-run it.
Jobs that reached `done` or `rejected` are never re-billed. You do not need to
pass a resume flag, and you do not even need the same queue file: if you lose
`l10n/queue/` entirely, just run `plan` again and it will re-derive a queue
containing only what is still missing, because the memory is the resume state.

Concurrency is bounded by your account's rate limit, not by this flag. On a
free-tier Groq account at 8000 TPM, measured on 30 real units: 232s serially
versus 156s at `-c 8`. Pushing concurrency higher mostly produces more 429s, and
an account-wide rate-limit gate parks the whole run when one arrives rather than
letting every worker retry into the same wall. Those numbers are Groq's tier —
every provider meters differently, so re-measure rather than assuming `-c 8`
transfers. The gate itself is provider-independent.

A rejected job is a reportable outcome, not a crash: `run` exits `1`, the queue
records the failure, and the renderer falls back to English for that unit.

### 5.3 `render`

Splices the memory into the source tree and writes the locale files.

```bash
venv/bin/cl10n render --langs he,ru
venv/bin/cl10n render --langs he md/skills/guide.md
venv/bin/cl10n render --langs he,ru --dry-run --report /tmp/r.json
```

| Flag | Default | Meaning |
| --- | --- | --- |
| `--out-dir` | `locales` | root of the rendered mirror |
| `--queue` | `l10n/queue/queue.json` | queue to take the run id from |
| `--run-id` | from queue | override the recorded run id |
| `--report` | none | write the JSON render report here |
| `-n`, `--dry-run` | off | render and report, write nothing at all |
| `--fail-on-fallback` | off | exit non-zero if any unit rendered as English |
| `--no-verify` | off | skip the block-structure check (don't) |
| `--no-gc` | off | keep memory entries no document references |

Output paths mirror the source: `md/skills/x/SKILL.md` becomes
`locales/he/skills/x/SKILL.md`.

```
locales/he/skills/_shared/jira-api-reference.md [he]: 176/176 units translated, 31 opaque block(s) verbatim
locales/ru/skills/_shared/jira-api-reference.md [ru]: 176/176 units translated, 31 opaque block(s) verbatim
...
6 file(s) rendered across 2 language(s); 2 English fallback(s), 0 placeholder violation(s)
```

**Structure comes from the source, never from the translation.** The document is
parsed once and the only thing replaced is the inline content of each unit.
Heading levels, list nesting, table shape and code fences are preserved by
construction — there is no code path that could emit different block structure.
Each file is then re-parsed and refused if its block structure moved anyway.

Three things can happen to a unit, and none is silent:

| Memory state | Rendered as | Counted as |
| --- | --- | --- |
| entry present, placeholders intact | the translation | `translated` |
| no entry, or an empty one, or the job was rejected | the English source | `fallback` |
| entry present but a placeholder was lost | the English source | `violation` |

A **violation** is worse than a fallback: it means a *broken* translation is
sitting in your memory. The memory is a committed, hand-editable file, so the
renderer re-checks every entry before splicing it — this is the last line of
defence between a mangled command and a published document.

`render` also does the manifest bookkeeping and the `RETIRE` garbage collection
that the reassembly component leaves to an orchestrator. Garbage collection runs
only after a **clean, full-corpus** render: a single-file render sees a partial
ledger, and collecting against it would delete other documents' translations.

If any language for a document is refused with a structure mismatch, **the whole
document's manifest entry is held back**, not just that language's. The ledger's
revision fields are per-document, so advancing them for the languages that did
render would tell the next run that this revision is localized and strand the
refused language for ever.

### 5.4 `status`

Read-only coverage report. Writes nothing, needs no key, safe at any time.

```bash
venv/bin/cl10n status --langs he,ru
venv/bin/cl10n status --langs he,ru --json
venv/bin/cl10n status --langs he --fail-on-incomplete
```

| Flag | Default | Meaning |
| --- | --- | --- |
| `--out-dir` | `locales` | where to look for rendered files |
| `--fail-on-incomplete` | off | exit non-zero while any unit is untranslated |

```
3 document(s) under md, 399 translation unit(s)

he: 398/399 units translated (99.7%), 1 fallback(s), 0 document(s) needing a render
     md/skills/_shared/jira-api-reference.md: 167/168, 1 fallback(s)
     md/skills/_shared/project-config.md: 112/112
     md/skills/jira-task-assigner/SKILL.md: 119/119
ru: 398/399 units translated (99.7%), 1 fallback(s), 0 document(s) needing a render
     md/skills/_shared/jira-api-reference.md: 167/168, 1 fallback(s)
     ...

('*' = the source changed since the last render)
```

Two per-document markers:

- `*` — the source has changed since the last render; this document needs
  `render` (and possibly `plan`/`run` first).
- `[not rendered]` — no file exists at the expected locale path at all.

The `--json` form gives you the same data plus per-document review-status
breakdowns:

```json
{
  "he": { "units": 399, "translated": 398, "missing": 1,
          "fallbacks": 1, "stale_documents": 0 },
  "ru": { "units": 399, "translated": 398, "missing": 1,
          "fallbacks": 1, "stale_documents": 0 }
}
```

______________________________________________________________________

## 6. Reading the numbers

The three commands report three different counts of "units" and they will not
match. This is correct, and it confuses everyone once. For this repository's
corpus:

| Number | Value | What it counts |
| --- | --- | --- |
| `plan`'s action tally (`TRANSLATE=416`) | **416** | unit *occurrences*, summed over documents |
| `status`'s "translation unit(s)" | **399** | distinct units *within* each document, summed |
| `plan`'s jobs per language | **397** | distinct units across the *whole corpus* |

Reading down that list is reading three successive deduplications:

- 416 → 399: seventeen units repeat *inside* a single document. The corpus has
  many short table cells, and `| yes |` appearing in five rows is one unit
  occurring five times.
- 399 → 397: two units are shared *between* documents. The same sentence in two
  files is one entry in the memory and one job in the queue.

So `plan` reports `794 job(s)` for two languages — 397 × 2, not 416 × 2 and not
399 × 2. Every one of those reductions is money you did not spend.

One more number that looks wrong and isn't. Once the corpus is fully localized
into two languages, `plan` reports:

```
  TRANSLATE=0  REVISE=0  RECHECK=0  REUSE=416  COPY=0  RETIRE=0
  0 job(s) → l10n/queue/queue.json
  798 unit(s) already in the translation memory, 0 flagged for recheck
  render required: no
```

798 is 399 × 2, not 397 × 2. Memory hits are counted **per document**, so the
two units shared between documents are counted once for each document that
contains them. The figure measures work avoided, not distinct entries — which is
the useful reading, since each of those lookups is a job that did not happen.

If that number comes back *lower* than 399 × your language count, the difference
is exactly what is still owed. A corpus with one rejected unit per language
reports 796, and `plan` will have enqueued those two jobs.

______________________________________________________________________

## 7. The files the pipeline owns

```
md/**.md                      source corpus            committed  (you write this)
locales/<lang>/**.md          rendered translations    committed  (the product)
l10n/tm/<lang>.json           translation memory       committed  (the real asset)
l10n/manifest.json            per-document ledger      committed
l10n/queue/queue.json         one run's state          GITIGNORED
cl10n/providers.toml          the provider registry    committed
*creds*                       provider keys            GITIGNORED
```

The creds glob has no separator or extension filter, and both widenings were
near-misses: `nvidia-nim-creds.txt` while only `*_creds.txt` was ignored, then a
`.mistral-creds.txt.swp` swap file holding a key in plain text. A key file that
does not match the ignore pattern is one `git add -A` away from being published.

### `l10n/tm/<lang>.json` — the translation memory

One file per language, keyed by unit hash, serialized with sorted keys so diffs
stay local and reviewable.

```json
{
  "059ddd5a5b192501": {
    "source": "**Yes** — checked into the repo",
    "translation": "«**Yes** — checked into the repo»",
    "model": "stub/model",
    "prompt_version": "v1",
    "translated_at": "2026-08-04T14:42:36Z",
    "review_status": "machine",
    "action": "TRANSLATE"
  }
}
```

`review_status` is the field a human moves. `machine` is fresh and unreviewed;
`recheck` means the text is unchanged but moved position, so its context may
have shifted; `approved` is human-reviewed and is the one thing here that cannot
be regenerated — the crash-recovery merge is deliberately built so a restored
entry never overwrites a committed one, precisely to protect it.

`prompt_version` controls whether `plan` and `status` consider an entry usable.
`render` deliberately does **not** check it: an entry from an older prompt is
still a real translation and shipping it beats shipping English. This is why
`python -m cl10n.pseudo_tm`, which stamps `prompt_version: "pseudo"`, can render the
whole corpus while `status` correctly reports 0% translated.

One file per language rather than one file total is a merge-conflict decision:
parallel language runs then write disjoint files, and a Hebrew reviewer's diff
never touches Russian.

### `l10n/manifest.json` — the ledger

```json
{
  "schema": "manifest/v1",
  "languages": ["he", "ru"],
  "files": {
    "md/skills/_shared/project-config.md": {
      "source_blob": "31d57a0a7cb2337bb159e75c5c9232f5ae5e8b9f",
      "doc_hash": "016f1ed72c35ead3",
      "unit_hashes": ["22f2f81ef1745ebf", "28c3452bd63cf5a4", "… 121 total"],
      "opaque_hashes": ["ff38d097d68001c1", "… 4 total"],
      "localized": {
        "he": { "run_id": "20260804T144233Z-e47181",
                "completed_at": "2026-08-04T14:42:41Z",
                "fallbacks": [] },
        "ru": { "run_id": "20260804T144233Z-e47181",
                "completed_at": "2026-08-04T14:42:41Z",
                "fallbacks": [] }
      }
    }
  }
}
```

`source_blob` is the git blob SHA of the last-localized revision — this is how
`plan` recovers the old text without keeping a second copy of the corpus on
disk. `unit_hashes` doubles as the reference count for garbage collection: a
memory entry dies only when no file lists its hash. `fallbacks` records which
units shipped as English, so CI can say "localized with 3 fallbacks" instead of
shipping them silently.

An unreadable blob — a shallow clone, a rewritten history, a manifest carried
between repos — is **not an error**. That document is simply planned against
the empty document, everything reads as new, and the memory turns almost all of
it back into no-ops.

### `l10n/queue/` — never committed

A queue is meaningful only to the run that owns it. Committing one would ship
transient state and cause exactly the merge conflicts the per-language memory
files avoid. If you lose it, run `plan` again.

______________________________________________________________________

## 8. Flow: adding a language

**There is no separate first-time flow.** Adding German to a project already
localized into Hebrew and Russian is the same three commands with a longer
`--langs`.

```bash
venv/bin/cl10n plan --langs he,ru,de
```

```
  TRANSLATE=0  REVISE=0  RECHECK=0  REUSE=416  COPY=0  RETIRE=0
  397 job(s) → l10n/queue/queue.json  {'he': 0, 'ru': 0, 'de': 397}
```

Every existing Hebrew and Russian unit is a memory hit and costs nothing;
German is planned from scratch. The diff is identical for all three languages —
what differs is only which memory has entries.

```bash
venv/bin/cl10n run -c 8
venv/bin/cl10n render --langs he,ru,de
venv/bin/cl10n status --langs he,ru,de
git add locales l10n/tm l10n/manifest.json
git commit -m "l10n: add German"
```

Then make the new language permanent so nobody has to remember the flag:

- update `LANGS` in `.github/workflows/cl10n.yml`;
- if you use the defaults a lot, change `DEFAULT_LANGS` in `cl10n/cli.py`.

The manifest's `languages` list updates itself — `plan` appends any language it
is asked about that the ledger has not seen.

**Budget the first run.** A new language is the whole corpus, so `run --dry-run`
first and check the call count against your rate limit. If it is large, there is
no harm in doing it in slices; each slice is a normal resumable run:

```bash
venv/bin/cl10n plan --langs de md/section-one
venv/bin/cl10n run -c 8
venv/bin/cl10n plan --langs de md/section-two
venv/bin/cl10n run -c 8
venv/bin/cl10n render --langs he,ru,de       # full corpus at the end
```

Render the **full corpus** at the end, not per slice — garbage collection only
runs on a full render, and a single-file render deliberately skips it.

**Removing a language** is not a CLI operation. Delete `l10n/tm/<lang>.json`,
delete `locales/<lang>/`, drop it from `--langs` and from the workflow. The
manifest keeps a `localized.<lang>` record until that document is next rendered.

______________________________________________________________________

## 9. Flow: day-to-day continuous localization

Once a project is set up, this is the whole loop:

```bash
# 1. Somebody edits English and commits.
$EDITOR md/skills/guide.md
git commit -am "clarify the rollback section"

# 2. What will this cost?
venv/bin/cl10n plan --langs he,ru
venv/bin/cl10n run --dry-run

# 3. Pay for it, render, check.
venv/bin/cl10n run -c 8
venv/bin/cl10n render --langs he,ru
venv/bin/cl10n status --langs he,ru

# 4. Commit the three outputs together.
git add locales l10n/tm l10n/manifest.json
git commit -m "l10n: update translations"
```

Commit those three paths **in one commit**. The manifest claims that a given
revision is localized; committing it without the locale files it describes
leaves the repository asserting something the tree does not show.

In CI, `.github/workflows/cl10n.yml` does exactly this on every push to the
default branch that touches `md/**`, and opens a pull request from the fixed
branch `cl10n/translations` rather than pushing. Local runs and CI runs are the
same commands, so anything you debug by hand is what CI does.

**What to look at in review.** The memory diff is the translations. A
`locales/**` diff with no corresponding `l10n/tm/**` diff means an opaque block
changed (a code fence, front matter) with no translation involved.

______________________________________________________________________

## 10. Flow: integrating a new project

Vendoring the pipeline into another repository has its own guide, because it has
its own failure modes — which files to copy (and which emphatically not to), why
the upstream test suite does not belong in your project, what committing
actually buys you, and the manifest rule for a repository with two corpus roots
that silently deletes translations if you get it backwards.

**→ [`INTEGRATION.md`](INTEGRATION.md)**

It was written by following it from an empty directory to a working Hebrew
localization against the live provider, so every transcript in it is real.

## 11. Cookbook

**Estimate cost without spending anything**

```bash
venv/bin/cl10n plan --langs he,ru
venv/bin/cl10n run --dry-run
```

**Translate one document only**

```bash
venv/bin/cl10n plan --langs he md/skills/guide.md
venv/bin/cl10n run
venv/bin/cl10n render --langs he md/skills/guide.md
```

Garbage collection is skipped for single-file renders by design.

**Resume an interrupted run** — just re-run it. Or, if the queue is gone:

```bash
venv/bin/cl10n plan --langs he,ru    # re-derives what is missing
venv/bin/cl10n run -c 8
```

**Recover translations from a crashed CI job.** If a run wrote a memory that was
never merged — an unmerged PR branch, a build artifact — fold it in before
planning. Earlier directories win, and a committed entry always beats a restored
one:

```bash
venv/bin/cl10n plan --langs he --restore-tm /tmp/salvaged-tm
```

```
PLAN 20260804T145103Z-efaebe — 1 document(s), 1 language(s)
  TRANSLATE=3  REVISE=0  RECHECK=0  REUSE=0  COPY=0  RETIRE=0
  0 job(s) → l10n/queue/queue.json
  3 unit(s) already in the translation memory, 0 flagged for recheck
  restored from ['/tmp/salvaged-tm']: {'he': 3}
  render required: yes
```

That output is the whole design in five lines. The diff still says `TRANSLATE=3`
— it has no manifest entry, so every unit looks new — and yet **zero jobs** are
enqueued, because the restored memory already answers all three. The diff
supplies the action; the memory supplies the decision.

Repeat the flag to stack sources, cheapest and freshest first:

```bash
venv/bin/cl10n plan --langs he,ru \
  --restore-tm /tmp/from-pr-branch --restore-tm /tmp/from-artifact
```

**Fix a bad translation by hand.** Edit `l10n/tm/<lang>.json` directly, set
`review_status` to `approved` so it is recognisably human work, then re-render:

```bash
venv/bin/cl10n render --langs he
```

Do not touch `source` or the key — the key is a hash of `source`, and changing
either makes the entry unreachable. To force a **re-translation** instead, delete
the entry and run `plan` again; it will be re-enqueued.

**Blank a translation to retranslate it.** An entry whose `translation` is empty
or whitespace is treated as missing by `plan` and as a fallback by `render`, so
emptying one is a safe way to queue a redo.

**Gate CI on completeness**

```bash
venv/bin/cl10n status --langs he,ru --fail-on-incomplete
venv/bin/cl10n render --langs he,ru --fail-on-fallback
```

**See what render would do without writing**

```bash
venv/bin/cl10n render --langs he,ru --dry-run --report /tmp/r.json
```

`--dry-run` writes nothing at all — no locale files, no manifest, no collection.

**Machine-readable everything**

```bash
venv/bin/cl10n plan   --langs he --json | jq .jobs
venv/bin/cl10n status --langs he --json | jq .totals.he.missing
venv/bin/cl10n plan   --langs he --report /tmp/plan.json
venv/bin/cl10n render --langs he --report /tmp/render.json
venv/bin/python3 -m cl10n.ci_report --plan /tmp/plan.json \
  --queue l10n/queue/queue.json --render /tmp/render.json -o /tmp/body.md
```

______________________________________________________________________

## 12. Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `no source markdown found under md` | wrong `--md-root`, or you are not at the repo root | `cd` to the root or pass `--md-root` |
| `<KEY> is not set` (e.g. `GROQ_API_KEY`, `NVIDIA_NIM_API_KEY`) | no env var and no creds file for the **selected** provider | export it, or `--creds-file`; the name in the message is the provider's `api_key_env` |
| `unknown provider 'x'` | `--provider` or a `provider:` model prefix names something not in `providers.toml` | the message lists what is declared |
| `module 'groq' has no attribute 'GroqTranslator'` | a connector is being resolved by module name instead of by file path | see [`PROVIDERS.md`](PROVIDERS.md) → the name-collision trap |
| `status` says 0% but files rendered fine | memory entries carry an older `prompt_version` | expected with `pseudo_tm`; otherwise re-run `plan`/`run` |
| `plan` finds everything new after a history rewrite | `source_blob` unreachable | harmless — memory turns it back into no-ops |
| `plan` keeps enqueueing the same unit | its job ends `rejected` every run | look at `error` in the queue file; often a placeholder the model keeps dropping |
| `render` exits 1, `N placeholder violation(s)` | a **broken** translation is in the memory | fix or delete those entries, then re-render |
| `STRUCTURE MISMATCH` | a translation changed the block structure | the file was *not* written; fix that entry |
| `run` exits 1 with rejected jobs | attempts exhausted | re-run; if it persists, raise `plan --max-attempts` |
| lots of `rate_limit` errors | concurrency above your tier | lower `-c`, raise `--max-attempts` |
| everything reads `[not rendered]` | `--out-dir` differs between `render` and `status` | pass the same flags to both |

**A unit will not stop being re-enqueued.** Inspect it:

```bash
venv/bin/python3 - <<'EOF'
import json
q = json.load(open("l10n/queue/queue.json"))
for job in q["jobs"]:
    if job["state"] in ("rejected", "failed"):
        print(job["id"], job["attempts"], job.get("error"))
        print("  ", job["source"][:120])
EOF
```

`error.kind` tells you which way to go. `placeholder_lost` means the model keeps
dropping a literal — often a long inline code span; consider rewording the
English. `api_error` on 401/403/400 is terminal and will never succeed on retry.
`rate_limit` means lower concurrency, not more retries.

**A rendered file looks wrong.** Check whether the unit is a fallback (English,
expected, recorded in the manifest) or a bad translation (in the memory, fix it
there). `status --json` gives per-document fallback counts; the manifest gives
you the exact hashes under `localized.<lang>.fallbacks`.

______________________________________________________________________

## 13. Appendix

### Exit codes

| Code | `plan` | `run` | `render` | `status` |
| --- | --- | --- | --- | --- |
| `0` | planned | all jobs terminal, none rejected | rendered cleanly | reported |
| `1` | — | one or more jobs `rejected` | violation, structure mismatch, or `--fail-on-fallback` with fallbacks | `--fail-on-incomplete` with missing units |
| `2` | no source markdown found | the selected provider's key is unset | no source markdown found | no source markdown found |

### Environment

| Variable | Used by | Notes |
| --- | --- | --- |
| `GROQ_API_KEY` | `run`, provider `groq` | env wins over the creds file |
| `NVIDIA_NIM_API_KEY` | `run`, provider `nvidia` | env wins over the creds file |
| `MISTRAL_API_KEY` | `run`, provider `mistral` | env wins over the creds file |

Only the selected provider's variable is read. The authoritative list is the
`api_key_env` of each entry in `cl10n/providers.toml`.

### Defaults

| Constant | Value | Where |
| --- | --- | --- |
| languages | `he,ru` | `cli.DEFAULT_LANGS` |
| corpus root | `md` | `cli.DEFAULT_MD_ROOT` |
| memory dir | `l10n/tm` | `cli.DEFAULT_TM_DIR` |
| locales root | `locales` | `cli.DEFAULT_LOCALES_ROOT` |
| queue | `l10n/queue/queue.json` | `cli.DEFAULT_QUEUE` |
| manifest | `l10n/manifest.json` | `manifest.DEFAULT_MANIFEST` |
| concurrency | `4` | `queue_runner.DEFAULT_CONCURRENCY` |
| request timeout | `120.0`s | `queue_runner.DEFAULT_REQUEST_TIMEOUT` |
| provider | `groq` | `providers.toml` → `default` |
| model | `openai/gpt-oss-120b` | `providers.toml` → `[providers.groq] default_model` |
| prompt version | `v1` | `prompt.PROMPT_VERSION` (`cl10n/core/prompt.py`) |
| max attempts | `3` | `plan --max-attempts` |

The prompt, its version and the language-name table are **provider-agnostic**
and live in `cl10n/core/prompt.py`. `cl10n/core/groq_api.py` re-exports them for backward
compatibility, but new code should import from `prompt`.

### `plan --report` shape

```json
{
  "langs": ["he", "ru"], "documents": 3, "run_id": "...", "source_commit": "...",
  "actions": {"TRANSLATE": 416, "REVISE": 0, "RECHECK": 0,
              "REUSE": 0, "COPY": 40, "RETIRE": 0},
  "jobs": 794,
  "jobs_by_lang": {"he": 397, "ru": 397},
  "jobs_by_action": {"TRANSLATE": 794, "REVISE": 0},
  "translation_memory_hits": 0, "rechecked": 0,
  "restored_entries": {}, "render_required": true,
  "queue": "l10n/queue/queue.json",
  "files": [
    {"path": "md/skills/guide.md", "units": 119, "actions": {"TRANSLATE": 119},
     "jobs": 119, "previous_revision": false, "render_required": true}
  ]
}
```

### `render --report` shape

```json
{
  "summary": {
    "run_id": "20260804T144233Z-e47181", "langs": ["he", "ru"],
    "files_rendered": 6, "units": 832, "translated": 830, "opaque": 80,
    "fallbacks": 2, "violations": 0, "structure_mismatches": 0,
    "retired_entries": []
  },
  "files": [
    {"lang": "he", "source": "md/...", "target": "locales/he/...",
     "units": 176, "translated": 176, "opaque": 31,
     "fallbacks": [], "fallback_hashes": []}
  ]
}
```

### Things the CLI deliberately will not do

- **No bootstrap mode.** First-time and incremental are one sequence.
- **`run` takes no CLI-specific flags.** It is a verbatim delegation, so there is
  never a second argument surface to keep in step.
- **`plan` never writes the manifest or the locales.** It writes the queue and,
  for no-API state changes, the memory. `render` owns the rest.
- **`render` never invents structure.** It replaces inline content and nothing
  else, and refuses to write a file whose block structure moved.
- **Nothing here commits or pushes.** Committing the three output paths is yours
  to do, or the workflow's.

### See also

| Document | What it covers |
| --- | --- |
| [`cl10n-cli-spec.md`](../.claude/rules/cl10n-cli-spec.md) | why the memory is the resume state, the enqueue rule, CI decisions |
| [`l10n-pipeline-spec.md`](../.claude/rules/l10n-pipeline-spec.md) | the twelve pipeline steps and the three JSON contracts |
| [`cl10n-runner-spec.md`](../.claude/rules/cl10n-runner-spec.md) | write orderings, retry classification, the rate-limit gate |
| [`cl10n-reassembly-spec.md`](../.claude/rules/cl10n-reassembly-spec.md) | why the splice is the only mutation, the render-time gate |
| [`tree-diff-spec.md`](../.claude/rules/tree-diff-spec.md) | segmentation, hashing, why tree edit distance is the wrong tool |
