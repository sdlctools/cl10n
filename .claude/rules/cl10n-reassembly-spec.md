---
description: >-
  Why reassembly splices inline tokens instead of rebuilding Markdown, what
  the render-time placeholder gate is for, and the two hazards that turned out
  to need explicit handling. Read before changing cl10n/reassemble.py, the
  splice, the fallback rules, or the structure check.
paths:
  - cl10n/reassemble.py
  - cl10n/pseudo_tm.py
  - cl10n/placeholders.py
  - locales/**
---

# `cl10n/reassemble.py` — splicing translations back into the tree

Steps 10-11 of [`l10n-pipeline-spec.md`](l10n-pipeline-spec.md): translation
memory plus source document in, `locales/<lang>/<mirrored path>` out. That spec
fixes the *contract* (COPY semantics in its §2, the English fallback in its §5,
the file layout in its §6) and leaves the internals here. Where the two
disagree, the pipeline spec wins and this file is the bug.

```bash
venv/bin/python3 cl10n/reassemble.py --langs he,ru
venv/bin/python3 cl10n/reassemble.py --langs he md/skills/_shared/project-config.md
venv/bin/python3 cl10n/reassemble.py --langs he,ru --dry-run --fail-on-fallback
```

## 1. The one design decision: structure is never re-derived

The document is canonicalised, parsed once, and the only mutation is to the
`inline` token of each translation unit — its children are replaced by the
tokenisation of the translation, and everything else in the token stream is
the source's.

```
canonicalise(source) ──► tokens ──► SyntaxTreeNode ──► hash_tree
                            │                              │
                            │        unit.h ──► TM lookup ──┘
                            │             │
                            │             ├─ hit  ─► inline.children = parse_inline(translation)
                            │             └─ miss ─► leave the tokens alone  ← the English fallback
                            ▼
                     ast_to_markdown ──► locales/<lang>/…
```

Heading levels, list nesting, table shape, block quotes and fences are
therefore preserved **by construction**: no code path can emit a different
block structure, because no code path constructs block structure at all. This
is the whole reason to do it this way — the corruption this component could
cause is invisible to anyone who does not read the target language, so
"careful code" is not an acceptable substitute for "no such code path".

It is also why the fallback is *nothing happening*. A miss does not
reconstruct the English text from the TM's `source` field or from a saved
string; it simply skips the splice, and the source tokens render themselves.
A fallback path that has to rebuild something is a fallback path that can
rebuild it wrongly.

`SyntaxTreeNode` wraps the very `Token` objects it was built from, so mutating
`inline.token` and then rendering the original flat token list is not a trick —
it is the same object.

### Why `parse_inline`, not a block parse

A translation is inline content by definition. `utils.parse_inline` runs
markdown-it's inline rules only, so a translated segment that happens to begin
with `- ` or `1. ` stays one paragraph instead of quietly becoming a list.
Block-parsing the translated string and fishing the `inline` token out of the
result would look equivalent and would not be.

### Canonicalise first

`canonicalise` is the same mdformat round-trip `tree_diff` hashes over, so the
unit hashes computed here are the ones the planner enqueued and the runner
keyed the TM by. It also makes AC1 — empty TM reproduces the source byte for
byte — a statement about a stable form rather than about whatever the file
happened to look like. Verified on the corpus: the round trip is idempotent,
and unit hashes are identical before and after it.

## 2. The render-time placeholder gate

The rule lives in [`cl10n/placeholders.py`](../../cl10n/placeholders.py) and is
enforced twice: by `queue_runner` on every API response, and again here on
every TM entry about to be spliced.

The second check is **not** redundant. `l10n/tm/<lang>.json` is a committed,
reviewable, human-editable artifact — that is what `review_status: "approved"`
means — and it can also be written by an older runner or a different
implementation of it (the pipeline spec's §7 compatibility bar invites exactly
that). The renderer is the last thing standing between a broken command or a
redirected link and a published document, so it checks.

A unit renders as English in three cases, and the report names all three:

| TM state | reason reported |
| --- | --- |
| no entry — never translated, or its job ended `rejected` (pipeline spec §5) | `untranslated` |
| entry whose `translation` is empty or whitespace | `empty_translation` |
| entry that lost a placeholder | `placeholder_lost` |

`empty_translation` exists because rendering an empty string would *blank a
paragraph*, which is worse than leaving English: it is a silent deletion, and
it also breaks the structure check.

The gate compares against the **node's** source text, not the entry's `source`
field. They agree by construction — the entry's key is the hash of that text —
and trusting the node keeps a hand-edited `source` from waving a broken
translation through.

Note a property inherited from `tree_diff._placeholders`: a code span's
placeholder is its *content*, so `` `create` `` contributes the bare string
`create`, and the "at least as many times" rule then counts occurrences of that
word in ordinary prose too. Both gates share the behaviour, so a TM entry the
runner accepted always passes here; it only matters when generating synthetic
translations (see §5).

## 3. Two hazards that needed explicit handling

Everything else fell out of §1. These did not:

**A newline inside a table cell.** GFM pipe-table rows are single-line: a
translation containing `\n` spliced into a `th`/`td` ends the row and the
document grows a phantom table line — the one structural corruption the splice
can cause on its own. Whitespace in a cell translation is therefore collapsed
(`SINGLE_LINE_TYPES`). Headings need no such handling because mdformat already
collapses newlines in them, and a paragraph's soft breaks are legitimate and
must survive.

**Right-to-left output.** Hebrew needed no special handling in the end, and
the reason is worth recording so nobody adds any: `ast_to_markdown` renders
tables with `compact_tables`, so cells are never padded to a column width and
the wcwidth/bidi questions never arise. Verified on the real corpus — nested
lists, tables, inline code and fences all survive the round trip in `he`, and
the rendered files are structurally identical to the source.

## 4. The structure check

Every render re-parses its own output and compares `block_signature` — the
nested tuple of node types and tags, stopping at `inline` — against the
source's. Inside a unit things legitimately differ (a placeholder may sit in a
different clause in Hebrew); what contains it may not.

On a mismatch, `render_markdown` raises `StructureMismatch` and the CLI
**does not write that file** and exits non-zero, while the rest of the corpus
still renders. A structurally corrupt file is the one output worse than no
output. It costs one extra parse per file and it is not optional — `--no-verify`
exists for debugging the checker, not for runs.

## 5. `cl10n/pseudo_tm.py` — development scaffolding

Writes a *pseudolocalized* TM: the prose transliterated into Hebrew or
Cyrillic glyphs, every non-translatable span byte-identical. It exists so the
render path can be exercised end-to-end — both languages, the whole corpus,
RTL text in tables and nested lists — without a single API call or a key, and
it is what most of the test suite's "translations" are.

It is **not** a pipeline component and must not become one: it writes
gibberish into what is otherwise a committed artifact, so point it at a
scratch directory. For a memory with meaning, run `cl10n/queue_runner.py`.

## 6. What is not here

**The manifest.** This module *reports* `fallback_hashes` per (document,
language) — precisely the manifest's `localized.<lang>.fallbacks` (pipeline
spec §6), per hash rather than per occurrence — and `--report` writes them as
JSON. Recording them in `l10n/manifest.json`, along with `source_blob`,
`doc_hash` and the RETIRE garbage collection, belongs to the orchestrator.

**Deciding what to render.** Pipeline spec §2: render whenever `plan` emitted
anything but pure `REUSE`, because a changed `COPY` block changes the output
with no queue and no TM write. This module renders what it is given; choosing
the set is the orchestrator's job.

**Any provider or queue knowledge.** Reassembly reads a TM and a source file.
If you find an `await` or a Groq import in here, something has gone wrong.

## 7. Conventions any change must preserve

1. The token stream is mutated in exactly one place: an `inline` token's
   `children` and `content`. Nothing else is ever constructed or reordered.
2. A unit with no usable translation is left untouched — the fallback is the
   absence of a splice, never a reconstruction.
3. Unit identity, unit source and the placeholder list come from `tree_diff`,
   never from a second implementation here. The private aliases at the top of
   the module are deliberate: a rename there must break this import.
4. The structure check runs on every render and refuses to write on mismatch.
5. Output paths mirror the source tree: `md/<rel>` → `locales/<lang>/<rel>`.
6. Writes go through `l10n_store.atomic_write_text` like every other state
   file in the pipeline.
