"""markdown-it-py ↔ mdformat compatibility drift detector.

The pipeline's parser (`app/utils.make_parser`) is a *contract*, not a
convenience: every unit hash in every translation memory is taken over its
output. The two libraries behind it evolve independently, and their
disagreements have already cost this project twice — markdown-it-py grew
native task lists and native GitHub alerts, both of which mdformat cannot
render, so any document using them raised instead of localizing.

That failure mode is cheap to detect and expensive to discover. This script is
the detector. Run it in CI, and run it before accepting any upgrade of
`markdown-it-py`, `mdformat`, `mdformat-gfm`, `mdformat-frontmatter` or
`mdit-py-plugins`.

    venv/bin/python3 cl10n/compat_check.py            # verify against the baseline
    venv/bin/python3 cl10n/compat_check.py --json     # machine-readable
    venv/bin/python3 cl10n/compat_check.py --update   # re-record (review the diff!)

Exit code 0 means no drift, 1 means drift was found, 2 means the check itself
could not run.

## The three drift classes, cheapest signal first

1. **Option surface** — the effective options of the configured parser. A
   preset gaining an option (`gfm-like2` gained `alerts`) shows up here
   *before* anyone writes a document that triggers it. This is the earliest
   possible warning and the reason this check exists at all.

2. **Renderability** — every node type the parser can emit must be renderable,
   or the document raises. Two signals: the fixture must actually render, and
   no new node type may appear without a renderer. Some node types are
   legitimately rendererless because a parent consumes them (`tr` only exists
   inside a `table`, whose renderer swallows it) — those are allowlisted by
   name, so a *new* one is a finding rather than noise.

3. **Canonical form** — the fixture's canonical bytes and its unit hashes. A
   library that renders the same document differently silently moves every
   hash in the corpus, orphaning every translation and re-billing the lot.
   Nothing crashes; the bill just arrives. This is the most expensive drift
   and the least visible, so it is pinned exactly.

The fixture rather than `md/**` is the subject, deliberately: the corpus
changes when writers edit it, which would make the baseline churn and train
everyone to re-record it without reading. `cl10n/tests/fixtures/kitchen-sink.md`
changes only when someone means to change it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [_HERE, os.path.join(os.path.dirname(_HERE), "app")]

from markdown_it.tree import SyntaxTreeNode  # noqa: E402
from mdformat.renderer import DEFAULT_RENDERERS  # noqa: E402
import mdformat.plugins  # noqa: E402

import tree_diff  # noqa: E402
from utils import ast_to_markdown, make_parser, markdown_to_ast  # noqa: E402

BASELINE = os.path.join(_HERE, "compat-baseline.json")
FIXTURE = os.path.join(_HERE, "tests", "fixtures", "kitchen-sink.md")

# Node types with no renderer of their own that are nonetheless safe, because
# they are unreachable except under a parent whose renderer consumes the whole
# subtree. Keyed to the parent so a reviewer can check the claim rather than
# trust the list.
CONSUMED_BY_PARENT = {
    "thead": "table",
    "tbody": "table",
    "tr": "table",
}

# Packages whose versions decide everything above.
TRACKED = (
    "markdown-it-py",
    "mdit-py-plugins",
    "mdformat",
    "mdformat-gfm",
    "mdformat-frontmatter",
)


def versions() -> dict[str, str]:
    from importlib.metadata import PackageNotFoundError, version

    out = {}
    for name in TRACKED:
        try:
            out[name] = version(name)
        except PackageNotFoundError:
            out[name] = "MISSING"
    return out


def option_surface() -> dict[str, str]:
    """The parser's effective options, as strings so the JSON stays comparable."""
    md = make_parser()
    return {
        key: repr(value)
        for key, value in sorted(md.options.items())
        # Module objects — their identity is meaningless across runs, and the
        # `renderers` check below covers what they actually contribute.
        if key != "parser_extension"
    }


def renderable_node_types() -> set[str]:
    out = set(DEFAULT_RENDERERS)
    for plugin in mdformat.plugins.PARSER_EXTENSIONS.values():
        out |= set(getattr(plugin, "RENDERERS", {}))
    return out


def fixture_text() -> str:
    with open(FIXTURE, "r", encoding="utf-8") as fh:
        return fh.read()


def node_types(md_text: str) -> set[str]:
    root = SyntaxTreeNode(markdown_to_ast(md_text))
    seen: set[str] = set()

    def walk(node):
        seen.add(node.type)
        for child in node.children:
            walk(child)

    walk(root)
    return seen


def observe() -> dict:
    """Everything the baseline records, measured from the installed libraries."""
    source = fixture_text()
    canonical = ast_to_markdown(markdown_to_ast(source))
    root = SyntaxTreeNode(markdown_to_ast(canonical))
    doc_hash = tree_diff.hash_tree(root)

    seen = node_types(canonical)
    unrenderable = sorted(seen - renderable_node_types() - set(CONSUMED_BY_PARENT))

    return {
        "versions": versions(),
        "options": option_surface(),
        "node_types": sorted(seen),
        "node_types_without_renderer": unrenderable,
        "renderers": sorted(renderable_node_types()),
        "fixture_canonical_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
        "fixture_doc_hash": doc_hash,
        "fixture_unit_hashes": [u.h for u in tree_diff._units_under(root)],
        "fixture_is_idempotent": ast_to_markdown(markdown_to_ast(canonical)) == canonical,
        "fixture_is_stable": canonical == source,
    }


