"""The per-document ledger — `l10n/manifest.json`, spec §6.

The manifest is how a run finds out what the *previous* run localized. It holds
no copy of the corpus: each file records the git blob SHA of the revision that
was last localized, and step 3 recovers that revision with `git cat-file blob`.
A blob that cannot be read — shallow clone, never-committed edit, a manifest
carried across a rewritten history — is not an error: the document is planned
against the empty document, which is spec §1's degenerate first-time case and
the reason this pipeline has no separate bootstrap mode.

Two things live here rather than in `cli.py`, because both are ledger
questions rather than command-line ones:

- **the document inventory** (`doc_hash`, `unit_hashes`, `opaque_hashes`) —
  taken from `tree_diff` over the canonicalised source, so it is the same
  segmentation the planner diffs and the renderer splices;
- **RETIRE garbage collection** — a TM entry dies only when *no* file in the
  ledger still lists its hash. Unit hashes are content-addressed, so the same
  paragraph in two documents is one entry with two references; deleting on the
  first RETIRE would break the second document (spec §2).

No provider, no asyncio, no argparse — importable from a REPL or a test.
"""

from __future__ import annotations

import json
import os
import subprocess

from markdown_it.tree import SyntaxTreeNode

import tree_diff
from l10n_store import atomic_write_text
from utils import ast_to_markdown, markdown_to_ast

MANIFEST_SCHEMA = "manifest/v1"
DEFAULT_MANIFEST = "l10n/manifest.json"

_opaque_under = tree_diff._opaque_under
_units_under = tree_diff._units_under


# --------------------------------------------------------------------------
# Git access
# --------------------------------------------------------------------------


def _git(repo: str, *args: str) -> str | None:
    """`git -C repo …`, or `None` if git failed. Never raises.

    Every caller here has a defined answer for "git could not tell me": plan
    against the empty document, or record an all-zero blob. Turning a missing
    object into an exception would make an unlocalizable corner of the corpus
    fail the whole run.
    """
    try:
        out = subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True, text=True, check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, UnicodeDecodeError):
        return None
    return out.stdout


def head_commit(repo: str) -> str:
    """The 40-hex commit the plan is computed from — `queue.source_commit`."""
    out = _git(repo, "rev-parse", "HEAD")
    return (out or "").strip() or "0" * 40


def blob_sha(repo: str, path: str) -> str:
    """Blob SHA of the file's *current* content (`git hash-object`).

    Deliberately the working tree's content and not `git rev-parse HEAD:<path>`:
    the manifest records what was actually localized, and in CI that is the file
    on disk. When the corpus is committed — which it is, it is what triggers the
    pipeline — the two agree and the blob is readable by the next run.
    """
    out = _git(repo, "hash-object", "--", path)
    sha = (out or "").strip()
    return sha if len(sha) == 40 else "0" * 40


def blob_text(repo: str, sha: str) -> str:
    """The blob's content, or `""` when it cannot be read (spec §1)."""
    if not sha or set(sha) == {"0"}:
        return ""
    return _git(repo, "cat-file", "blob", sha) or ""


# --------------------------------------------------------------------------
# Document inventory
# --------------------------------------------------------------------------


def canonicalise(md: str) -> str:
    """The mdformat round-trip every hash in this pipeline is taken over.

    Same function as `reassemble.canonicalise`, applied at the *other* end of
    the pipeline so the hashes the planner enqueues are the ones the renderer
    looks up. Empty in, empty out — the degenerate first-time revision must not
    become a one-newline document with a hash of its own.
    """
    return ast_to_markdown(markdown_to_ast(md)) if md.strip() else ""


def inventory(md: str) -> tuple[str, list[str], list[str]]:
    """`(doc_hash, unit_hashes, opaque_hashes)` for one document, in document order."""
    root = SyntaxTreeNode(markdown_to_ast(canonicalise(md)))
    doc_hash = tree_diff.hash_tree(root)
    return (
        doc_hash,
        [u.h for u in _units_under(root)],
        [o.h for o in _opaque_under(root)],
    )


# --------------------------------------------------------------------------
# Load / save
# --------------------------------------------------------------------------


def new_manifest(langs) -> dict:
    return {"schema": MANIFEST_SCHEMA, "languages": list(langs), "files": {}}


def load(path: str, langs) -> dict:
    if not os.path.exists(path):
        return new_manifest(langs)
    with open(path, "r", encoding="utf-8") as fh:
        manifest = json.load(fh)
    # A language added since the last run must appear in `languages`, or the
    # ledger claims a corpus narrower than the one being localized.
    known = manifest.setdefault("languages", [])
    for lang in langs:
        if lang not in known:
            known.append(lang)
    manifest.setdefault("files", {})
    return manifest


def save(path: str, manifest: dict) -> None:
    """Atomic, with `files` sorted — spec §7.1, so diffs stay deterministic."""
    out = dict(manifest)
    out["languages"] = sorted(manifest.get("languages", []))
    out["files"] = {k: manifest["files"][k] for k in sorted(manifest["files"])}
    atomic_write_text(path, json.dumps(out, ensure_ascii=False, indent=2) + "\n")


# --------------------------------------------------------------------------
# Reads and writes against one file entry
# --------------------------------------------------------------------------


def previous_source(manifest: dict, rel_path: str, repo: str) -> str:
    """The last-localized revision of `rel_path`, or `""` if there is none."""
    entry = manifest["files"].get(rel_path)
    if not entry:
        return ""
    return blob_text(repo, entry.get("source_blob", ""))


def record_revision(
    manifest: dict,
    rel_path: str,
    *,
    source_blob: str,
    doc_hash: str,
    unit_hashes: list[str],
    opaque_hashes: list[str],
) -> dict:
    """Advance the file to the revision that has just been rendered.

    Per-language completion is preserved: a `he` run must not erase what a
    previous `ru` run recorded for the same file.
    """
    entry = manifest["files"].setdefault(rel_path, {"localized": {}})
    entry["source_blob"] = source_blob
    entry["doc_hash"] = doc_hash
    entry["unit_hashes"] = list(unit_hashes)
    entry["opaque_hashes"] = list(opaque_hashes)
    entry.setdefault("localized", {})
    return entry


def record_localized(
    manifest: dict,
    rel_path: str,
    lang: str,
    *,
    run_id: str,
    completed_at: str,
    fallbacks: list[str],
) -> None:
    entry = manifest["files"].setdefault(rel_path, {"localized": {}})
    entry.setdefault("localized", {})[lang] = {
        "run_id": run_id,
        "completed_at": completed_at,
        "fallbacks": sorted(set(fallbacks)),
    }


# --------------------------------------------------------------------------
# RETIRE garbage collection
# --------------------------------------------------------------------------


def referenced_hashes(manifest: dict) -> set[str]:
    """Every unit hash any file in the ledger still points at."""
    out: set[str] = set()
    for entry in manifest["files"].values():
        out.update(entry.get("unit_hashes", []))
    return out


def collect_garbage(tm, referenced: set[str]) -> list[str]:
    """Drop TM entries no file references any more. Returns what was dropped.

    Caller's contract: `referenced` must be the union over the **whole** corpus,
    not the subset this run happened to touch. `cli.render` only calls this
    after a full-corpus render for exactly that reason — retiring on a
    single-file render would delete the other files' translations.
    """
    dead = sorted(set(tm.entries) - referenced)
    for unit_hash in dead:
        del tm.entries[unit_hash]
    return dead
