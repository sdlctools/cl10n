"""Reassembly and rendering — the acceptance criteria of JST-264, as tests.

Fixtures are the **real corpus** (`md/skills/**`), not toy documents: the
failure this component exists to prevent is structural corruption that only
shows up on a document with tables, nested lists, fences, front matter and
inline code all in one file, and the corpus has exactly that. Small synthetic
documents appear only where a hazard has to be constructed deliberately (a
newline inside a table cell, a translation that drops a placeholder).

Nothing here contacts a provider or needs a key: the "translations" come from
`cl10n/pseudo_tm.py`, which transliterates the source while preserving every
non-translatable span, or are written by hand in real Hebrew.
"""

from __future__ import annotations

import collections
import json
import os

import pytest
from markdown_it.tree import SyntaxTreeNode

import pseudo_tm
import reassemble
import tree_diff
from l10n_store import TranslationMemory
from utils import markdown_to_ast

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CORPUS = [
    os.path.join(REPO, "md", "skills", "_shared", "jira-api-reference.md"),
    os.path.join(REPO, "md", "skills", "_shared", "project-config.md"),
    os.path.join(REPO, "md", "skills", "jira-task-assigner", "SKILL.md"),
]
LANGS = ("he", "ru")


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture(scope="session")
def canonical():
    """`{path: canonicalised source}` — parsed once for the whole session."""
    out = {}
    for path in CORPUS:
        with open(path, encoding="utf-8") as fh:
            out[path] = reassemble.canonicalise(fh.read())
    return out


@pytest.fixture(scope="session")
def pseudo_entries(canonical):
    """A fully populated TM per language, built without an API call."""
    memories = {lang: {} for lang in LANGS}
    for md in canonical.values():
        root = SyntaxTreeNode(markdown_to_ast(md))
        tree_diff.hash_tree(root)
        for unit in tree_diff._units_under(root):
            source = tree_diff._unit_source(unit)
            for lang in LANGS:
                translation = pseudo_tm.pseudo(source, lang) + pseudo_tm.SUFFIXES[lang]
                memories[lang][unit.h] = {
                    "source": source,
                    "translation": pseudo_tm.top_up(
                        source, translation, tree_diff._placeholders(unit)
                    ),
                    "model": pseudo_tm.MODEL,
                    "prompt_version": pseudo_tm.PROMPT_VERSION,
                    "translated_at": "2026-08-04T12:00:00Z",
                    "review_status": "machine",
                    "action": "TRANSLATE",
                }
    return memories


def entry(source: str, translation: str, **overrides) -> dict:
    """A schema-shaped TM entry."""
    return {
        "source": source,
        "translation": translation,
        "model": "stub/model",
        "prompt_version": "v1",
        "translated_at": "2026-08-04T12:00:00Z",
        "review_status": "machine",
        "action": "TRANSLATE",
        **overrides,
    }


def units_of(md: str) -> dict:
    """`{unit_hash: source}` for a document — how a test names a unit."""
    return tree_diff.tm_keys(reassemble.canonicalise(md))


def hash_of(md: str, needle: str) -> str:
    """The hash of the one unit whose source contains `needle`."""
    matches = [h for h, src in units_of(md).items() if needle in src]
    assert len(matches) == 1, f"{needle!r} matched {len(matches)} units"
    return matches[0]


def inventory(md: str) -> collections.Counter:
    """Every span AC3 says must survive byte-for-byte.

    Counted rather than set-compared: dropping one of two identical fences is
    exactly the kind of loss a set would hide.
    """
    out: collections.Counter = collections.Counter()
    stack = list(SyntaxTreeNode(markdown_to_ast(md)).children)
    while stack:
        node = stack.pop()
        if node.type in ("fence", "code_block"):
            out[("fence", node.info or "", node.content)] += 1
        elif node.type == "code_inline":
            out[("code_inline", node.content)] += 1
        elif node.type == "link":
            out[("href", node.attrs.get("href", ""))] += 1
        elif node.type == "image":
            out[("src", node.attrs.get("src", ""))] += 1
        elif node.type in ("html_block", "html_inline", "front_matter"):
            out[(node.type, node.content)] += 1
        stack.extend(node.children)
    return out