# --------------------------------------------------------------------------
# Comparison
# --------------------------------------------------------------------------


def _diff_map(name: str, old: dict, new: dict) -> list[str]:
    findings = []
    for key in sorted(set(old) | set(new)):
        before, after = old.get(key), new.get(key)
        if before != after:
            if before is None:
                findings.append(f"{name}: NEW `{key}` = {after}")
            elif after is None:
                findings.append(f"{name}: REMOVED `{key}` (was {before})")
            else:
                findings.append(f"{name}: `{key}` changed {before} → {after}")
    return findings


def compare(baseline: dict, current: dict) -> list[str]:
    """Findings, most structural first. Empty means no drift."""
    findings: list[str] = []

    # 1. Option surface — the earliest signal.
    findings += _diff_map("option", baseline.get("options", {}), current["options"])

    # 2. Renderability.
    new_gaps = set(current["node_types_without_renderer"])
    old_gaps = set(baseline.get("node_types_without_renderer", []))
    for node_type in sorted(new_gaps - old_gaps):
        findings.append(
            f"renderability: node type `{node_type}` has no mdformat renderer — "
            "any document producing it will raise. Either disable the parser "
            "option that emits it, or add it to CONSUMED_BY_PARENT with the "
            "parent that swallows it."
        )
    for node_type in sorted(set(current["node_types"]) - set(baseline.get("node_types", []))):
        findings.append(
            f"renderability: the fixture now produces node type `{node_type}`, "
            "which it did not before — a library started parsing something new."
        )
    if not current["fixture_is_idempotent"]:
        findings.append(
            "canonical form: canonicalisation is no longer idempotent — "
            "canonicalise(canonicalise(x)) != canonicalise(x). Every hash in "
            "the pipeline is unstable until this is fixed."
        )
    if not current["fixture_is_stable"]:
        findings.append(
            "canonical form: the fixture no longer round-trips to itself. "
            "Either a normalisation changed, or the fixture was edited without "
            "re-recording the baseline."
        )

    # 3. Canonical form and hashes — the expensive, silent class.
    for field, label in (
        ("fixture_canonical_sha256", "the canonical bytes"),
        ("fixture_doc_hash", "the document hash"),
    ):
        if baseline.get(field) != current[field]:
            findings.append(
                f"canonical form: {label} of the fixture changed "
                f"({baseline.get(field)} → {current[field]}). Every unit hash in "
                "every translation memory is taken over this canonicalisation, "
                "so shipping this orphans existing translations and re-bills the "
                "whole corpus."
            )
    before_units = baseline.get("fixture_unit_hashes", [])
    after_units = current["fixture_unit_hashes"]
    if before_units != after_units:
        moved = len(set(before_units) ^ set(after_units))
        findings.append(
            f"canonical form: {moved} fixture unit hash(es) moved "
            f"({len(before_units)} → {len(after_units)} units). This is the "
            "corpus-wide rehash signal — do not ship without a migration plan."
        )

    return findings


def report_versions(baseline: dict, current: dict) -> list[str]:
    """Version changes are context, never findings — an upgrade is the *cause*."""
    return _diff_map("version", baseline.get("versions", {}), current["versions"])


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def load_baseline() -> dict | None:
    if not os.path.exists(BASELINE):
        return None
    with open(BASELINE, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save_baseline(payload: dict) -> None:
    with open(BASELINE, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Detect markdown-it-py / mdformat drift against a recorded baseline.",
    )
    parser.add_argument("--update", action="store_true",
                        help="re-record the baseline (review the diff before committing)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    try:
        current = observe()
    except Exception as exc:  # a raising parser is itself the finding
        message = f"the configured parser could not process the fixture: {exc!r}"
        if args.json:
            print(json.dumps({"ok": False, "fatal": message}, indent=2))
        else:
            print(f"COMPAT FATAL — {message}", file=sys.stderr)
            print("This is the crash class of drift: a construct the parser now "
                  "emits has no renderer.", file=sys.stderr)
        return 1

    if args.update:
        save_baseline(current)
        if not args.json:
            print(f"baseline re-recorded → {os.path.relpath(BASELINE)}")
            print("Review the diff before committing — a changed hash means a "
                  "corpus-wide rehash.")
        return 0

    baseline = load_baseline()
    if baseline is None:
        print(f"no baseline at {BASELINE}; run with --update to create one",
              file=sys.stderr)
        return 2

    findings = compare(baseline, current)
    version_changes = report_versions(baseline, current)

    if args.json:
        print(json.dumps(
            {"ok": not findings, "findings": findings,
             "version_changes": version_changes, "observed": current},
            ensure_ascii=False, indent=2,
        ))
        return 1 if findings else 0

    for line in version_changes:
        print(f"  {line}")
    if not findings:
        print(f"COMPAT OK — {len(current['node_types'])} node types, "
              f"{len(current['fixture_unit_hashes'])} fixture units, hashes unchanged")
        return 0

    print(f"COMPAT DRIFT — {len(findings)} finding(s)\n")
    for finding in findings:
        print(f"  - {finding}")
    print("\nIf every change above is understood and intended, re-record with "
          "`--update` and commit the baseline alongside the dependency bump.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
