"""
Incremental Markdown localization — locating *what changed* in an AST.

Pipeline this file implements (steps 2-3 of the research plan):

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
       translation units (the nodes you actually hand to the LLM)

Why Merkle hashing: hash(node) = H(type, tag, own_text, hash(c1)…hash(cn)).
Two subtrees are identical iff their hashes match, so a diff never has to
descend into an unchanged branch. Cost is O(n) to hash + O(k²) per *changed*
sibling list, i.e. proportional to the size of the edit, not the document.

Why LCS per level and not positional compare: inserting one paragraph shifts
every following sibling. Positional comparison would mark the whole tail
dirty; LCS over the children's hashes recovers the alignment exactly.

Run:  venv/bin/python3 app/tree_diff.py            # demo on the sample doc
      venv/bin/python3 app/tree_diff.py a.md b.md
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
from dataclasses import dataclass, field
from difflib import SequenceMatcher

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from markdown_it.tree import SyntaxTreeNode

from utils import ast_to_markdown, markdown_to_ast

# ---------------------------------------------------------------------------
# Node classification
# ---------------------------------------------------------------------------

# Blocks that carry translatable prose. In markdown-it's tree these are exactly
# the nodes that own an `inline` child: the inline node's `.content` is the raw
# markdown source of the segment (`` Reference for `x`. Read **this** ``), which
# is what we hand to the translator.
UNIT_PARENTS = {"heading", "paragraph", "th", "td"}

# Opaque blocks: hashed so we notice they moved/changed, never translated.
OPAQUE = {"fence", "code_block", "html_block", "front_matter", "hr"}

# Inline leaves whose text must survive translation verbatim.
NON_TRANSLATABLE_INLINE = {"code_inline", "html_inline", "image"}

_WS = re.compile(r"\s+")


def norm(text: str) -> str:
    """Whitespace-insensitive normalisation.

    Reflowing a paragraph (80 cols -> 72 cols) must NOT invalidate its
    translation, so line breaks and runs of spaces collapse before hashing.
    """
    return _WS.sub(" ", text or "").strip()


def is_unit(node: SyntaxTreeNode) -> bool:
    return node.type in UNIT_PARENTS


def attr(node: SyntaxTreeNode, name: str, default=""):
    """Safe accessor — the root node raises on tag/content/map/level/info."""
    if node.type == "root":
        return default
    try:
        return getattr(node, name, default) or default
    except AttributeError:
        return default


def own_text(node: SyntaxTreeNode) -> str:
    """The text this node contributes on its own (excluding children)."""
    return attr(node, "content")


# ---------------------------------------------------------------------------
# 1. Merkle hashing
# ---------------------------------------------------------------------------


def hash_tree(node: SyntaxTreeNode) -> str:
    """Annotate every node with `.h` (Merkle hash) and `.size` (#descendants).

    Deliberately excluded from the hash:
      * `map` (line numbers)  — shift on every insert above
      * `level`               — shifts when nesting changes elsewhere
    Included: type, tag, `info` (fence language), and normalised own text.
    """
    if is_unit(node):
        # A unit's identity is its inline source; its children are just the
        # parsed view of that same string.
        payload = f"{node.type}|{attr(node, 'tag')}|{norm(_unit_source(node))}"
        h = hashlib.sha256(payload.encode()).hexdigest()[:16]
        node.h = h
        node.size = 1
        for c in node.children:
            hash_tree(c)
        return h

    parts = [node.type, attr(node, "tag"), attr(node, "info")]
    if not node.children:
        parts.append(norm(own_text(node)))
    size = 1
    for child in node.children:
        parts.append(hash_tree(child))
        size += child.size
    node.h = hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]
    node.size = size
    return node.h


def _unit_source(node: SyntaxTreeNode) -> str:
    for c in node.children:
        if c.type == "inline":
            return c.content or ""
    return own_text(node)


# ---------------------------------------------------------------------------
# 2. Diff — per-level LCS + local fuzzy pairing
# ---------------------------------------------------------------------------

KINDS = ("EQUAL", "UPDATE", "INSERT", "DELETE", "MOVE")


@dataclass
class Op:
    kind: str
    old: SyntaxTreeNode | None = None
    new: SyntaxTreeNode | None = None
    sim: float = 1.0

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        n = self.new or self.old
        return f"<{self.kind} {n.type} sim={self.sim:.2f}>"


# Below this, two nodes are considered unrelated rather than "one edited into
# the other". GumTree (Falleri et al., ASE'14) uses 0.5 for structural
# similarity; 0.4 is a little more eager, which is what we want for prose.
SIM_THRESHOLD = 0.4


def ratio(a: str, b: str) -> float:
    """Normalised text similarity in [0, 1].

    `autojunk=False` is not optional here. difflib's default heuristic marks any
    element occurring in >1% of a sequence as junk once the sequence reaches 200
    items — on *character* sequences that is every common letter, which both
    skews the score on long paragraphs and makes it asymmetric.
    """
    return SequenceMatcher(None, norm(a), norm(b), autojunk=False).ratio()


def similarity(a: SyntaxTreeNode, b: SyntaxTreeNode) -> float:
    if a.type != b.type:
        return 0.0
    if is_unit(a):
        return ratio(_unit_source(a), _unit_source(b))

    # Structural: Dice coefficient over the sets of descendant hashes.
    ah, bh = _descendant_hashes(a), _descendant_hashes(b)
    dice = 1.0 if not ah and not bh else 2 * len(ah & bh) / (len(ah) + len(bh))

    # Dice collapses to 0 for a small container (a one-paragraph list_item):
    # editing its only sentence invalidates every descendant hash. Fall back to
    # comparing the flattened prose, which still reads as "the same item".
    if dice >= SIM_THRESHOLD or len(ah) > 24:
        return dice
    return max(dice, ratio(_flat_text(a), _flat_text(b)))


_FLAT_CAP = 4000


def _flat_text(node: SyntaxTreeNode) -> str:
    if getattr(node, "_flat", None) is None:
        node._flat = norm(" ".join(_unit_source(u) for u in _units_under(node)))[:_FLAT_CAP]
    return node._flat


def _descendant_hashes(node: SyntaxTreeNode) -> set[str]:
    out: set[str] = set()
    stack = list(node.children)
    while stack:
        n = stack.pop()
        out.add(n.h)
        stack.extend(n.children)
    return out


def diff_trees(old_root: SyntaxTreeNode, new_root: SyntaxTreeNode) -> list[Op]:
    hash_tree(old_root)
    hash_tree(new_root)
    ops: list[Op] = []
    _diff_children(old_root, new_root, ops)
    return _detect_moves(ops)


def _diff_node(a: SyntaxTreeNode, b: SyntaxTreeNode, ops: list[Op]) -> None:
    if a.h == b.h:
        ops.append(Op("EQUAL", a, b))
        return
    if is_unit(a) and is_unit(b) and a.type == b.type:
        ops.append(Op("UPDATE", a, b, similarity(a, b)))
        return
    if a.type != b.type or not a.children or not b.children:
        ops.append(Op("DELETE", a, None))
        ops.append(Op("INSERT", None, b))
        return
    _diff_children(a, b, ops)


def _diff_children(a: SyntaxTreeNode, b: SyntaxTreeNode, ops: list[Op]) -> None:
    ah = [c.h for c in a.children]
    bh = [c.h for c in b.children]
    # autojunk=False: a doc with many identical short nodes (e.g. `---`) must
    # not have them silently treated as noise.
    sm = SequenceMatcher(None, ah, bh, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                ops.append(Op("EQUAL", a.children[i1 + k], b.children[j1 + k]))
        elif tag == "delete":
            ops.extend(Op("DELETE", c, None) for c in a.children[i1:i2])
        elif tag == "insert":
            ops.extend(Op("INSERT", None, c) for c in b.children[j1:j2])
        else:
            _align_window(a.children[i1:i2], b.children[j1:j2], ops)


def _align_window(olds, news, ops: list[Op]) -> None:
    """Pair up the nodes inside one `replace` window by best similarity.

    Greedy best-first over an m×n score matrix; m and n are the size of a
    single changed sibling run, so this stays cheap.
    """
    scored = sorted(
        (
            (similarity(o, n), i, j)
            for i, o in enumerate(olds)
            for j, n in enumerate(news)
        ),
        key=lambda t: -t[0],
    )
    used_o: set[int] = set()
    used_n: set[int] = set()
    pairs: list[tuple[int, int]] = []
    for score, i, j in scored:
        if score < SIM_THRESHOLD or i in used_o or j in used_n:
            continue
        used_o.add(i)
        used_n.add(j)
        pairs.append((i, j))

    for i, j in sorted(pairs, key=lambda p: p[1]):
        _diff_node(olds[i], news[j], ops)
    ops.extend(Op("DELETE", o, None) for i, o in enumerate(olds) if i not in used_o)
    ops.extend(Op("INSERT", None, n) for j, n in enumerate(news) if j not in used_n)


def _detect_moves(ops: list[Op]) -> list[Op]:
    """A DELETE and an INSERT with the same Merkle hash is a move.

    Detecting moves is what makes hashing worth it: moved content keeps its
    translation for free, where a plain edit-script differ would re-translate.
    """
    deletes: dict[str, list[int]] = {}
    for idx, op in enumerate(ops):
        if op.kind == "DELETE":
            deletes.setdefault(op.old.h, []).append(idx)

    consumed: set[int] = set()
    for op in ops:
        if op.kind != "INSERT":
            continue
        bucket = deletes.get(op.new.h)
        while bucket:
            idx = bucket.pop(0)
            if idx in consumed:
                continue
            consumed.add(idx)
            op.kind = "MOVE"
            op.old = ops[idx].old
            break
    return [op for i, op in enumerate(ops) if i not in consumed]


# ---------------------------------------------------------------------------
# 3. Translation units — what actually goes to the LLM
# ---------------------------------------------------------------------------


@dataclass
class WorkItem:
    action: str  # TRANSLATE | REVISE | REUSE | RECHECK | RETIRE | COPY
    unit_hash: str
    node_type: str
    context: str  # heading trail — read-only context for the prompt
    new_source: str = ""
    old_source: str = ""
    sim: float = 0.0
    placeholders: list[str] = field(default_factory=list)
    node: SyntaxTreeNode | None = field(default=None, repr=False)


def _heading_trail(node: SyntaxTreeNode) -> str:
    """Ancestor headings above `node` — the context an LLM needs to translate
    a fragment correctly without being told to translate the context itself."""
    trail: list[str] = []
    cur = node
    while cur is not None and cur.type != "root":
        parent = cur.parent
        if parent is None:
            break
        sibs = parent.children
        for sib in sibs[: sibs.index(cur)][::-1]:
            if sib.type == "heading":
                trail.append(norm(_unit_source(sib)))
                break
        cur = parent
    return " › ".join(reversed(trail))


def _placeholders(node: SyntaxTreeNode) -> list[str]:
    """Inline spans the translator must reproduce byte-for-byte.

    This is the XLIFF `<ph>` idea: protect them, don't trust the model with
    them. Verifying they survive round-trip is a cheap post-translation gate.
    """
    out: list[str] = []
    stack = list(node.children)
    while stack:
        n = stack.pop()
        if n.type in NON_TRANSLATABLE_INLINE:
            out.append(own_text(n) or getattr(n, "attrs", {}).get("src", ""))
        if n.type == "link":
            out.append(n.attrs.get("href", ""))
        stack.extend(n.children)
    return [p for p in out if p]


def _units_under(node: SyntaxTreeNode):
    if is_unit(node):
        yield node
        return
    if node.type in OPAQUE:
        return
    for c in node.children:
        yield from _units_under(c)


def _opaque_under(node: SyntaxTreeNode):
    """Code fences, raw HTML, front matter — never translated, but they still
    have to be carried into the target document when they change.

    Only the *new* side is ever walked: assembly rebuilds the target from the
    new tree, so a removed opaque block disappears by construction and needs no
    op. Nothing here enters the translation memory, so there is nothing to
    retire either.
    """
    if is_unit(node):
        return
    if node.type in OPAQUE:
        yield node
        return
    for c in node.children:
        yield from _opaque_under(c)


def _copy_items(node: SyntaxTreeNode) -> list[WorkItem]:
    """COPY work items for the opaque blocks under a changed subtree."""
    return [
        WorkItem("COPY", o.h, o.type, _heading_trail(o),
                 new_source=own_text(o), node=o)
        for o in _opaque_under(node)
    ]


def plan(old_md: str, new_md: str) -> list[WorkItem]:
    """Turn two markdown revisions into a translation work list."""
    old_root = SyntaxTreeNode(markdown_to_ast(old_md))
    new_root = SyntaxTreeNode(markdown_to_ast(new_md))
    ops = diff_trees(old_root, new_root)

    items: list[WorkItem] = []
    for op in ops:
        if op.kind == "EQUAL":
            for u in _units_under(op.new):
                items.append(WorkItem("REUSE", u.h, u.type, _heading_trail(u),
                                      _unit_source(u)))
        elif op.kind == "MOVE":
            # Content identical, position changed: keep the translation but
            # flag it — gendered/deictic phrasing can depend on the section.
            for u in _units_under(op.new):
                items.append(WorkItem("RECHECK", u.h, u.type, _heading_trail(u),
                                      _unit_source(u)))
            items.extend(_copy_items(op.new))
        elif op.kind == "UPDATE":
            items.append(WorkItem("REVISE", op.new.h, op.new.type,
                                  _heading_trail(op.new),
                                  _unit_source(op.new), _unit_source(op.old),
                                  op.sim, _placeholders(op.new)))
        elif op.kind == "INSERT":
            for u in _units_under(op.new):
                items.append(WorkItem("TRANSLATE", u.h, u.type, _heading_trail(u),
                                      _unit_source(u),
                                      placeholders=_placeholders(u), node=u))
            items.extend(_copy_items(op.new))
        elif op.kind == "DELETE":
            for u in _units_under(op.old):
                items.append(WorkItem("RETIRE", u.h, u.type, _heading_trail(u),
                                      old_source=_unit_source(u), node=u))
    return _fuzzy_pair(items)


# Fuzzy matches below this are worse than translating from scratch; SDL/Trados
# and memoQ default their TM cut-off to 70% for the same reason.
FUZZY_THRESHOLD = 0.6


def _fuzzy_pair(items: list[WorkItem]) -> list[WorkItem]:
    """Upgrade leftover RETIRE+TRANSLATE pairs into REVISE.

    The local alignment in `_align_window` only sees one sibling run. A unit
    that was *moved and edited* lands in the global delete/insert pools, so a
    second, document-wide fuzzy pass recovers it — this is the classic CAT-tool
    fuzzy match, just keyed on units instead of raw lines.
    """
    gone = [i for i in items if i.action == "RETIRE"]
    fresh = [i for i in items if i.action == "TRANSLATE"]
    if not gone or not fresh or len(gone) * len(fresh) > 250_000:
        return items

    scored = sorted(
        (
            (ratio(g.old_source, f.new_source), gi, fi)
            for gi, g in enumerate(gone)
            for fi, f in enumerate(fresh)
            if g.node_type == f.node_type
        ),
        key=lambda t: -t[0],
    )
    used_g: set[int] = set()
    used_f: set[int] = set()
    for score, gi, fi in scored:
        if score < FUZZY_THRESHOLD or gi in used_g or fi in used_f:
            continue
        used_g.add(gi)
        used_f.add(fi)
        fresh[fi].action = "REVISE"
        fresh[fi].old_source = gone[gi].old_source
        fresh[fi].sim = score
    retired = {id(gone[gi]) for gi in used_g}
    return [i for i in items if id(i) not in retired]


# ---------------------------------------------------------------------------
# 4. Translation memory keying (the O(n) shortcut)
# ---------------------------------------------------------------------------


def tm_keys(md: str) -> dict[str, str]:
    """{unit_hash: source} for one document.

    With a persisted TM keyed by these hashes you do not need a diff at all to
    answer "what is untranslated": it is `tm_keys(new) - tm.keys()`. The tree
    diff above is what upgrades a plain miss into a *revision* (old source +
    old translation + new source), which is cheaper and far more consistent
    than translating from scratch.
    """
    root = SyntaxTreeNode(markdown_to_ast(md))
    hash_tree(root)
    return {u.h: _unit_source(u) for u in _units_under(root)}


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLE = os.path.join(HERE, "..", "md", "skills", "_shared", "templates",
                      "review-report.md")


# def _mutate(md: str) -> str:
#     """Four realistic edits: append, in-place edit, insert, reflow."""
#     md = md.replace(
#         "- the Jira per-issue comment (3d),",
#         "- the Jira per-issue comment (3d), including the audit trail,",
#     )
#     md = md.replace(
#         "## Template — fill every section",
#         "A worked example follows the template below.\n\n"
#         "## Template — fill every section",
#     )
#     md = md.replace(
#         "Someone following one review across GitHub, Jira, and chat therefore sees\n"
#         "one layout at one level of detail.",
#         "Someone following one review across GitHub, Jira, and chat therefore "
#         "sees one layout at one level of detail.",  # pure reflow, must be a no-op
#     )
#     return md


WIDTH = 110


def _clip(text: str, start: int = 0) -> str:
    body = norm(text)
    frag = body[start:start + WIDTH]
    return ("…" if start else "") + frag + ("…" if start + WIDTH < len(body) else "")


def _focus(old: str, new: str) -> tuple[str, str]:
    """Clip both sides around their *first difference*.

    Plain head-truncation hides the edit whenever it falls past the cut-off,
    which makes a REVISE line look like it reports two identical strings.
    """
    o, n = norm(old), norm(new)
    lead = 30
    for tag, i1, _i2, j1, _j2 in SequenceMatcher(None, o, n, autojunk=False).get_opcodes():
        if tag != "equal":
            return _clip(o, max(0, i1 - lead)), _clip(n, max(0, j1 - lead))
    return _clip(o), _clip(n)


def main() -> None:
    if len(sys.argv) == 3:
        old_md = open(sys.argv[1], encoding="utf-8").read()
        new_md = open(sys.argv[2], encoding="utf-8").read()
    else:
        sys.exit(f"usage: {os.path.basename(sys.argv[0])} OLD.md NEW.md")

    items = plan(old_md, new_md)

    counts: dict[str, int] = {}
    for it in items:
        counts[it.action] = counts.get(it.action, 0) + 1
    copies = counts.get("COPY", 0)
    total = len(items) - copies
    touched = total - counts.get("REUSE", 0)
    print(f"{total} translation units — {touched} need the LLM "
          f"({100 * touched / max(total, 1):.1f}%)"
          + (f"; {copies} opaque block(s) to copy verbatim" if copies else ""))
    print("  " + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())) + "\n")

    for it in items:
        if it.action == "REUSE":
            continue
        print(f"[{it.action}] {it.node_type}  #{it.unit_hash}"
              + (f"  sim={it.sim:.2f}" if it.action == "REVISE" else ""))
        if it.context:
            print(f"    ctx : {it.context}")
        if it.old_source and it.new_source:
            old_frag, new_frag = _focus(it.old_source, it.new_source)
            print(f"    old : {old_frag}")
            print(f"    new : {new_frag}")
        elif it.old_source:
            print(f"    old : {_clip(it.old_source)}")
        elif it.new_source:
            print(f"    new : {_clip(it.new_source)}")
        if it.placeholders:
            print(f"    keep: {it.placeholders}")
        print()


if __name__ == "__main__":
    main()