# --------------------------------------------------------------------------
# AC1 — the round-trip identity test. Nothing else is trusted until this holds.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path", CORPUS, ids=os.path.basename)
def test_empty_tm_reproduces_the_canonical_source_byte_for_byte(path, canonical):
    rendered, report = reassemble.render_markdown(canonical[path], {}, lang="he")
    assert rendered == canonical[path]
    assert report.translated == 0
    assert len(report.fallbacks) == report.units


@pytest.mark.parametrize("path", CORPUS, ids=os.path.basename)
def test_canonicalisation_is_idempotent(path, canonical):
    """AC1 depends on it: re-canonicalising must be a fixed point, or the
    identity render would drift a little further on every pipeline run."""
    assert reassemble.canonicalise(canonical[path]) == canonical[path]


def test_empty_tm_render_is_the_english_source_not_a_placeholder_token():
    md = "# Title\n\nA paragraph with `code`.\n"
    rendered, _ = reassemble.render_markdown(md, {}, lang="he")
    assert "A paragraph with `code`." in rendered
    assert "Title" in rendered


# --------------------------------------------------------------------------
# AC2 — structure is the source's, verified by node types and nesting
# --------------------------------------------------------------------------


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("path", CORPUS, ids=os.path.basename)
def test_full_translation_preserves_block_structure(path, lang, canonical, pseudo_entries):
    rendered, report = reassemble.render_markdown(
        canonical[path], pseudo_entries[lang], lang=lang
    )
    assert report.fallbacks == []
    assert report.translated == report.units
    assert reassemble.block_signature(rendered) == reassemble.block_signature(canonical[path])


def test_block_signature_distinguishes_heading_levels():
    """The guard has to be able to fail, or it guards nothing."""
    assert reassemble.block_signature("# a\n") != reassemble.block_signature("## a\n")
    assert reassemble.block_signature("- a\n") != reassemble.block_signature("1. a\n")


def test_structure_mismatch_is_raised_rather_than_written():
    """With the single-line handling disabled, a newline in a table cell splits
    the row and grows the table — the corruption the verify pass exists for."""
    md = "| a | b |\n| --- | --- |\n| c | d |\n"
    entries = {hash_of(md, "c"): entry("c", "line one\nline two")}
    original = reassemble.SINGLE_LINE_TYPES
    reassemble.SINGLE_LINE_TYPES = set()
    try:
        with pytest.raises(reassemble.StructureMismatch) as excinfo:
            reassemble.render_markdown(md, entries, lang="he")
    finally:
        reassemble.SINGLE_LINE_TYPES = original
    assert "table" in str(excinfo.value)


def test_table_cell_translation_with_a_newline_is_collapsed():
    """And with it enabled, the same translation renders as one intact row."""
    md = "| a | b |\n| --- | --- |\n| c | d |\n"
    entries = {hash_of(md, "c"): entry("c", "line one\nline two")}
    rendered, report = reassemble.render_markdown(md, entries, lang="he")
    assert "| line one line two | d |" in rendered
    assert reassemble.block_signature(rendered) == reassemble.block_signature(
        reassemble.canonicalise(md)
    )
    assert report.translated == 1  # the other three cells have no entry
    assert report.violations == []


def test_translation_that_looks_like_a_list_stays_a_paragraph():
    """A translated segment is inline content; block syntax in it is text."""
    md = "Some prose here.\n"
    entries = {hash_of(md, "prose"): entry("Some prose here.", "- not a list\n1. nor this")}
    rendered, _ = reassemble.render_markdown(md, entries, lang="he")
    assert reassemble.block_signature(rendered) == reassemble.block_signature(md)
    assert "\\- not a list" in rendered


def test_nested_list_and_table_survive_real_hebrew():
    md = (
        "# Heading\n\n"
        "- outer item\n"
        "  - inner item with `code`\n\n"
        "| head | second |\n| --- | --- |\n| cell | other |\n"
    )
    entries = {
        hash_of(md, "Heading"): entry("Heading", "כותרת"),
        hash_of(md, "outer"): entry("outer item", "פריט חיצוני"),
        hash_of(md, "inner"): entry("inner item with `code`", "פריט פנימי עם `code`"),
        hash_of(md, "head"): entry("head", "כותרת"),
        hash_of(md, "second"): entry("second", "שנייה"),
        hash_of(md, "cell"): entry("cell", "תא"),
        hash_of(md, "other"): entry("other", "אחר"),
    }
    rendered, report = reassemble.render_markdown(md, entries, lang="he")
    assert report.fallbacks == []
    assert reassemble.block_signature(rendered) == reassemble.block_signature(
        reassemble.canonicalise(md)
    )
    assert "# כותרת" in rendered
    assert "  - פריט פנימי עם `code`" in rendered
    assert "| תא | אחר |" in rendered


