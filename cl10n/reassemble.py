"""Reassembly and rendering — steps 10-11 of `.claude/rules/l10n-pipeline-spec.md`.

Turns a source Markdown document plus a populated translation memory into the
target-language file under `locales/<lang>/`.

**Structure comes from the source tree and is never re-derived from translated
text.** The document is canonicalised, parsed once, and then the *only* thing
that changes is the inline content of each translation unit: for every
`heading` / `paragraph` / `th` / `td` node, the `inline` token's children are
replaced with the tokenisation of its translation. Heading levels, list
nesting, table shape, block quotes and code fences are therefore preserved by
construction rather than by care — there is no code path that could emit a
different block structure, which is what makes this component trustworthy in
a language nobody on the team reads.

That is also why the splice re-parses with `utils.parse_inline` rather than
block-parsing the translated string: a translation that happens to begin with
`- ` or `1. ` stays one paragraph instead of silently becoming a list.

    +-------------------+   canonicalise    +--------------+
    |  md/<path>.md     | ----------------> | token stream | -- units --> TM
    +-------------------+                   +--------------+      |
                                                   |              | lookup
                                       splice <----+--------------+
                                                   |
                                                   v
                          ast_to_markdown -> locales/<lang>/<path>.md

Three things can happen to a unit, and none of them is silent:

| TM state for the unit | rendered as | reported as |
| --- | --- | --- |
| entry present, placeholders intact | the translation | `translated` |
| no entry (or `rejected` job, spec §5) | the English source | `untranslated` |
| entry present, a placeholder lost | the English source | `placeholder_lost` |

Opaque blocks (`fence`, `code_block`, `html_block`, `front_matter`, `hr`) are
never looked up and never touched — `tree_diff`'s COPY action, realised as the
absence of any splice. A removed opaque block disappears by construction
because the target is rebuilt from the source tree.

**Not here** (spec §6, and `cl10n-runner-spec.md` §8): the manifest. This
module *reports* the fallback hashes a run produced; writing them into
`l10n/manifest.json` belongs to the orchestrator.

    venv/bin/python3 cl10n/reassemble.py --langs he,ru
    venv/bin/python3 cl10n/reassemble.py --langs he md/skills/_shared/project-config.md
    venv/bin/python3 cl10n/reassemble.py --langs he,ru --dry-run
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from dataclasses import dataclass, field

_HERE = os.path.dirname(os.path.abspath(__file__))
# This package plus `app/`, which owns the parser (`utils`) and the segmenter
# (`tree_diff`). Bare scripts rather than an installed package is the repo's
# existing convention — see `cl10n/queue_runner.py`.
sys.path[:0] = [_HERE, os.path.join(os.path.dirname(_HERE), "app")]

from markdown_it.tree import SyntaxTreeNode  # noqa: E402

import tree_diff  # noqa: E402  (app/, path fixed up above)
from l10n_store import TranslationMemory, atomic_write_text  # noqa: E402
from placeholders import describe as describe_lost  # noqa: E402
from placeholders import lost_placeholders  # noqa: E402
from utils import ast_to_markdown, markdown_to_ast, parse_inline  # noqa: E402

# Reassembly is a *consumer* of tree_diff's segmentation, not a second
# implementation of it: the unit set, each unit's source string and its
# placeholder list must be exactly what the planner hashed and what the runner
# translated, or every TM lookup misses. Aliasing the private names here is
# deliberate — a rename in tree_diff then breaks this import loudly instead of
# letting the two definitions of "translation unit" drift apart quietly.
_units_under = tree_diff._units_under
_unit_source = tree_diff._unit_source
_placeholders = tree_diff._placeholders
_opaque_under = tree_diff._opaque_under

DEFAULT_TM_DIR = "l10n/tm"
DEFAULT_MD_ROOT = "md"
DEFAULT_LOCALES_ROOT = "locales"

# Fallback reasons — why a unit rendered as English.
UNTRANSLATED = "untranslated"
EMPTY_TRANSLATION = "empty_translation"
PLACEHOLDER_LOST = "placeholder_lost"

# Node types whose Markdown syntax is single-line: a newline spliced into one
# ends the row and grows the table by a phantom line (verified — it is the one
# structural corruption the splice can cause on its own). mdformat already
# collapses newlines inside a `heading`, and a `paragraph` may legitimately
# keep its soft breaks, so only cells need this.
SINGLE_LINE_TYPES = {"th", "td"}

_WS = re.compile(r"\s+")


class StructureMismatch(RuntimeError):
    """The rendered document's block structure differs from the source's.

    Raised by the post-render verification (AC2), which exists because this is
    the one failure this component could otherwise ship invisibly. Never
    expected in practice: the caller should treat it as a bug here or a
    pathological translation, not write the file, and report it.
    """


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------


@dataclass
class Fallback:
    """One unit occurrence that rendered as English, and why."""

    unit_hash: str
    node_type: str
    reason: str  # untranslated | empty_translation | placeholder_lost
    detail: str = ""

    def as_dict(self) -> dict:
        return {
            "unit_hash": self.unit_hash,
            "node_type": self.node_type,
            "reason": self.reason,
            "detail": self.detail,
        }


@dataclass
class RenderReport:
    """What one (document, language) render did — AC4 and AC5's reporting half."""

    lang: str
    source: str = ""
    target: str = ""
    units: int = 0  # unit occurrences in the document, not distinct hashes
    translated: int = 0
    opaque: int = 0  # blocks carried through verbatim (tree_diff's COPY)
    fallbacks: list[Fallback] = field(default_factory=list)

    @property
    def fallback_hashes(self) -> list[str]:
        """Distinct unit hashes rendered as English — the manifest's
        `localized.<lang>.fallbacks` (spec §6), which is per hash, not per
        occurrence."""
        return sorted({f.unit_hash for f in self.fallbacks})

    def of_reason(self, reason: str) -> list[Fallback]:
        return [f for f in self.fallbacks if f.reason == reason]

    @property
    def violations(self) -> list[Fallback]:
        """Placeholder-gate rejections — the ones that mean a *broken*
        translation exists in the TM, not merely a missing one."""
        return self.of_reason(PLACEHOLDER_LOST)

    def as_dict(self) -> dict:
        return {
            "lang": self.lang,
            "source": self.source,
            "target": self.target,
            "units": self.units,
            "translated": self.translated,
            "opaque": self.opaque,
            "fallback_hashes": self.fallback_hashes,
            "fallbacks": [f.as_dict() for f in self.fallbacks],
        }

    def as_text(self) -> str:
        parts = [
            f"{self.target or self.source} [{self.lang}]: "
            f"{self.translated}/{self.units} units translated"
        ]
        if self.opaque:
            parts.append(f"{self.opaque} opaque block(s) verbatim")
        missing = len(self.of_reason(UNTRANSLATED)) + len(self.of_reason(EMPTY_TRANSLATION))
        if missing:
            parts.append(f"{missing} English fallback(s)")
        if self.violations:
            parts.append(f"{len(self.violations)} PLACEHOLDER VIOLATION(S)")
        return ", ".join(parts)


