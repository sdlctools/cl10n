"""The provider registry — config-driven, no runner changes to add a provider.

A provider is declared in `cl10n/providers.toml` (CLN-1 AC2): a unique name,
the module path of its connector (which holds the client/inference code), the
env var and optional creds file its API key comes from, its base_url if it is
not the library default, and its default model. The registry loads that file
once, resolves a route (which provider a given `--model` / `--provider`
selects, AC1), lazy-imports the connector module only when it is resolved
(adding NVIDIA never drags `openai` into a Groq run), and builds a
`Translator` plus the `classify` the runner pairs it with.

The runner never grows a branch per provider. It holds the resolved
`Translator` and `classify` injected by `main()`; a third provider is a new
entry in `providers.toml` plus a new connector module — nothing in
`queue_runner.py` changes (AC2).

Routing (AC1), in priority order:

1. a `--model` of the form `provider:model` selects that provider and uses
   `model` (the prefix overrides and routes);
2. a `--provider` flag selects that provider, with a bare `--model` (or none)
   resolved against its default model;
3. nothing set → the default provider (backward compatible: Groq, default
   model), identical to today.

The prefix syntax is `provider:model` (colon). It is unambiguous because
provider names are short identifiers and no native model id we use contains a
colon (they are `vendor/model` or `vendor/tag`).
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import os
import re
import sys
import tomllib
from dataclasses import dataclass

_HERE = os.path.dirname(os.path.abspath(__file__))
_CL10N = os.path.dirname(_HERE)
# `providers.toml` lives beside the registry, in `cl10n/`.
DEFAULT_CONFIG = os.path.join(_CL10N, "providers.toml")

# `provider:model` — the provider name is a leading identifier up to the first
# colon; everything after is the model. A model id containing a colon would
# route to a non-existent provider and fail loudly, which is the right failure.
_PREFIX_RE = re.compile(r"^([a-zA-Z_][a-zA-Z0-9_-]*):(.+)$")


@dataclass(frozen=True)
class ProviderConfig:
    """One provider's declaration, as loaded from `providers.toml`.

    `connector` is a `module:Class` string the registry splits and
    lazy-imports; `default_model`, `api_key_env` and `api_key_creds_file` are
    the connector's defaults fed to the constructor / the key loader; and
    `base_url` lets an OpenAI-compatible connector override the library's
    default endpoint (NVIDIA uses it; Groq's is None, leaving the groq default).
    """

    name: str
    connector: str  # "module:Class" — resolved lazily
    default_model: str
    api_key_env: str
    api_key_creds_file: str | None = None
    base_url: str | None = None


@dataclass(frozen=True)
class Route:
    """What routing resolved: which provider, and which model to hand it.

    `model` is the model id after any prefix is stripped — the connector never
    sees the `provider:` prefix; the registry resolved it.
    """

    provider: str
    model: str


class Registry:
    """All providers declared in `providers.toml`, indexed by name (AC2)."""

    def __init__(self, configs: dict[str, ProviderConfig], default: str):
        self.providers = configs
        self.default = default
        if default not in configs:
            raise KeyError(f"default provider {default!r} not declared in providers.toml")

    def __contains__(self, name: str) -> bool:
        return name in self.providers

    def get(self, name: str) -> ProviderConfig:
        try:
            return self.providers[name]
        except KeyError:
            available = ", ".join(sorted(self.providers)) or "(none)"
            raise KeyError(f"unknown provider {name!r}; declared: {available}") from None

    def names(self) -> list[str]:
        return sorted(self.providers)


def load_registry(path: str | None = None) -> Registry:
    """Parse `providers.toml` (stdlib `tomllib`) into a `Registry` (AC2)."""
    path = path or DEFAULT_CONFIG
    with open(path, "rb") as fh:
        data = tomllib.load(fh)

    default = data.get("default")
    if not default:
        raise ValueError("providers.toml is missing a top-level `default = \"...\"`")
    if not isinstance(default, str):
        raise ValueError("providers.toml `default` must be a string (a provider name)")

    table = data.get("providers") or {}
    if not isinstance(table, dict):
        raise ValueError("providers.toml needs a `[providers.xxx]` table per provider")
    if default not in table:
        raise ValueError(f"default provider {default!r} has no [providers.{default}] table")

    configs: dict[str, ProviderConfig] = {}
    for name, cfg in table.items():
        if not isinstance(cfg, dict):
            raise ValueError(f"[providers.{name}] must be a table")
        for required in ("connector", "default_model", "api_key_env"):
            if required not in cfg:
                raise ValueError(f"[providers.{name}] is missing `{required}`")
        configs[name] = ProviderConfig(
            name=name,
            connector=cfg["connector"],
            default_model=cfg["default_model"],
            api_key_env=cfg["api_key_env"],
            api_key_creds_file=cfg.get("api_key_creds_file"),
            base_url=cfg.get("base_url"),
        )
    return Registry(configs, default)


def resolve_route(
    registry: Registry,
    *,
    model: str | None = None,
    provider: str | None = None,
) -> Route:
    """Which provider's connector to build, and which model to hand it (AC1).

    Priority: `provider:model` prefix > `--provider` flag > default. A bare
    model (no prefix) is resolved against the *selected* provider's default;
    with no model at all the selected provider's default is used. The connector
    never sees the prefix — it is stripped here.

    >>> r = Registry({"groq": ProviderConfig("groq", "", "g/default", "G"),
    ...              "nvidia": ProviderConfig("nvidia", "", "n/default", "N")}, "groq")
    >>> resolve_route(r, model="nvidia:n/specific")
    Route(provider='nvidia', model='n/specific')
    >>> resolve_route(r, model="bare/model", provider="nvidia")
    Route(provider='nvidia', model='bare/model')
    >>> resolve_route(r)
    Route(provider='groq', model='g/default')
    """
    if provider is not None and provider not in registry:
        raise KeyError(f"unknown provider {provider!r}; declared: " +
                       ", ".join(registry.names()))

    # 1. Prefix on --model wins, routing to that provider.
    if model is not None:
        match = _PREFIX_RE.match(model)
        if match:
            routed, bare = match.group(1), match.group(2)
            if routed not in registry:
                raise KeyError(
                    f"unknown provider {routed!r} in model prefix "
                    f"{model!r}; declared: " + ", ".join(registry.names())
                )
            return Route(provider=routed, model=bare)

    # 2. --provider flag selects; a bare model resolves against its default.
    selected = provider or registry.default
    cfg = registry.get(selected)
    return Route(provider=selected, model=model or cfg.default_model)


def _load_connector_module(module_name: str):
    """Import a connector module, lazily, resolving it unambiguously.

    **A bare connector name is loaded from this directory by file path, never
    by module name.** `cl10n/providers/groq.py` and the `groq` PyPI package
    share the top-level name `groq`, and the library is usually already in
    `sys.modules` — so `importlib.import_module("groq")` returns the *library*
    and the connector lookup fails with a confusing
    `module 'groq' has no attribute 'GroqTranslator'`. Loading by path also
    means the providers directory never has to go on `sys.path`, which is what
    would break `import groq` inside the connector itself.

    A dotted name (`my_pkg.connector`) is treated as a real importable module,
    for a future connector that lives outside this directory.
    """
    if "." in module_name:
        return importlib.import_module(module_name)

    key = f"_cl10n_providers_{module_name}"
    if key in sys.modules:
        return sys.modules[key]
    path = os.path.join(_HERE, f"{module_name}.py")
    if not os.path.exists(path):
        raise ModuleNotFoundError(
            f"connector module {module_name!r} not found at {path}"
        )
    spec = importlib.util.spec_from_file_location(key, path)
    module = importlib.util.module_from_spec(spec)
    # Registered before exec so a connector importing itself does not recurse.
    sys.modules[key] = module
    spec.loader.exec_module(module)
    return module


def _import_connector(connector_spec: str):
    """Resolve a `module:Class` connector spec to the class itself."""
    if ":" not in connector_spec:
        raise ValueError(f"connector {connector_spec!r} must be 'module:Class'")
    module_name, cls_name = connector_spec.rsplit(":", 1)
    mod = _load_connector_module(module_name)
    try:
        return getattr(mod, cls_name)
    except AttributeError:
        raise AttributeError(
            f"connector module {module_name!r} has no class {cls_name!r} "
            f"(loaded from {getattr(mod, '__file__', '?')})"
        ) from None


def build_translator(cfg: ProviderConfig, model: str):
    """Import the connector class lazily and construct it with the route's model.

    `base_url` and `api_key_env` are passed only when the provider declares one
    *and* the connector accepts it — Groq takes neither (its library default
    endpoint is right, and `groq_api.get_client` owns its key), NVIDIA takes
    both. Introspecting the signature rather than special-casing by name keeps
    this generic: a new connector opts in by naming the parameter.

    The connector's `__init__` keeps its own lazy client, so constructing the
    translator makes no network connection and needs no key (AC6).
    """
    cls = _import_connector(cfg.connector)
    accepted = inspect.signature(cls.__init__).parameters
    kwargs: dict = {"model": model}
    if cfg.base_url is not None and "base_url" in accepted:
        kwargs["base_url"] = cfg.base_url
    if "api_key_env" in accepted:
        kwargs["api_key_env"] = cfg.api_key_env
    return cls(**kwargs)


def get_classify(cfg: ProviderConfig):
    """Import the connector's `classify` lazily (same module, same loader).

    The runner pairs each `Translator` with its connector's `classify` so the
    right exception taxonomy is applied; this returns it without importing
    unrelated providers. Uses `_load_connector_module` for the same reason
    `_import_connector` does — a bare name must not resolve to a same-named
    provider library.
    """
    module_name = cfg.connector.rsplit(":", 1)[0]
    mod = _load_connector_module(module_name)
    if not hasattr(mod, "classify"):
        raise AttributeError(f"connector {cfg.connector!r} has no classify()")
    return mod.classify


def load_creds_file(path: str | None, env_var: str) -> None:
    """Read `<ENV_VAR>="..."` out of a creds file into the environment.

    Generalised from the runner's former `GROQ_API_KEY`-only loader: a connector
    declares its `api_key_creds_file` in `providers.toml`, and the runner asks
    for it by env-var name. Same convenience semantics — `os.environ.setdefault`
    so a real env var always wins, and a missing file is a no-op (CI passes the
    variable directly). The file is gitignored; no test reads it.
    """
    if not path or not os.path.exists(path):
        return
    pattern = re.compile(rf'\s*(?:export\s+)?{re.escape(env_var)}\s*=\s*["\']?([^"\'\s]+)')
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            match = pattern.match(line)
            if match:
                os.environ.setdefault(env_var, match.group(1))
                return