# --------------------------------------------------------------------------
# AC3 — code fences, inline code, link targets and image sources are verbatim
# --------------------------------------------------------------------------


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("path", CORPUS, ids=os.path.basename)
def test_opaque_and_inline_spans_are_byte_identical(path, lang, canonical, pseudo_entries):
    rendered, _ = reassemble.render_markdown(canonical[path], pseudo_entries[lang], lang=lang)
    assert inventory(rendered) == inventory(canonical[path])


def test_opaque_blocks_are_never_looked_up_in_the_memory():
    """A fence's Merkle hash is a legal TM key by shape; reassembly must still
    never consult it. `COPY` means "emit verbatim", not "translate"."""
    md = "Prose.\n\n```python\nprint(\"keep me\")\n```\n"
    root = SyntaxTreeNode(markdown_to_ast(reassemble.canonicalise(md)))
    tree_diff.hash_tree(root)
    (fence,) = list(tree_diff._opaque_under(root))
    entries = {fence.h: entry(fence.content, "print(\"ruined\")")}

    rendered, report = reassemble.render_markdown(md, entries, lang="he")
    assert 'print("keep me")' in rendered
    assert "ruined" not in rendered
    assert report.opaque == 1


def test_front_matter_is_carried_through_verbatim():
    md = "---\ntitle: keep me\n---\n\nProse.\n"
    entries = {hash_of(md, "Prose"): entry("Prose.", "פרוזה.")}
    rendered, report = reassemble.render_markdown(md, entries, lang="he")
    assert rendered.startswith("---\ntitle: keep me\n---")
    assert report.opaque == 1


def test_link_target_survives_a_rewritten_link_text():
    md = "See [the docs](https://example.test/a?b=c#d) now.\n"
    src = "See [the docs](https://example.test/a?b=c#d) now."
    entries = {hash_of(md, "docs"): entry(src, "ראה [את התיעוד](https://example.test/a?b=c#d) עכשיו.")}
    rendered, report = reassemble.render_markdown(md, entries, lang="he")
    assert "https://example.test/a?b=c#d" in rendered
    assert report.fallbacks == []


# --------------------------------------------------------------------------
# AC4 — a unit missing from the memory renders as English, and is counted
# --------------------------------------------------------------------------


def test_missing_unit_renders_english_and_is_reported():
    md = "First paragraph.\n\nSecond paragraph.\n"
    entries = {hash_of(md, "First"): entry("First paragraph.", "פסקה ראשונה.")}
    rendered, report = reassemble.render_markdown(md, entries, lang="he")

    assert "פסקה ראשונה." in rendered
    assert "Second paragraph." in rendered
    assert report.units == 2
    assert report.translated == 1
    assert [f.reason for f in report.fallbacks] == [reassemble.UNTRANSLATED]
    assert report.fallbacks[0].unit_hash == hash_of(md, "Second")


def test_empty_translation_falls_back_rather_than_blanking_the_paragraph():
    md = "Only paragraph.\n"
    entries = {hash_of(md, "Only"): entry("Only paragraph.", "   ")}
    rendered, report = reassemble.render_markdown(md, entries, lang="he")
    assert "Only paragraph." in rendered
    assert [f.reason for f in report.fallbacks] == [reassemble.EMPTY_TRANSLATION]


@pytest.mark.parametrize("path", CORPUS, ids=os.path.basename)
def test_partial_memory_counts_add_up(path, canonical, pseudo_entries):
    """Half the units translated: the report's arithmetic has to be exact,
    because "localized with N fallbacks" is what CI reports off it."""
    full = pseudo_entries["he"]
    half = {h: e for i, (h, e) in enumerate(sorted(full.items())) if i % 2 == 0}
    rendered, report = reassemble.render_markdown(canonical[path], half, lang="he")

    assert report.translated + len(report.fallbacks) == report.units
    assert 0 < report.translated < report.units
    assert reassemble.block_signature(rendered) == reassemble.block_signature(canonical[path])


