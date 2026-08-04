---
description: >-
  Design spec for incremental Markdown localization — Merkle-hashed AST diff,
  translation-unit segmentation, and translation-memory keying. Read before
  changing how changed content is detected, hashed, segmented, or scored.
paths:
  - app/tree_diff.py
  - app/utils.py
---

# Incremental Markdown localization — AST diff spec

How to decide *what to retranslate* when a Markdown document changes, and why.
Implemented in [`tree_diff.py`](../../app/tree_diff.py).

## The theory — yes, this is a well-studied problem

**Tree Edit Distance (TED)** is the formal frame: minimum-cost sequence of
*insert / delete / relabel* turning tree A into tree B.

| Algorithm | Complexity | Verdict |
| --- | --- | --- |
| Zhang–Shasha (1989) | O(n²·d²) | Exact, classic, too slow + gives no "these are the same node" mapping you can key a TM on |
| RTED (2011) / APTED (2016) | O(n³) worst, provably optimal strategy | State of the art for *exact distance*. We don't need exact distance |
| Chawathe et al. (1996) | O(n·e + e²) | First edit script **with moves**; heuristic |
| **GumTree** (Falleri et al., ASE'14) | ~linear in practice | The standard for source-code AST diffing. Two phases: top-down hash matching of isomorphic subtrees, then bottom-up similarity matching |
| **Merkle hash + per-level LCS** | O(n) hash + O(k²) per *changed* sibling run | ← what we implement |

Two facts worth knowing:

- **Ordered** tree edit distance is polynomial; **unordered** is NP-hard
  (MAX SNP-hard, Zhang–Statman–Shasha 1992). Markdown is an ordered tree —
  never throw that away.
- Edit distance **with moves** is NP-hard in general. Every real tool uses
  heuristics. But Merkle hashing makes the *common* move case exact and free:
  identical hash on both sides = same content, wherever it landed.

The Merkle idea is the whole trick:

```
h(node) = H(type, tag, own_text, h(c₁) … h(cₙ))
```

Subtrees are identical iff hashes match, so the diff never descends into an
unchanged branch. Cost is proportional to the size of the *edit*, not the
document. Per-level LCS (Myers / `difflib` over child hashes) is what stops one
inserted paragraph from marking the whole tail dirty.

### References

- Zhang & Shasha, *Simple fast algorithms for the editing distance between trees
  and related problems*, SIAM J. Comput. 18(6), 1989.
- Zhang, Statman & Shasha, *On the editing distance between unordered labeled
  trees*, Inf. Process. Lett. 42(3), 1992.
- Chawathe, Rajaraman, Garcia-Molina & Widom, *Change detection in
  hierarchically structured information*, SIGMOD 1996.
- Pawlik & Augsten, *RTED: a robust algorithm for the tree edit distance*,
  VLDB 2011; *Tree edit distance: robust and memory-efficient* (APTED),
  Inf. Syst. 56, 2016.
- Falleri, Morandat, Blanc, Martinez & Monperrus, *Fine-grained and accurate
  source code differencing* (GumTree), ASE 2014.

## The translation unit — not "nearest parent node"

Passing the nearest common parent of a change over-inflates: edit one word in a
list item and the nearest parent containing the change can be the entire
`bullet_list`.

The correct concept from localization practice (XLIFF/TMX) is the **translation
unit** — the *smallest* block whose inline content is a complete sentence
context. In markdown-it's tree those are exactly the nodes owning an `inline`
child:

```
heading   paragraph   th   td
```

The `inline` token's `.content` is the raw Markdown source of that segment
(`` Reference for `x`. Read **this** ``), which is exactly the right LLM
payload.

The parent chain isn't useless — it's **read-only context**. Send the heading
trail with the segment so the model resolves gender / deixis / register, but
tell it to translate only the segment.

## XML: not for diffing, yes for the payload

Diff on the AST — hashes are cheaper and lossless; serializing to XML first
just adds a parse round-trip and a way to lose information.

XML earns its place in two other spots:

1. **The LLM envelope.** Tag the non-translatables — `code_inline`, link
   `href`s, image `src`s — as XLIFF-style `<ph>` placeholders, then assert they
   survive round-trip as a cheap quality gate.
2. **TM interchange format** (XLIFF / TMX), if the memory ever leaves the repo.

## You may not need the diff at all

Key a translation memory by unit hash. Then "what's untranslated" is a set
difference — `tm_keys(new) - tm.keys()`, O(n), no tree comparison. Moved
content reuses its translation automatically.

The tree diff is what *upgrades* a plain TM miss into a **revision** (old source
+ old translation + new source), which is cheaper and far more consistent than
translating from scratch. That is the `REVISE` vs `TRANSLATE` distinction below.

## Canonicalization is load-bearing

Both revisions go through the mdformat round-trip
(`ast_to_markdown(markdown_to_ast(src))`) before hashing, and hashing is
additionally whitespace-insensitive. Consequence: re-wrapping a paragraph from
80 to 72 columns is a **no-op**, not a full retranslation.

Deliberately excluded from the hash:

- `map` (line numbers) — shift on every insert above
- `level` — shifts when nesting changes elsewhere

Including either would make every diff document-wide dirty.

### The parser configuration is part of the contract

`utils.make_parser` is the single parser every stage shares, and two of its
options are switched **off** against the `gfm-like2` preset's defaults. Both
for the same reason: markdown-it-py grew a native implementation of a
construct that mdformat cannot render, and a construct that cannot be rendered
cannot be canonicalised, hashed, planned or localized — it raises.

| Option | Off because |
| --- | --- |
| `tasklists` | native parsing marks the item `task-list-item` but emits no checkbox token; `mdformat_gfm`'s list renderer reads that class and asserts on the checkbox only `mdit_py_plugins.tasklists` produces. Off, that plugin — which `mdformat_gfm` installs anyway — owns task lists, and the renderer finds what it expects |
| `alerts` | `> [!NOTE]` parses into `alert` / `alert_title` nodes and mdformat has a renderer for neither: `KeyError: 'alert'`. Off, alerts are ordinary blockquotes, which round-trip byte-for-byte and render identically on GitHub |

Turning `alerts` off puts the `[!NOTE]` marker inside the paragraph's inline
content, so it lands in a translation unit and a model may translate it,
producing a blockquote that only looks like an alert. `_placeholders` therefore
protects the five GitHub alert keywords, which costs nothing and makes a
translated marker a gate failure — a retry, then an English fallback — rather
than a silent downgrade. Doing better would mean teaching mdformat to render
`alert` nodes; until then this is the honest trade.

Both switches are **hash-neutral**: no other construct's token stream changes,
verified byte-for-byte over the corpus. Anything that alters this parser is a
corpus-wide rehash, so `cl10n/tests/test_canonicalise.py` pins the constructs
and their normalisations.

## Pipeline

```
v1.md ─┐
       ├─► canonicalise (mdformat round-trip)
v2.md ─┘        │
                ▼
          markdown-it AST  ──►  Merkle hash every node (bottom-up)
                │
                ▼
   per-level LCS alignment on child hashes   (difflib / Myers)
                │
                ▼
   EQUAL / INSERT / DELETE / UPDATE / MOVE ops
                │
                ▼
   translation units + heading-trail context  ──►  LLM
```

Two global passes run after the local alignment, because a single sibling
window can't see them:

- `_detect_moves` — a DELETE and an INSERT with the same Merkle hash is a move.
- `_fuzzy_pair` — leftover RETIRE + TRANSLATE pairs above 0.6 similarity become
  REVISE. This recovers a unit that was *moved and edited*, which lands in the
  global delete/insert pools. Classic CAT-tool fuzzy matching, keyed on units
  instead of raw lines.

### Actions emitted

| Action | Meaning |
| --- | --- |
| `REUSE` | Hash hit — translation valid as-is |
| `RECHECK` | Moved verbatim — translation valid, context changed |
| `REVISE` | Fuzzy match ≥ 0.6 — send old source + new source + old translation |
| `TRANSLATE` | Fresh segment |
| `RETIRE` | Drop from TM |
| `COPY` | Opaque block (fence / raw HTML / front matter) changed — carry verbatim into the target, no LLM |

`COPY` exists because "never translated" is not the same as "never emitted". A
changed code fence has no translation unit, but the target document still has
to receive the new code — without this the localized copy silently keeps a
stale command forever. Only the *new* side is walked: assembly rebuilds the
target from the new tree, so a removed opaque block disappears by construction,
and since opaque blocks never enter the TM there is nothing to `RETIRE`.

### Thresholds

| Constant | Value | Rationale |
| --- | --- | --- |
| `SIM_THRESHOLD` | 0.4 | Below this two nodes are unrelated rather than "one edited into the other". GumTree uses 0.5 for structural similarity; 0.4 is a little more eager, which suits prose |
| `FUZZY_THRESHOLD` | 0.6 | Below this, a fuzzy match is worse than translating from scratch. Trados / memoQ default their TM cut-off to ~70% for the same reason |

Every `SequenceMatcher` in this codebase must pass `autojunk=False` — use the
`ratio()` helper rather than calling difflib directly. The default heuristic
marks any element occurring in more than 1% of a sequence as junk once the
sequence reaches 200 items; on *character* sequences that is every common
letter, which both skews similarity on long paragraphs and makes the score
asymmetric (`ratio(a, b) != ratio(b, a)`).

## Usage

```bash
venv/bin/python3 app/tree_diff.py            # demo on review-report.md
venv/bin/python3 app/tree_diff.py a.md b.md
```

Demo output — the sample doc plus three synthetic edits (one in-place list-item
edit, one inserted paragraph, one pure reflow):

```
35 translation units — 2 need the LLM (5.7%)
  REUSE=33  REVISE=1  TRANSLATE=1

[REVISE] paragraph  #45ea81ee0da64068  sim=0.70
    old : the Jira per-issue comment (3d),
    new : the Jira per-issue comment (3d), including the audit trail,
```

The reflow correctly produced zero work. Verified separately: a section moved
*and* edited comes back as `MOVE` + `REVISE` (not delete/insert), and table
cells diff independently.

## Implementation notes

- [`hash_tree`](../../app/tree_diff.py) — Merkle hashing; a translation unit's identity is
  its `inline` source string, not its parsed children.
- [`similarity`](../../app/tree_diff.py) — falls back from Dice-over-descendant-hashes to
  flat-text ratio for small containers, because editing a one-paragraph
  `list_item`'s only sentence zeroes its Dice score.
- [`_align_window`](../../app/tree_diff.py) — greedy best-first pairing inside one
  `replace` window; the m×n matrix stays small because the window is one changed
  sibling run.
- [`tm_keys`](../../app/tree_diff.py) — the O(n) shortcut: `{unit_hash: source}` for a
  document.

## Downstream

**Reassembly** — splicing translated `inline` content back into the tree and
rendering via the existing `ast_to_markdown` — is `cl10n/reassemble.py`
([`cl10n-reassembly-spec.md`](cl10n-reassembly-spec.md)). It is where the
placeholder round-trip check pays off, and it consumes this module's unit
segmentation directly (`_units_under`, `_unit_source`, `_placeholders`,
`_opaque_under`) rather than re-deriving it — renaming one of those breaks
that import on purpose.