# --------------------------------------------------------------------------
# Canonicalisation and structural verification
# --------------------------------------------------------------------------


def canonicalise(md: str) -> str:
    """The mdformat round-trip every hash in this pipeline is taken over.

    Rendering starts here so the unit hashes computed below are the same ones
    the planner enqueued and the runner keyed the TM by (spec §1 step 1), and
    so AC1's "empty TM reproduces the source byte for byte" is a statement
    about a stable form rather than about whatever the file happened to look
    like.
    """
    return ast_to_markdown(markdown_to_ast(md))


def block_signature(md: str) -> tuple:
    """Nested `(type, ...)` tuple of the document's *block* structure.

    Stops at `inline`: what is inside a translation unit is allowed to differ
    (that is the point — a placeholder may sit in a different clause in
    Hebrew), what contains it is not. This is AC2's "compare node types and
    nesting rather than by eye", and it is cheap enough to run on every render.
    """

    def walk(node) -> tuple:
        if node.type == "inline":
            return ("inline",)
        head = node.type if node.type == "root" else f"{node.type}:{tree_diff.attr(node, 'tag')}"
        return (head,) + tuple(walk(c) for c in node.children)

    return walk(SyntaxTreeNode(markdown_to_ast(md)))


def _first_difference(a: tuple, b: tuple, path: str = "") -> str:
    """A human-readable "where" for a structure mismatch."""
    if a == b:
        return ""
    if not isinstance(a, tuple) or not isinstance(b, tuple) or a[:1] != b[:1]:
        return f"{path or '<root>'}: source {a!r} vs rendered {b!r}"
    here = f"{path}/{a[0]}"
    for i in range(max(len(a), len(b))):
        if i >= len(a):
            return f"{here}: rendered has an extra child {b[i]!r}"
        if i >= len(b):
            return f"{here}: rendered is missing child {a[i]!r}"
        diff = _first_difference(a[i], b[i], here)
        if diff:
            return diff
    return f"{here}: differs"


