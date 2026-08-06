"""`cl10n.core.utils.make_parser` — the one parser configuration everything shares.

Every unit hash in the pipeline is taken over this parser's output, so a change
here moves translation-memory keys and silently orphans existing translations.
These tests pin the GFM constructs the corpus actually contains: each must
survive the canonicalisation round-trip unchanged, and — the part that is easy
to get wrong — must survive it *at all*, without the renderer raising.

Task lists are the reason this file exists. markdown-it-py 4.2's `gfm-like2`
preset parses them natively, setting `class="task-list-item"` and consuming the
`[ ] ` marker while emitting no checkbox token; `mdformat_gfm`'s list-item
renderer reads that class and then asserts on a checkbox only
`mdit_py_plugins.tasklists` produces. With both active the assertion fires and
any document containing a task list cannot be rendered — which means it cannot
be canonicalised, hashed, planned or localized.
"""

from __future__ import annotations

import pytest

from cl10n.core.utils import ast_to_markdown, markdown_to_ast  # noqa: E402


def canonicalise(md: str) -> str:
    return ast_to_markdown(markdown_to_ast(md))


GFM_CONSTRUCTS = {
    "task list": "- [ ] not done\n- [x] done\n",
    "task list, nested": "- [ ] outer\n  - [x] inner\n",
    "task list with inline code": "- [ ] run `make deploy`\n",
    "task list with a link": "- [ ] read [the docs](https://example.com)\n",
    "plain bullets": "- alpha\n- beta\n",
    "ordered list": "1. alpha\n2. beta\n",           # `number: True` renumbers
    "strikethrough": "Text with ~~deleted~~ words.\n",
    "table": "| a | b |\n| -- | -- |\n| 1 | 2 |\n",  # `compact_tables` narrows the rule
    "fence": "```bash\necho hello\n```\n",
    "heading + paragraph": "# Title\n\nA paragraph.\n",
    "blockquote": "> quoted text\n",
    "alert": "> [!NOTE]\n> Useful information.\n",
    "alert, warning": "> [!WARNING]\n> Be careful.\n",
    "raw html block": '<div align="center">\nhi\n</div>\n',
    "raw html inline": "Text with <b>bold</b> inside.\n",
    "image": "![alt](img.png)\n",
}

# Constructs mdformat deliberately normalises. They are listed separately
# because "unchanged" is the wrong assertion for them — what matters is that
# the normalisation is stable, since the hash is taken over the canonical form
# and both revisions of a document go through it.
NORMALISED = {
    "setext heading": ("Title\n=====\n", "# Title\n"),
    "single-tilde strikethrough": ("A ~word~ struck.\n", "A ~~word~~ struck.\n"),
    "hard break": ("line one  \nline two\n", "line one\\\nline two\n"),
    "reference link": (
        "[text][ref]\n\n[ref]: https://example.com\n",
        "[text](https://example.com)\n",
    ),
}


@pytest.mark.parametrize("name", sorted(GFM_CONSTRUCTS))
def test_construct_survives_canonicalisation(name):
    """Round-tripping must neither raise nor alter the construct."""
    src = GFM_CONSTRUCTS[name]
    once = canonicalise(src)
    assert once == src, f"{name}: canonicalisation changed the source"


@pytest.mark.parametrize("name", sorted(GFM_CONSTRUCTS))
def test_canonicalisation_is_idempotent(name):
    """The hash is taken over the canonical form, so it must be a fixed point."""
    once = canonicalise(GFM_CONSTRUCTS[name])
    assert canonicalise(once) == once


@pytest.mark.parametrize("name", sorted(NORMALISED))
def test_normalisation_is_what_we_expect_and_is_stable(name):
    """Pin the normalisations, so a library upgrade that changes one is visible.

    Every one of these rewrites the source, which is fine — both revisions of a
    document are canonicalised before hashing, so a normalisation is invisible
    to the diff as long as it is *stable*. A normalisation that changed between
    two runs would move every hash in the corpus at once.
    """
    src, expected = NORMALISED[name]
    once = canonicalise(src)
    assert once == expected
    assert canonicalise(once) == once


