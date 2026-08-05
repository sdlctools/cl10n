"""The placeholder-integrity rule — `.claude/rules/l10n-pipeline-spec.md` §5.

One rule, two enforcement points, and they must not drift:

- `queue_runner` applies it to every API response, so a translation that lost
  a placeholder never enters the translation memory;
- `reassemble` applies it again to every TM entry it is about to splice, so a
  translation that got into the memory some *other* way — a hand-edited
  `l10n/tm/<lang>.json`, an entry written by an older or third-party runner —
  cannot reach a rendered file silently either.

The second check is not redundant. The TM is a committed, reviewable,
human-editable artifact (that is what `review_status: "approved"` means), and
the renderer is the last thing standing between a broken command or a dead
link and the published document.

No provider, no asyncio, no Markdown parsing — importable from anywhere in the
pipeline.
"""

from __future__ import annotations


def lost_placeholders(source: str, translation: str, placeholders) -> list[str]:
    """The placeholders `translation` failed to carry over from `source`.

    The rule: every placeholder must occur in the translation **at least as
    many times** as in the source, verbatim. "At least" rather than "exactly"
    because a target language may legitimately repeat a term the source states
    once; losing one is the failure this catches.

    A placeholder absent from the source cannot be lost, so it never fails —
    `source.count(ph)` is the bar, not mere presence.

    Empty entries are ignored: `tree_diff._placeholders` filters falsy spans
    already, but a hand-written queue or TM need not have.
    """
    return [
        ph
        for ph in dict.fromkeys(p for p in placeholders if p)  # dedup, keep order
        if translation.count(ph) < source.count(ph)
    ]


def describe(source: str, translation: str, lost) -> str:
    """`"placeholder '`x`' occurs 2x in source, 1x in translation"`, joined.

    The operator-facing half of the rule: which span, and by how much — enough
    to tell a dropped code span from a link the model rewrote.
    """
    return "; ".join(
        f"placeholder {ph!r} occurs {source.count(ph)}x in source, "
        f"{translation.count(ph)}x in translation"
        for ph in lost
    )
