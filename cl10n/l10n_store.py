"""Durable state files for the localization pipeline.

The *persistence* half of `.claude/rules/l10n-pipeline-spec.md` — atomic
writes (§7.5), the queue file (§4) and the per-language translation memory
(§3). Kept apart from `queue_runner` so a reader (reassembly, CI reporting,
a human with a REPL) can load the same files without pulling in an async
provider client.

**Every write here is synchronous on purpose.** The runner is single-threaded
asyncio, so a function with no `await` in it cannot be interleaved with another
coroutine: serialize-then-rename is therefore atomic against concurrent jobs
without a single lock. Making these `async` would open exactly the window the
locks would then have to close.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import secrets
import tempfile

# --------------------------------------------------------------------------
# Primitives
# --------------------------------------------------------------------------

QUEUE_SCHEMA = "queue/v1"
TM_SCHEMA = "tm/v1"


def utc_now() -> str:
    """ISO 8601 UTC, second precision, `Z` suffix — spec §7.3."""
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_run_id() -> str:
    """`20260804T120000Z-3f9c21` — compact UTC stamp plus 6 random hex."""
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(3)}"


def _dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2) + "\n"


def atomic_write_text(path: str, text: str) -> None:
    """Write via a temp file in the *same directory* plus `rename(2)`.

    Same directory matters: `rename` is only atomic within one filesystem, and
    a reader — including a runner restarted after a kill — must never observe a
    half-written state file (spec §4 "Durability").
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        # A crash mid-write leaves the previous good file in place; drop the
        # partial temp rather than littering the queue directory with them.
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# --------------------------------------------------------------------------
# Queue
# --------------------------------------------------------------------------


def load_queue(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save_queue(path: str, queue: dict) -> None:
    """Flush the whole queue. Job field order is the schema's; no sorting.

    The queue is rewritten in full on every state transition — the file on
    disk is the only thing a restart gets to read (spec §4), so a partial or
    deferred write is the same as no write.
    """
    atomic_write_text(path, _dumps(queue))


def new_queue(source_commit: str, jobs: list[dict], run_id: str | None = None) -> dict:
    return {
        "schema": QUEUE_SCHEMA,
        "run_id": run_id or new_run_id(),
        "created_at": utc_now(),
        "source_commit": source_commit,
        "jobs": jobs,
    }


def new_job(
    *,
    unit_hash: str,
    lang: str,
    action: str,
    source: str,
    context: str = "",
    placeholders: list[str] | None = None,
    old_source: str | None = None,
    prior_translation: str | None = None,
    max_attempts: int = 3,
) -> dict:
    """A `pending` job with every schema-required field present.

    `queue.schema.json` sets `additionalProperties: false` and requires all
    fifteen fields, nullable ones included — so a job is constructed complete
    and only ever has values replaced, never keys added.
    """
    return {
        "id": f"{lang}:{unit_hash}",
        "unit_hash": unit_hash,
        "lang": lang,
        "action": action,
        "source": source,
        "context": context,
        "placeholders": list(placeholders or []),
        "old_source": old_source,
        "prior_translation": prior_translation,
        "state": "pending",
        "attempts": 0,
        "max_attempts": max_attempts,
        "error": None,
        "started_at": None,
        "finished_at": None,
    }


# --------------------------------------------------------------------------
# Translation memory
# --------------------------------------------------------------------------


def tm_path(tm_dir: str, lang: str) -> str:
    return os.path.join(tm_dir, f"{lang}.json")


class TranslationMemory:
    """One language's `l10n/tm/<lang>.json` (spec §3).

    Entries are serialized with sorted keys so two runs that translate the
    same units produce byte-identical files and insertions stay local in a
    diff (spec §7.1).
    """

    def __init__(self, path: str, lang: str, data: dict | None = None):
        self.path = path
        self.lang = lang
        self.data = data or {"schema": TM_SCHEMA, "language": lang, "entries": {}}

    @classmethod
    def load(cls, tm_dir: str, lang: str) -> "TranslationMemory":
        path = tm_path(tm_dir, lang)
        if not os.path.exists(path):
            return cls(path, lang)
        with open(path, "r", encoding="utf-8") as fh:
            return cls(path, lang, json.load(fh))

    @property
    def entries(self) -> dict:
        return self.data["entries"]

    def get(self, unit_hash: str) -> dict | None:
        return self.entries.get(unit_hash)

    def upsert(
        self,
        unit_hash: str,
        *,
        source: str,
        translation: str,
        model: str,
        prompt_version: str,
        action: str,
        review_status: str = "machine",
        translated_at: str | None = None,
    ) -> dict:
        """Idempotent write keyed by unit hash — what makes at-least-once
        execution have exactly-once effect after a restart (spec §1)."""
        entry = {
            "source": source,
            "translation": translation,
            "model": model,
            "prompt_version": prompt_version,
            "translated_at": translated_at or utc_now(),
            "review_status": review_status,
            "action": action,
        }
        self.entries[unit_hash] = entry
        return entry

    def save(self) -> None:
        out = dict(self.data)
        out["entries"] = {k: self.entries[k] for k in sorted(self.entries)}
        atomic_write_text(self.path, _dumps(out))