# --------------------------------------------------------------------------
# The splice
# --------------------------------------------------------------------------


def _inline_child(unit):
    for child in unit.children:
        if child.type == "inline":
            return child
    return None


def _resolve(unit, entries) -> tuple[str | None, Fallback | None]:
    """The text to splice into `unit`, or the reason it stays English.

    The placeholder gate runs here, against the *node's* source rather than
    the TM entry's copy of it: the node is what is being rendered. The two
    agree by construction (the entry's key is the hash of that text), and
    trusting the node keeps a hand-edited `source` field from waving a broken
    translation through.
    """
    unit_hash = unit.h
    entry = entries.get(unit_hash)
    if entry is None:
        return None, Fallback(unit_hash, unit.type, UNTRANSLATED)

    translation = entry.get("translation") or ""
    if not translation.strip():
        # An entry with no text is not a translation. Rendering it would blank
        # a paragraph — worse than leaving English, which is at least readable.
        return None, Fallback(unit_hash, unit.type, EMPTY_TRANSLATION)

    source = _unit_source(unit)
    lost = lost_placeholders(source, translation, _placeholders(unit))
    if lost:
        return None, Fallback(
            unit_hash,
            unit.type,
            PLACEHOLDER_LOST,
            describe_lost(source, translation, lost),
        )
    return translation, None


def _fit(node_type: str, text: str) -> str:
    """Collapse whitespace where the block's syntax cannot survive a newline."""
    if node_type in SINGLE_LINE_TYPES:
        return _WS.sub(" ", text).strip()
    return text


def splice(tokens, entries, report: RenderReport) -> None:
    """Replace every translation unit's inline content in place.

    Mutates the `inline` tokens of `tokens` — `SyntaxTreeNode` wraps the very
    same `Token` objects, so rendering the original list afterwards picks the
    replacements up. Units with no usable translation are left exactly as they
    are, which *is* the English fallback (AC4): no branch has to reconstruct
    the source text, so it cannot reconstruct it wrongly.
    """
    root = SyntaxTreeNode(tokens)
    tree_diff.hash_tree(root)

    report.opaque = sum(1 for _ in _opaque_under(root))
    for unit in _units_under(root):
        inline = _inline_child(unit)
        if inline is None:
            # A `heading`/`td` with no inline child (an empty table cell) has
            # nothing to translate and no hash collision to worry about.
            continue
        report.units += 1
        translation, fallback = _resolve(unit, entries)
        if fallback is not None:
            report.fallbacks.append(fallback)
            continue
        text = _fit(unit.type, translation)
        inline.token.children = parse_inline(text)
        inline.token.content = text
        report.translated += 1


# --------------------------------------------------------------------------
# Render
# --------------------------------------------------------------------------


def render_markdown(
    source_md: str,
    entries,
    *,
    lang: str,
    source: str = "",
    verify: bool = True,
) -> tuple[str, RenderReport]:
    """Render one document into `lang`. Returns the Markdown and the report.

    `entries` is a `{unit_hash: entry}` mapping — `TranslationMemory.entries`,
    or any dict shaped like it. An empty one is the identity case (AC1): every
    unit falls back, nothing is spliced, and the output is the canonicalised
    source.
    """
    canonical = canonicalise(source_md)
    tokens = markdown_to_ast(canonical)
    report = RenderReport(lang=lang, source=source)
    splice(tokens, entries, report)
    rendered = ast_to_markdown(tokens)

    if verify:
        want = block_signature(canonical)
        got = block_signature(rendered)
        if want != got:
            raise StructureMismatch(
                f"{source or '<string>'} [{lang}]: rendered block structure "
                f"differs from the source — {_first_difference(want, got)}"
            )
    return rendered, report