def test_fallback_hashes_are_distinct_and_sorted():
    """The manifest's `fallbacks` is per hash, not per occurrence (spec §6)."""
    md = "Repeated line.\n\nRepeated line.\n\nOther line.\n"
    _, report = reassemble.render_markdown(md, {}, lang="he")
    assert report.units == 3
    assert len(report.fallbacks) == 3
    assert report.fallback_hashes == sorted(set(report.fallback_hashes))
    assert len(report.fallback_hashes) == 2


def test_repeated_unit_is_spliced_at_every_occurrence():
    md = "Repeated line.\n\nRepeated line.\n"
    entries = {hash_of(md, "Repeated"): entry("Repeated line.", "שורה חוזרת.")}
    rendered, report = reassemble.render_markdown(md, entries, lang="he")
    assert rendered.count("שורה חוזרת.") == 2
    assert report.translated == 2


# --------------------------------------------------------------------------
# AC5 — a translation that lost a placeholder is caught, reported, not shipped
# --------------------------------------------------------------------------


def test_translation_that_dropped_a_code_span_falls_back_to_english():
    md = "Run `git status` before you push.\n"
    src = "Run `git status` before you push."
    entries = {hash_of(md, "git status"): entry(src, "הרץ לפני הדחיפה.")}
    rendered, report = reassemble.render_markdown(md, entries, lang="he")

    assert rendered.strip() == src
    assert "הרץ" not in rendered
    (violation,) = report.violations
    assert violation.reason == reassemble.PLACEHOLDER_LOST
    assert "git status" in violation.detail
    assert violation.unit_hash in report.fallback_hashes


def test_translation_that_dropped_a_link_target_falls_back_to_english():
    md = "Open [the page](https://example.test/x).\n"
    src = "Open [the page](https://example.test/x)."
    entries = {hash_of(md, "the page"): entry(src, "פתח [את הדף](https://evil.test/x).")}
    rendered, report = reassemble.render_markdown(md, entries, lang="he")

    assert "https://evil.test/x" not in rendered
    assert "https://example.test/x" in rendered
    assert len(report.violations) == 1


def test_translation_that_dropped_an_image_source_falls_back_to_english():
    md = "Look ![a diagram](img/flow.png) here.\n"
    src = "Look ![a diagram](img/flow.png) here."
    entries = {hash_of(md, "diagram"): entry(src, "הבט כאן.")}
    rendered, report = reassemble.render_markdown(md, entries, lang="he")

    assert "img/flow.png" in rendered
    assert [f.reason for f in report.violations] == [reassemble.PLACEHOLDER_LOST]


def test_gate_counts_occurrences_not_mere_presence():
    """Same rule the runner applies — one of two identical spans is a loss."""
    md = "Use `x` and then `x` again.\n"
    src = "Use `x` and then `x` again."
    entries = {hash_of(md, "then"): entry(src, "השתמש ב-`x` שוב.")}
    _, report = reassemble.render_markdown(md, entries, lang="he")
    assert len(report.violations) == 1


def test_a_translation_that_repeats_a_placeholder_passes():
    md = "Use `x` here.\n"
    src = "Use `x` here."
    entries = {hash_of(md, "Use"): entry(src, "השתמש ב-`x` וגם ב-`x` כאן.")}
    rendered, report = reassemble.render_markdown(md, entries, lang="he")
    assert report.violations == []
    assert "השתמש" in rendered


def test_review_status_does_not_gate_rendering():
    """`recheck` keeps its translation (spec §2); `approved` is human-reviewed.
    Neither is a reason to fall back to English."""
    md = "A line.\n"
    for status in ("machine", "recheck", "approved"):
        entries = {hash_of(md, "line"): entry("A line.", "שורה.", review_status=status)}
        rendered, report = reassemble.render_markdown(md, entries, lang="he")
        assert report.translated == 1, status
        assert "שורה." in rendered


# --------------------------------------------------------------------------
# AC6 — the whole corpus renders for both languages, through the CLI
# --------------------------------------------------------------------------


