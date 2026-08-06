"""Package data, located through `importlib.resources` — never by `__file__`.

Four kinds of file ship *inside* the wheel and are read at runtime:

| Resource | Read by |
| --- | --- |
| `providers.toml` | `cl10n.providers` — the registry |
| `compat-baseline.json` | `cl10n.compat_check` — the recorded parser contract |
| `fixtures/kitchen-sink.md` | `cl10n.compat_check` — the document that contract is measured on |
| `schemas/*.schema.json` | the test suite, and anyone validating pipeline state |

`os.path.join(os.path.dirname(__file__), …)` would work in a checkout and
break in `site-packages` the moment a build backend stopped copying a data
file — silently, and only for installed users. Going through
`importlib.resources` makes the wheel's contents the single source of truth,
so a missing entry in `pyproject.toml`'s package data fails loudly and
identically in both places. `.github/workflows/checks.yml`'s `clean-install`
job is what proves it, by running from a directory that holds no checkout.

`path()` returns a real filesystem path. That is a deliberate narrowing: it
assumes the package was installed as ordinary files rather than imported out
of a zip, which is what `pip install` produces for a wheel. In exchange every
caller keeps taking plain paths, including `compat_check --update`, which has
to *write* its baseline back.
"""

from __future__ import annotations

from importlib.resources import files as _files


def path(*parts: str) -> str:
    """Filesystem path to a package data file, e.g. `path("providers.toml")`."""
    return str(_files("cl10n").joinpath(*parts))


def read_text(*parts: str) -> str:
    """UTF-8 contents of a package data file."""
    return _files("cl10n").joinpath(*parts).read_text(encoding="utf-8")


def schema_path(name: str) -> str:
    """Path to one of the three JSON Schema contracts.

    `name` is the bare contract name — `queue`, `manifest`,
    `translation-memory` — not the file name.
    """
    return path("schemas", f"{name}.schema.json")
