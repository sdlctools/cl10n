# cl10n — continuous localization for Markdown

Keep translated mirrors of a Markdown corpus up to date, one changed paragraph
at a time, without ever paying to translate the same text twice.

`cl10n` parses each document into an AST, Merkle-hashes it, and diffs two
revisions **structurally**. What comes out is not "this file changed" but a
per-paragraph verdict: reuse, revise, translate, retire. Only the units that
actually need an LLM become jobs; everything else is served from a committed
translation memory. Rendering then splices translations back into the *source
tree*, so headings, list nesting, table shape and code fences come from the
original and cannot be corrupted by a translation.

```
md/**.md ──► AST + Merkle hash ──► diff vs. last localized revision
                                          │
                    REUSE / RECHECK ──────┤ (no API call)
                    TRANSLATE / REVISE ───┴──► queue ──► provider ──► memory
                                                                       │
                                          locales/<lang>/** ◄── splice ┘
```

## Install

```bash
pip install markdown-localization[groq]          # or markdown-localization[nvidia], markdown-localization[mistral]
pip install markdown-localization[all-providers] # all three connectors
```

The distribution is named `markdown-localization`; the package you import and
the command you run are both `cl10n`.

Python 3.11+. Providers are pluggable: `groq` is the default, NVIDIA NIM and
Mistral ship alongside it, and adding another is one TOML entry plus one
module — [`cl10n/PROVIDERS.md`](cl10n/PROVIDERS.md).

## Quickstart

Point it at a corpus under `md/`, and pick your target languages:

```bash
export GROQ_API_KEY=...

cl10n plan   --langs he,ru          # diff the corpus → a queue of jobs
cl10n run    l10n/queue/queue.json -c 8   # execute the queue
cl10n render --langs he,ru          # memory → locales/he/**, locales/ru/**
cl10n status --langs he,ru          # coverage per language
```

The same four commands serve a first-time translation and a daily update —
there is no bootstrap mode. First-time translation *is* an incremental update
whose previous revision happens to be empty.

Kill a run at any point and re-run it. The resume state is the translation
memory, not the queue: `plan` re-derives what is missing, so finished work is
never re-billed and interrupted work is never lost.

## What you get for free

- **Nothing is translated twice.** Units are content-addressed, so the same
  paragraph in two files costs one translation, and a killed run resumes for
  the price of what it had not reached.
- **Placeholders survive.** Inline code, link targets and image sources are
  extracted per unit and checked against every response; a translation that
  loses one never enters the memory. The check runs again at render time,
  because the memory is a committed, hand-editable file.
- **Structure cannot drift.** Every render re-parses its own output and
  refuses to write a file whose block structure moved.
- **Fallbacks are visible.** A unit with no usable translation renders as
  English and is *counted*, never shipped silently.
- **The parser is pinned, and drift is detected.** Every hash is taken over
  one exact parsing configuration; `python -m cl10n.compat_check` is the gate
  for moving a pin, and CI runs it weekly against the newest releases as an
  early warning.

## Documentation

| | |
| --- | --- |
| [`cl10n/USERGUIDE.md`](cl10n/USERGUIDE.md) | every flag of every subcommand, real output explained, worked flows, cookbook, troubleshooting |
| [`cl10n/PROVIDERS.md`](cl10n/PROVIDERS.md) | teaching the pipeline a new LLM API |
| [`cl10n/INTEGRATION.md`](cl10n/INTEGRATION.md) | adding cl10n to an existing repository, and the CI workflow that runs it |
| [`AGENTS.md`](AGENTS.md) | the repository itself: layout, tests, release process |
| [`.claude/rules/`](.claude/rules/) | the design specs — *why* each component is shaped the way it is |

## License

MIT — see [LICENSE](LICENSE).
