"""Build a *pseudolocalized* translation memory — **development scaffolding**.

Not a pipeline component and never run in CI's real path: this writes machine
gibberish into `l10n/tm/<lang>.json`, which is a committed artifact. Point it
at a scratch directory.

What it is for: exercising reassembly end-to-end without spending a single API
call. Pseudolocalization is the standard localization-QA trick — replace the
prose with text that *looks* like the target language while keeping every
non-translatable span byte-identical — and it stresses exactly what the
renderer can get wrong:

- Hebrew output is right-to-left, so RTL/LTR mixing inside tables, nested
  lists and inline code is exercised for real;
- the text is longer than the source, so table column widths and wrapping move;
- placeholders (code spans, link targets, image sources) are preserved
  verbatim, so the placeholder gate should pass on every unit — and a run that
  reports violations is reporting a bug in the renderer, not in the data.

    venv/bin/python3 cl10n/pseudo_tm.py --langs he,ru --tm-dir /tmp/tm
    venv/bin/python3 cl10n/reassemble.py --langs he,ru --tm-dir /tmp/tm --out-dir /tmp/locales

For a translation memory with real meaning, run `cl10n/queue_runner.py`.
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [_HERE, os.path.join(os.path.dirname(_HERE), "app")]

from markdown_it.tree import SyntaxTreeNode  # noqa: E402

import tree_diff  # noqa: E402
from l10n_store import TranslationMemory  # noqa: E402
from utils import ast_to_markdown, markdown_to_ast  # noqa: E402

MODEL = "pseudo/localizer"
PROMPT_VERSION = "pseudo"

# Every inline construct whose *source* must survive: code spans, links,
# images, raw HTML, emphasis markers, entity escapes. Anything matched here is
# copied through untouched; everything between matches gets transliterated.
_PROTECTED = re.compile(
    r"(`[^`]*`"          # code span
    r"|!?\[[^\]]*\]\([^)]*\)"  # link / image
    r"|<[^>]+>"          # raw inline html, autolink
    r"|\\.|"             # escape
    r"[*_~]+)"           # emphasis / strikethrough markers
)

# 26 letters of the target script per 26 ASCII letters. Hebrew is caseless, so
# both cases map to the same glyph — the mapping is lossy on purpose: it only
# has to *look* like the target language and be unmistakably not English.
ALPHABETS = {
    "he": "אבגדהוזחטיכלמנסעפצקרשתםןףץ",
    "ru": "абвгдежзийклмнопрстуфхцчшщ",
}
# Target languages run longer than English; a suffix per segment moves the
# rendered table widths, which is where a length change can break something.
SUFFIXES = {"he": "ים", "ru": "ый"}

_ASCII = "abcdefghijklmnopqrstuvwxyz"


def _table(lang: str):
    alphabet = ALPHABETS.get(lang)
    if alphabet is None:
        raise SystemExit(f"no pseudolocalization alphabet for {lang!r} "
                         f"(have {', '.join(sorted(ALPHABETS))})")
    assert len(alphabet) == 26, f"{lang}: {len(alphabet)} letters, want 26"
    return str.maketrans(_ASCII + _ASCII.upper(), alphabet * 2)


def pseudo(text: str, lang: str) -> str:
    """Transliterate the translatable parts of `text`, protect the rest."""
    table = _table(lang)
    out = []
    for i, chunk in enumerate(_PROTECTED.split(text)):
        out.append(chunk if i % 2 else chunk.translate(table))
    return "".join(out)


def top_up(source: str, translation: str, placeholders: list[str]) -> str:
    """Restore placeholder occurrences the transliteration ate.

    Worth understanding, because it is a property of the gate rather than of
    this script: `tree_diff._placeholders` yields a code span's *content*, so
    the placeholder for `` `create` `` is the bare word `create` — and the
    "at least as many times" rule then counts the times that same word appears
    as ordinary prose in the same segment. `pseudo` protects the code span and
    transliterates the prose, so the count drops and the gate fires.

    A real translation never hits this: the runner enforces the identical rule
    before the entry reaches the TM, so anything in the memory already passes.
    Here it would just fill the run with violations nobody should act on.
    """
    for ph in placeholders:
        deficit = source.count(ph) - translation.count(ph)
        if deficit > 0:
            translation += (" " + ph) * deficit
    return translation


def build(paths: list[str], langs: list[str], tm_dir: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for lang in langs:
        tm = TranslationMemory.load(tm_dir, lang)
        for path in paths:
            with open(path, "r", encoding="utf-8") as fh:
                canonical = ast_to_markdown(markdown_to_ast(fh.read()))
            root = SyntaxTreeNode(markdown_to_ast(canonical))
            tree_diff.hash_tree(root)
            for unit in tree_diff._units_under(root):
                source = tree_diff._unit_source(unit)
                translation = pseudo(source, lang) + SUFFIXES.get(lang, "")
                tm.upsert(
                    unit.h,
                    source=source,
                    translation=top_up(source, translation,
                                       tree_diff._placeholders(unit)),
                    model=MODEL,
                    prompt_version=PROMPT_VERSION,
                    action="TRANSLATE",
                )
        tm.save()
        counts[lang] = len(tm.entries)
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("sources", nargs="*", help="markdown files (default: md/**/*.md)")
    parser.add_argument("--langs", default="he,ru")
    parser.add_argument("--tm-dir", default="l10n/tm")
    args = parser.parse_args(argv)

    paths = args.sources or sorted(glob.glob(os.path.join("md", "**", "*.md"), recursive=True))
    if not paths:
        print("no source markdown found under md/", file=sys.stderr)
        return 2

    langs = [lang.strip() for lang in args.langs.split(",") if lang.strip()]
    counts = build(paths, langs, args.tm_dir)
    for lang, n in counts.items():
        print(f"{n} pseudo entries -> {os.path.join(args.tm_dir, lang + '.json')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