def test_cli_renders_the_whole_corpus_for_both_languages(tmp_path, capsys):
    tm_dir = str(tmp_path / "tm")
    out_dir = str(tmp_path / "locales")
    assert pseudo_tm.main(CORPUS + ["--langs", "he,ru", "--tm-dir", tm_dir]) == 0

    rc = reassemble.main(
        CORPUS
        + ["--langs", "he,ru", "--tm-dir", tm_dir, "--out-dir", out_dir,
           "--md-root", os.path.join(REPO, "md"),
           "--report", str(tmp_path / "render.json"),
           "--fail-on-fallback"]
    )
    assert rc == 0, capsys.readouterr().err

    for lang in LANGS:
        for path in CORPUS:
            rel = os.path.relpath(path, os.path.join(REPO, "md"))
            written = os.path.join(out_dir, lang, rel)
            assert os.path.exists(written), written
            with open(written, encoding="utf-8") as fh:
                rendered = fh.read()
            with open(path, encoding="utf-8") as fh:
                source = reassemble.canonicalise(fh.read())
            assert reassemble.block_signature(rendered) == reassemble.block_signature(source)
            assert inventory(rendered) == inventory(source)

    with open(tmp_path / "render.json", encoding="utf-8") as fh:
        report = json.load(fh)
    assert len(report["files"]) == 6
    assert all(f["fallback_hashes"] == [] for f in report["files"])


def test_cli_dry_run_writes_nothing(tmp_path):
    out_dir = str(tmp_path / "locales")
    rc = reassemble.main(
        [CORPUS[1], "--langs", "he", "--tm-dir", str(tmp_path / "empty-tm"),
         "--out-dir", out_dir, "--md-root", os.path.join(REPO, "md"), "--dry-run"]
    )
    assert rc == 0
    assert not os.path.exists(out_dir)


def test_cli_fail_on_fallback_is_opt_in(tmp_path):
    """An untranslated corpus is a normal state of the pipeline, not an error —
    unless CI asks for the stricter reading."""
    common = [CORPUS[1], "--langs", "he", "--tm-dir", str(tmp_path / "empty-tm"),
              "--out-dir", str(tmp_path / "locales"),
              "--md-root", os.path.join(REPO, "md")]
    assert reassemble.main(common) == 0
    assert reassemble.main(common + ["--fail-on-fallback"]) == 1


def test_cli_reports_placeholder_violations_with_a_non_zero_exit(tmp_path, capsys):
    src_dir = tmp_path / "md"
    src_dir.mkdir()
    doc = src_dir / "doc.md"
    doc.write_text("Run `git status` now.\n", encoding="utf-8")

    tm = TranslationMemory.load(str(tmp_path / "tm"), "he")
    tm.upsert(
        hash_of(doc.read_text(encoding="utf-8"), "git status"),
        source="Run `git status` now.",
        translation="הרץ עכשיו.",
        model="stub/model",
        prompt_version="v1",
        action="TRANSLATE",
    )
    tm.save()

    rc = reassemble.main(
        [str(doc), "--langs", "he", "--tm-dir", str(tmp_path / "tm"),
         "--out-dir", str(tmp_path / "locales"), "--md-root", str(src_dir)]
    )
    assert rc == 1
    assert "placeholder violation" in capsys.readouterr().err
    written = (tmp_path / "locales" / "he" / "doc.md").read_text(encoding="utf-8")
    assert written.strip() == "Run `git status` now."


# --------------------------------------------------------------------------
# Path mirroring
# --------------------------------------------------------------------------


def test_locale_path_mirrors_the_source_tree():
    assert reassemble.locale_path("md/skills/x/SKILL.md", "he") == (
        os.path.join("locales", "he", "skills", "x", "SKILL.md")
    )
    assert reassemble.locale_path("md/a.md", "ru", "md", "out") == (
        os.path.join("out", "ru", "a.md")
    )


def test_locale_path_keeps_a_source_outside_the_md_root():
    """Mirroring something odd beats silently dropping it."""
    out = reassemble.locale_path(os.path.join("elsewhere", "a.md"), "he", "md")
    assert out.startswith(os.path.join("locales", "he"))
    assert out.endswith("a.md")
