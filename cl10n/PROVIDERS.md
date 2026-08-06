# Adding a translation provider

How to teach the pipeline to talk to a new LLM API. The whole job is **one TOML
entry plus one Python module** — if you find yourself editing `queue_runner.py`,
stop and re-read [section 5](#5-what-you-must-not-do), because the seam has been
broken rather than used.

Read [`.claude/rules/cl10n-runner-spec.md`](../.claude/rules/cl10n-runner-spec.md)
§7 for *why* the seam is shaped this way. This file is the *how*.

______________________________________________________________________

## 1. The five-minute version

```bash
# 1. write the connector
$EDITOR cl10n/providers/acme.py

# 2. declare it
cat >> cl10n/providers.toml <<'EOF'

[providers.acme]
connector          = "acme:AcmeTranslator"
default_model      = "acme/best-model-v2"
api_key_env        = "ACME_API_KEY"
api_key_creds_file = "acme-creds.txt"
base_url           = "https://api.acme.example/v1"
EOF

# 3. make sure the key file cannot be committed
grep -q 'creds' .gitignore || echo '*creds*.txt' >> .gitignore

# 4. prove it resolves without a key or a network call
venv/bin/python3 cl10n/queue_runner.py l10n/queue/queue.json \
  --provider acme --dry-run

# 5. spend one cent
echo 'ACME_API_KEY="..."' > acme-creds.txt
venv/bin/python3 cl10n/cli.py run --provider acme -c 1
```

Nothing else in the pipeline changes. No runner edit, no CLI edit, no test edit
beyond the ones you add for your own connector.

______________________________________________________________________

## 2. What a connector must provide

Two module-level names. That is the entire contract.

| Name | Kind | Contract |
| --- | --- | --- |
| a translator class | class | `__init__(self, model=None, ...)`, an attribute `.model`, and `async def translate(self, prompt: str) -> str` |
| `classify` | function | `classify(exc: BaseException) -> Failure` |

`translate` receives a fully-assembled prompt and returns **the translated text**
— not a completion object, not a stream, not a JSON envelope. Everything
provider-shaped stays inside your module.

### The translator

```python
class AcmeTranslator:
    def __init__(self, model=None, client=None, base_url=DEFAULT_BASE_URL,
                 max_tokens=4096, api_key_env=DEFAULT_API_KEY_ENV):
        self.model = model or DEFAULT_MODEL
        self._client = client          # injected by tests
        self.base_url = base_url
        self.max_tokens = max_tokens
        self.api_key_env = api_key_env

    @property
    def client(self):
        # LAZY — see section 4. Never build this in __init__.
        if self._client is None:
            import acme_sdk
            self._client = acme_sdk.AsyncClient(
                api_key=os.environ.get(self.api_key_env),
                base_url=self.base_url,
            )
        return self._client

    async def translate(self, prompt: str) -> str:
        completion = await self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=self.max_tokens,
        )
        return extract_translation(completion.choices[0].message.content or "")
```

`extract_translation` comes from `base` and is provider-agnostic: the shared
prompt asks for `{"translation": "..."}`, and it unwraps that tolerantly (bare
string, fenced block, any plausible key). **Always route the reply through it.**
Returning the raw content stores the JSON envelope in the translation memory as
if it were the translation, and nothing downstream will notice until a human
reads the rendered file.

Optional constructor parameters are opt-in: the registry passes `base_url` and
`api_key_env` **only if your `__init__` names them** (it introspects the
signature). A connector that needs neither simply omits them.

### `classify`

Map your library's exceptions onto the queue schema's error kinds, and delegate
everything else to `base.classify`:

```python
def classify(exc):
    if isinstance(exc, acme_sdk.RateLimitError):
        return Failure("rate_limit", _message(exc), True, _retry_after(exc))
    if isinstance(exc, acme_sdk.TimeoutError):
        return Failure("network", f"request timed out: {_message(exc)}", True)
    if isinstance(exc, acme_sdk.ConnectionError):
        return Failure("network", _message(exc), True)
    if isinstance(exc, acme_sdk.StatusError):
        status = getattr(exc, "status_code", 0) or 0
        retryable = status >= 500 or status in (408, 409, 429)
        return Failure(
            "rate_limit" if status == 429 else "api_error",
            f"HTTP {status}: {_message(exc)}",
            retryable,
            _retry_after(exc),
        )
    return _base_classify(exc)   # timeouts, OS errors, and the unknown case
```

The kinds are **not negotiable** — retries, the account-wide rate-limit gate and
the "terminal vs. retryable" decision all key off them:

| Condition | `kind` | Retryable |
| --- | --- | --- |
| HTTP 429 / quota exhausted | `rate_limit` | yes |
| HTTP 5xx, 408, 409 | `api_error` | yes |
| Connection loss, request timeout | `network` | yes |
| HTTP 401 / 403 / 400 / 404 / 422 | `api_error` | **no** |
| Anything unrecognised | `api_error` | **no** |

Unrecognised exceptions are terminal on purpose: an unknown exception is usually
a bug in the connector, and retrying a bug three times only bills for it three
times.

`_retry_after` reads a `Retry-After` header off `exc.response.headers` and works
with any httpx-based SDK. If your library exposes the wait somewhere else, return
it yourself — the runner floors its backoff with whatever you provide.

______________________________________________________________________

## 3. Declaring it in `providers.toml`

```toml
[providers.acme]
connector          = "acme:AcmeTranslator"   # module:Class, module is a file in cl10n/providers/
default_model      = "acme/best-model-v2"    # used when --model is bare or absent
api_key_env        = "ACME_API_KEY"          # required
api_key_creds_file = "acme-creds.txt"        # optional, gitignored
base_url           = "https://api.acme..."   # optional; omit to use the SDK default
```

| Key | Required | Meaning |
| --- | --- | --- |
| `connector` | yes | `module:Class`; a bare module name is a file in `cl10n/providers/` |
| `default_model` | yes | what a bare `--model` or no `--model` resolves to |
| `api_key_env` | yes | the environment variable holding the key |
| `api_key_creds_file` | no | a gitignored file `run` reads when the variable is unset |
| `base_url` | no | passed to the connector if its `__init__` accepts it |

Changing the top-level `default = "..."` changes which provider runs when nobody
passes `--provider`. Leave it alone unless you intend to switch the pipeline's
default, which re-routes CI too.

Users then select it three ways, in priority order: `--model acme:some/model`
(prefix wins), `--provider acme`, or the registry default.

______________________________________________________________________

## 4. Two invariants, and one trap that will cost you an hour

### Invariant 1 — the client is built lazily

Construct the SDK client on **first use**, never at import and never in
`__init__`. Most SDKs raise or misconfigure when the key is absent, and eagerly
building one makes the module unimportable on any machine without credentials —
including CI, where the whole test suite stubs the provider and no key exists.

The test for this is one line: `build_translator(cfg, "m")._client is None`.

### Invariant 2 — the runner never learns your provider's name

The runner holds a `Translator` and a `classify` and knows nothing else. If a
change requires an `if provider == "acme"` anywhere outside `cl10n/providers/`,
the difference belongs **inside your connector** instead.

### The trap — a connector must never be imported by module name

`cl10n/providers/groq.py` and the `groq` PyPI package share the top-level name
`groq`. That collision bites in two directions, and both cost real debugging
time during CLN-1:

1. **Putting `cl10n/providers/` on `sys.path`** makes `import groq` *inside the
   connector* find the connector itself — a circular import that only surfaces
   at first use.
2. **Resolving the connector with `importlib.import_module("groq")`** returns the
   *library*, because `queue_runner` has already imported it for its exception
   taxonomy, so it is always in `sys.modules`. The symptom is
   `module 'groq' has no attribute 'GroqTranslator'`.

Both are solved the same way and it is already done for you: **connector modules
and `base` are loaded from an explicit file path** under a `_cl10n_providers_*`
module key. So:

- reach `base` the way the shipped connectors do (copy their header verbatim);
- do **not** add the providers directory to `sys.path`;
- do **not** "simplify" the registry's loader back to `import_module`.
  `test_providers.py` has a regression test that fails if you do.

If your provider's SDK has a name that cannot collide, this costs you nothing —
follow the same pattern anyway, so the next connector inherits it.

______________________________________________________________________

## 5. What you must not do

- **Do not edit `queue_runner.py`.** Adding a provider never requires it. The
  runner already takes a `classify` parameter and a `Translator`.
- **Do not edit `cl10n/cli.py`.** `run` is a verbatim pass-through; your flags
  reach the runner untouched.
- **Do not add a second definition of the prompt.** `app/prompt.py` owns
  `TRANSLATION_PROMPT`, `PROMPT_VERSION` and `LANG_NAMES`, and they are
  deliberately provider-agnostic — see [section 7](#7-the-prompt-is-shared-on-purpose).
- **Do not bump `PROMPT_VERSION`** because you added a provider. The rules did
  not change; bumping it makes every existing entry eligible for re-translation.
- **Do not import your SDK at module scope** if it is a heavy or optional
  dependency — import it inside the client property, as `nvidia.py` does. A Groq
  run should not pay to import your library.

______________________________________________________________________

## 6. Testing it

The bar is `cl10n/tests/test_providers.py`, and the rule is absolute: **no test
may require an API key or make a network call.** Add, alongside the existing
ones:

```python
def test_acme_classify(...):        # the kinds table above, one case per row
def test_acme_translator_is_constructible_without_an_api_key(...):
def test_acme_translator_unwraps_the_envelope(...):   # inject a fake client
```

A fake client is five lines — that is the point of the narrow seam:

```python
class FakeCompletions:
    async def create(self, **kwargs):
        sent.update(kwargs)
        message = type("M", (), {"content": '{"translation": "שלום"}'})()
        return type("C", (), {"choices": [type("Ch", (), {"message": message})()]})()
```

Then check the registry route resolves and the whole suite still passes:

```bash
venv/bin/python3 -m pytest cl10n/tests/test_providers.py -q
venv/bin/python3 -m pytest -q
```

### Then verify against the real API, once

Stubs prove the wiring; only a real call proves the provider. Spend a few cents:

```bash
venv/bin/python3 cl10n/cli.py run --provider acme --dry-run   # confirm the count
venv/bin/python3 cl10n/cli.py run --provider acme -c 1        # a tiny queue
```

Check three things in the resulting `l10n/tm/<lang>.json`:

1. the translation is in the **target language** and reads correctly;
2. a unit whose source contained inline code or a link came back with it
   **byte-identical** — that is the placeholder gate passing on real output,
   which a stub cannot demonstrate;
3. `model` records what you actually called.

Then confirm the memory is shared: re-run the same queue routed to a *different*
provider and expect **0 API calls, all TM hits**.

______________________________________________________________________

## 7. The prompt is shared on purpose

Every connector sends the identical prompt from `app/prompt.py` and records the
identical `PROMPT_VERSION`. The translation memory is keyed by content hash plus
prompt version — **not by provider** — so a unit translated by one provider is
reused by a run routed to another.

That is what makes switching providers free rather than a full re-translation of
the corpus, and it is why the prompt does not live in the connector. If you find
yourself wanting a provider-specific prompt, you are proposing to fork the
memory; raise it as a design change rather than doing it quietly.

What *is* legitimately provider-specific: how the reply is coaxed into JSON. Groq
sends `response_format={"type": "json_object"}` because its models support it;
the NVIDIA connector sends nothing, because not every NIM model accepts JSON
mode, and relies on the prompt plus the tolerant extractor. Both end up handing
the runner a plain translated string.

______________________________________________________________________

## 8. Wiring it into CI

`.github/workflows/cl10n.yml` binds every declared provider's secret to the
**single** Execute step:

```yaml
        env:
          PROVIDER: ${{ github.event.inputs.provider || '' }}
          GROQ_API_KEY: ${{ secrets.GROQ_API_KEY }}
          NVIDIA_NIM_API_KEY: ${{ secrets.NVIDIA_NIM_API_KEY }}
          ACME_API_KEY: ${{ secrets.ACME_API_KEY }}      # <- add yours here
```

Add the repository secret under **Settings → Secrets and variables → Actions**.
One line in one step is the entire CI change.

Two properties must survive, and `cl10n/tests/test_workflow.py` asserts them:

- **exactly one step** ever holds a provider key. Never add a secret to a second
  step; the runner reads only the active provider's variable, so an unused key
  sits bound-but-unread.
- **no `pull_request` trigger**, so code from a fork never executes where the
  secrets are.

To make CI run your provider by default, set `PROVIDER: acme` in the workflow's
top-level `env:`; to run it once by hand, use the `provider` dispatch input.

______________________________________________________________________

## 9. Checklist

- [ ] `cl10n/providers/<name>.py` with a translator class and `classify`
- [ ] client built lazily; `_client is None` after construction
- [ ] reply routed through `extract_translation`
- [ ] `classify` covers 429 / 5xx / network / 4xx and delegates the tail to `base.classify`
- [ ] `[providers.<name>]` in `providers.toml` with `connector`, `default_model`, `api_key_env`
- [ ] creds filename matched by the `.gitignore` glob — check with `git check-ignore -v <file>`
- [ ] tests added; **full suite passes with no key and no network**
- [ ] one real API call verified: target language, placeholder intact, provenance right
- [ ] cross-provider TM reuse confirmed (0 API calls on a re-run via another provider)
- [ ] CI secret added to the one Execute step
- [ ] `queue_runner.py` and `cli.py` **untouched**