def locale_path(
    src_path: str,
    lang: str,
    md_root: str = DEFAULT_MD_ROOT,
    locales_root: str = DEFAULT_LOCALES_ROOT,
) -> str:
    """`md/skills/x/SKILL.md` -> `locales/he/skills/x/SKILL.md` (spec §1).

    A path outside `md_root` keeps its full shape under the locale directory
    rather than being rejected: mirroring something is always better than
    dropping it, and the CLI's default glob only ever produces paths inside.
    """
    rel = os.path.relpath(src_path, md_root)
    if rel.startswith(os.pardir + os.sep) or rel == os.pardir:
        rel = os.path.normpath(src_path).lstrip(os.sep)
    return os.path.join(locales_root, lang, rel)


def render_file(
    src_path: str,
    entries,
    out_path: str,
    *,
    lang: str,
    write: bool = True,
    verify: bool = True,
) -> RenderReport:
    """Render `src_path` into `out_path`. `write=False` renders and reports only."""
    with open(src_path, "r", encoding="utf-8") as fh:
        source_md = fh.read()
    rendered, report = render_markdown(
        source_md, entries, lang=lang, source=src_path, verify=verify
    )
    report.target = out_path
    if write:
        atomic_write_text(out_path, rendered)
    return report


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Render locales/<lang>/ from the source corpus and the translation memory."
    )
    parser.add_argument("sources", nargs="*", help="markdown files (default: <md-root>/**/*.md)")
    parser.add_argument("--langs", default="he,ru", help="comma-separated target languages")
    parser.add_argument("--tm-dir", default=DEFAULT_TM_DIR)
    parser.add_argument("--md-root", default=DEFAULT_MD_ROOT)
    parser.add_argument("--out-dir", default=DEFAULT_LOCALES_ROOT)
    parser.add_argument("--report", default=None, help="write the per-file JSON report here")
    parser.add_argument("-n", "--dry-run", action="store_true",
                        help="render and report, write nothing")
    parser.add_argument("--fail-on-fallback", action="store_true",
                        help="exit non-zero if any unit rendered as English (CI gate)")
    parser.add_argument("--no-verify", dest="verify", action="store_false",
                        help="skip the block-structure check (do not)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    paths = args.sources or sorted(
        glob.glob(os.path.join(args.md_root, "**", "*.md"), recursive=True)
    )
    if not paths:
        print(f"no source markdown found under {args.md_root}", file=sys.stderr)
        return 2

    langs = [lang.strip() for lang in args.langs.split(",") if lang.strip()]
    reports: list[RenderReport] = []
    broken = 0

    for lang in langs:
        tm = TranslationMemory.load(args.tm_dir, lang)
        for path in paths:
            out = locale_path(path, lang, args.md_root, args.out_dir)
            try:
                report = render_file(
                    path, tm.entries, out, lang=lang,
                    write=not args.dry_run, verify=args.verify,
                )
            except StructureMismatch as exc:
                # Not written: a structurally corrupt file is the one output
                # worse than no output. The rest of the corpus still renders.
                broken += 1
                print(f"STRUCTURE MISMATCH {exc}", file=sys.stderr)
                continue
            reports.append(report)
            print(report.as_text())
            for violation in report.violations:
                print(f"  placeholder violation #{violation.unit_hash}: {violation.detail}",
                      file=sys.stderr)

    fallbacks = sum(len(r.fallbacks) for r in reports)
    violations = sum(len(r.violations) for r in reports)
    print(f"\n{len(reports)} file(s) rendered across {len(langs)} language(s); "
          f"{fallbacks} English fallback(s), {violations} placeholder violation(s)"
          + (f", {broken} NOT WRITTEN (structure mismatch)" if broken else "")
          + (" [dry run — nothing written]" if args.dry_run else ""))

    if args.report and not args.dry_run:
        atomic_write_text(
            args.report,
            json.dumps({"files": [r.as_dict() for r in reports]},
                       ensure_ascii=False, indent=2) + "\n",
        )

    if broken or violations:
        return 1
    if args.fail_on_fallback and fallbacks:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
