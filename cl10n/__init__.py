"""cl10n — continuous localization for Markdown corpora.

The pipeline is documented in `cl10n/USERGUIDE.md` (how) and
`.claude/rules/` (why). The four subcommands — `plan`, `run`, `render`,
`status` — are reached through the `cl10n` console script, which is
`cl10n.cli:main`.

Nothing is imported eagerly here on purpose. `cl10n.cli` pulls in the
parsing stack, and a connector pulls in its provider SDK; making
`import cl10n` cheap is what lets `cl10n.providers` be introspected (and
`__version__` be read) without paying for either.
"""

from importlib.metadata import PackageNotFoundError, version as _version

try:
    __version__ = _version("cl10n")
except PackageNotFoundError:  # running from a source tree, not installed
    __version__ = "0.0.0.dev0"

__all__ = ["__version__"]