def test_an_alert_marker_is_protected_from_translation():
    """`[!NOTE]` is a keyword, not prose.

    mdformat cannot render markdown-it's dedicated `alert` nodes, so the parser
    reads alerts as ordinary blockquotes — which leaves the marker sitting in
    the paragraph's inline content, inside the translation unit. Nothing else
    in `_placeholders` would protect it, and a model that translates it yields
    a blockquote that only looks like an alert.
    """
    from cl10n.core import tree_diff
    from cl10n.placeholders import lost_placeholders

    canonical = canonicalise("> [!NOTE]\n> Useful information here.\n")
    item = next(i for i in tree_diff.plan("", canonical) if i.action == "TRANSLATE")

    assert "[!NOTE]" in item.placeholders
    assert lost_placeholders(item.new_source, "[!NOTE]\nמידע שימושי", item.placeholders) == []
    assert lost_placeholders(
        item.new_source, "[!הערה]\nמידע שימושי", item.placeholders
    ) == ["[!NOTE]"]


def test_an_alert_survives_a_translated_splice():
    from cl10n import reassemble
    from cl10n.core import tree_diff

    src = "> [!WARNING]\n> Do not do that.\n"
    entries = {
        h: {"source": s, "translation": s.replace("Do not do that.", "אל תעשה זאת"),
            "prompt_version": "v1"}
        for h, s in tree_diff.tm_keys(reassemble.canonicalise(src)).items()
    }
    out, report = reassemble.render_markdown(src, entries, lang="he")
    assert out.startswith("> [!WARNING]\n")
    assert not report.fallbacks


def test_a_task_list_is_hashable_and_segmented():
    """The end-to-end consequence: a task list yields translation units.

    Before the parser fix this raised `AssertionError` inside the renderer, so
    a document containing one could not be canonicalised at all — no hash, no
    plan, no localization.
    """
    from cl10n.core import tree_diff
    from markdown_it.tree import SyntaxTreeNode

    src = "# Checklist\n\n- [ ] first item\n- [x] second item\n"
    root = SyntaxTreeNode(markdown_to_ast(canonicalise(src)))
    tree_diff.hash_tree(root)
    units = list(tree_diff._units_under(root))

    sources = [tree_diff._unit_source(u) for u in units]
    assert "first item" in " ".join(sources)
    assert "second item" in " ".join(sources)
    # The checkbox is list *structure*, not translatable content — it must not
    # land in a unit's source, or the model would be asked to translate it.
    assert not any("task-list-item-checkbox" in s for s in sources)
    assert all(len(u.h) == 16 for u in units)


def test_a_task_lists_checkbox_state_survives_a_translated_splice():
    """Checkbox state is the source's, and must outlive the splice.

    The checkbox lives as a leading `html_inline` child of the item's *inline*
    token, so replacing that token's children wholesale — which is exactly what
    splicing a translation does — drops it, leaving a `list_item` still classed
    `task-list-item` with no checkbox for mdformat to read. That raises, so a
    single translated checklist would take its whole document down.
    """
    from cl10n import reassemble
    from cl10n.core import tree_diff

    src = "# Checklist\n\n- [ ] not done yet\n- [x] already done\n\nA paragraph.\n"
    entries = {
        unit_hash: {"source": source,
                    "translation": f"«{source.strip()}»",
                    "prompt_version": "v1"}
        for unit_hash, source in tree_diff.tm_keys(reassemble.canonicalise(src)).items()
    }

    out, report = reassemble.render_markdown(src, entries, lang="he")

    assert report.translated == 4 and not report.fallbacks
    assert "- [ ] «not done yet»" in out
    assert "- [x] «already done»" in out
