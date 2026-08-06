---
description: >-
  cl10n/reassemble.py — how translated units are spliced back into the AST and
  rendered to locales/<lang>/. Read before changing the splice, the fallback
  rules, the render-time placeholder gate or the structure check.
paths:
  - cl10n/reassemble.py
  - cl10n/pseudo_tm.py
  - cl10n/placeholders.py
  - locales/**
---

# `cl10n/reassemble.py` — translation memory → `locales/<lang>/`

Steps 10-11 of [`l10n-pipeline-spec.md`](l10n-pipeline-spec.md), which owns the
contract (COPY semantics §2, English fallback §5, file layout §6) and wins
wherever the two disagree.

## Use it

```bash
venv/bin/python3 -m cl10n.reassemble --langs he,ru                  # md/**/*.md → locales/
venv/bin/python3 -m cl10n.reassemble --langs he md/skills/x/SKILL.md
venv/bin/python3 -m cl10n.reassemble --langs he,ru --dry-run --report l10n/render.json
```

Exit 1 on a placeholder violation or a structure mismatch. `--fail-on-fallback`
also fails on plain untranslated units — for CI, not for normal runs.

| Entry point | |
| --- | --- |
| `render_markdown(md, entries, *, lang) -> (str, RenderReport)` | the core; `entries` is a `TranslationMemory.entries` mapping |
| `render_file(src, entries, out, *, lang) -> RenderReport` | the same, plus read and atomic write |
| `locale_path(src, lang)` | `md/<rel>` → `locales/<lang>/<rel>` |
| `canonicalise(md)` | the mdformat round-trip every unit hash is taken over |
| `block_signature(md)` | block structure as a nested tuple — how you compare two documents |
| `RenderReport.fallback_hashes` | distinct hashes rendered as English = the manifest's `localized.<lang>.fallbacks` |

## How it works

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

The only mutation is an `inline` token's `children` and `content`. Everything
else in the stream is the source's, so heading levels, list nesting, table
shape and fences are preserved **by construction** — there is no code path that
builds block structure, therefore none that can build it wrong. Opaque blocks
(fences, raw HTML, front matter) are `COPY`: never looked up, never touched.

Three consequences worth knowing before you edit:

- `SyntaxTreeNode` wraps the very `Token` objects it was built from, so
  mutating the tree and rendering the original flat list is the same thing.
- The fallback is *nothing happening* — never a reconstruction from the TM's
  `source` field. A fallback that rebuilds text can rebuild it wrongly.
- `parse_inline` runs inline rules only, so a translation starting with `- `
  stays a paragraph instead of becoming a list.

## Fallbacks and the render-time gate

| TM state | reported as |
| --- | --- |
| no entry — never translated, or its job ended `rejected` | `untranslated` |
| entry whose translation is empty (rendering it would blank a paragraph) | `empty_translation` |
| entry that lost a placeholder | `placeholder_lost` |

The gate is [`cl10n/placeholders.py`](../../cl10n/placeholders.py), the same
rule `queue_runner` applies to API responses. Running it *again* here is not
redundant: `l10n/tm/<lang>.json` is committed and hand-editable
(`review_status: "approved"` means a human edited it), and the renderer is the
last thing between a broken command and a published document.

Every render re-parses its own output and compares `block_signature` with the
source. On a mismatch it raises `StructureMismatch`, the CLI **does not write
that file** and exits non-zero, and the rest of the corpus still renders.

## Two hazards, both handled

- **Newline in a `th`/`td` translation** ends the GFM pipe-table row and grows
  the document a phantom line — the one corruption the splice can cause on its
  own. Whitespace is collapsed for cells only (`SINGLE_LINE_TYPES`); mdformat
  already collapses newlines in headings, and a paragraph's soft breaks are
  legitimate.
- **RTL needed nothing**, and the reason matters so nobody adds any:
  `ast_to_markdown` uses `compact_tables`, so cells are never padded to a
  column width and the wcwidth/bidi questions never arise.
- **A task-list checkbox is block structure parked in an inline child.** The
  tasklists extension represents `- [x] ` as a leading `html_inline` token
  among the item's *inline* children, and mdformat reads `checked="checked"`
  back out of it. Replacing those children wholesale — which is what the splice
  does — drops it, leaving a `list_item` still classed `task-list-item` with no
  checkbox to render, which raises rather than degrading. `_tasklist_checkbox`
  carries the original token across; it is the one child of an inline token
  that is not translatable content.

## Not here

- **The manifest.** This reports `fallback_hashes`; writing them, plus
  `source_blob`, `doc_hash` and RETIRE GC, is the orchestrator's.
- **Choosing what to render.** Pipeline spec §2: render whenever `plan` emitted
  anything but pure `REUSE`. This module renders what it is given.
- **Anything provider- or queue-shaped.** An `await` or a Groq import in here
  means something went wrong.
- `cl10n/pseudo_tm.py` is **dev scaffolding** — a pseudolocalized memory so the
  corpus renders in both languages with no API key. It writes gibberish into
  what is otherwise a committed artifact; point it at a scratch directory.

## Don't break

1. One mutation only: an `inline` token's `children` and `content`.
2. No usable translation ⇒ leave the tokens alone.
3. Unit identity, unit source and placeholders come from `tree_diff` — the
   private aliases at the top of the module are deliberate, so a rename there
   breaks this import instead of silently drifting.
4. The structure check runs on every render and refuses to write on mismatch.
5. Writes go through `l10n_store.atomic_write_text`, like every other state
   file in the pipeline.
