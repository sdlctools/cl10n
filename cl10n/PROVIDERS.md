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
venv/bin/python3 -m cl10n.queue_runner l10n/queue/queue.json \
  --provider acme --dry-run

# 5. spend one cent
echo 'ACME_API_KEY="..."' > acme-creds.txt
venv/bin/cl10n run --provider acme -c 1
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

### Do not assume your SDK looks like groq's

`groq` and `openai` share an exception hierarchy (groq's SDK is a fork), so the
first two connectors look almost identical. The third did not, and each
difference below was a real bug caught only by inspecting the installed package:

- **One error class instead of a hierarchy.** `mistralai` raises a single
  `SDKError` for every HTTP failure, so its `classify` branches on
  `status_code` rather than on the exception type.
- **`Retry-After` in a different place.** `SDKError` exposes `headers` and
  `raw_response` but **no `.response`** — so the shared `_retry_after` silently
  returns `None` and the connector must read the header itself. A test asserts
  the shared helper cannot see it, so the reason the override exists survives.
- **Transport errors that `base.classify` gets wrong.** Raw `httpx` exceptions
  (`ConnectError`, `ReadTimeout`, `RemoteProtocolError`) are **not** `OSError`
  subclasses, so the generic tail classifies them as terminal `api_error` and
  the job is rejected on its first blip. If your SDK lets httpx errors escape,
  match `httpx.TimeoutException` and `httpx.TransportError` explicitly.
- **A reply that is not a plain string.** Mistral's `content` is
  `Union[str, List[ContentChunk]]`. Passing a chunk list to
  `extract_translation` stores a Python repr in the translation memory —
  which renders as garbage and passes every other check. Flatten first.
- **A moved import path.** `mistralai` 2.x put the client at
  `mistralai.client`; `from mistralai import Mistral` raises. Check the
  installed package rather than trusting a README snippet.

The lesson generalises: **read the installed SDK, do not pattern-match on
`groq.py`.** `venv/bin/python3 -c "import x; help(x)"` and
`inspect.signature` answer these in a minute, and each one is a rejected job or
a corrupted memory entry if you guess.

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

### The former trap — a connector sharing its SDK's name

`cl10n/providers/groq.py` and the `groq` PyPI package share a leaf name, and
before `cl10n` was a package that collision bit in two directions and cost
real debugging time during CLN-1. Both were top-level modules competing for
one entry in `sys.modules`: putting `cl10n/providers/` on `sys.path` made
`import groq` *inside the connector* find the connector itself, and resolving
the connector with `importlib.import_module("groq")` returned the *library*
(already imported for its exception taxonomy), giving the memorable
`module 'groq' has no attribute 'GroqTranslator'`.

**The package layout ended it, and you inherit the fix by doing nothing
special.** Python 3's absolute-import rule means `import groq` inside
`cl10n.providers.groq` is unambiguously the top-level library, while the
connector is only ever reachable as `cl10n.providers.groq`. The two names
cannot alias. So a connector named after its SDK is now ordinary:

```python
import acme                                    # the SDK — the real one
from cl10n.providers.base import Failure, extract_translation
```

What this means for you:

- import `base` and your SDK as plain absolute imports, exactly as the three
  shipped connectors do;
- do **not** reintroduce `sys.path` mutation or `spec_from_file_location`
  loading — they were a workaround for a problem the layout removed;
- keep the registry resolving bare connector names against
  `cl10n.providers.<name>`. `test_providers.py` pins this: it imports the real
  `groq` library first, then asserts the registry still builds the connector
  *and* that the connector's own `groq` attribute is the library.

______________________________________________________________________

## 5. What you must not do

- **Do not edit `queue_runner.py`.** Adding a provider never requires it. The
  runner already takes a `classify` parameter and a `Translator`.
- **Do not edit `cl10n/cli.py`.** `run` is a verbatim pass-through; your flags
  reach the runner untouched.
- **Do not add a second definition of the prompt.** `cl10n/core/prompt.py` owns
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
venv/bin/cl10n run --provider acme --dry-run   # confirm the count
venv/bin/cl10n run --provider acme -c 1        # a tiny queue
```

Check three things in the resulting `l10n/tm/<lang>.json`:

1. the translation is in the **target language** and reads correctly;
2. a unit whose source contained inline code or a link came back with it
   **byte-identical** — that is the placeholder gate passing on real output,
   which a stub cannot demonstrate;
3. `model` records what you actually called.

Then confirm the memory is shared: re-run the same queue routed to a *different*
provider and expect **0 API calls, all TM hits**.

### Test a placeholder-dense unit, and read the failure kinds before judging

Plain prose passes on every model worth using, so it proves almost nothing. The
unit that discriminates is one mixing inline code, a relative markdown link,
`<ANGLE_KEYS>` and `$ARGUMENTS` — that is what real technical documentation
looks like, and it is where a weaker model silently drops a placeholder and gets
its job rejected.

**Do not read a rejection as a capability verdict until you have ruled out
throttling.** Measuring Mistral's models at `-c 2`, `mistral-large-latest`
rejected both jobs — but the summary read `placeholder_lost=2, rate_limit=4`,
and re-running serially at `-c 1` it passed. The rate limiting had eaten the
retry budget the corrective re-prompt needed. `mistral-small-latest` failed the
same way serially (9 placeholder failures in 10 calls), which *is* a capability
result.

So when a model looks bad: re-run with `-c 1` and a raised `--max-attempts`,
then compare `failures by kind`. `rate_limit` means your tier; `placeholder_lost`
alone means the model.

______________________________________________________________________

## 7. The prompt is shared on purpose

Every connector sends the identical prompt from `cl10n/core/prompt.py` and records the
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
          MISTRAL_API_KEY: ${{ secrets.MISTRAL_API_KEY }}
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          CLAUDE_CODE_OAUTH_TOKEN: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
          ACME_API_KEY: ${{ secrets.ACME_API_KEY }}      # <- add yours here
```

Add the repository secret under **Settings → Secrets and variables → Actions**.
One line in one step is the entire CI change.

### A wired secret is not the same as a provider you want CI to use

`anthropic_oauth` is bound above like every other declared provider — the test
below requires it — but CI keeps routing to Groq, deliberately.

The reason is **billing, not capability**, and the distinction matters because
the obvious guess is wrong in both directions:

- Its transport is the Claude Code CLI, which sounds like something a runner
  would lack. But `claude-agent-sdk` ships a **bundled `claude` binary inside
  its wheel** on the platforms that have one (the installed
  `manylinux_2_17_x86_64` wheel carries a ~295 MB executable), so on
  `ubuntu-latest` a plain `pip install cl10n[all-providers]` already puts a
  working CLI on disk. Verified, not assumed — check
  `claude_agent_sdk/_bundled/claude` before believing either story.
- On a platform with no bundled wheel, the CLI genuinely is absent and every
  job fails with a terminal CLI-not-found. So the failure mode is real; it is
  just **platform-dependent**, which is the worst kind to discover in CI.

What actually argues against it: this route spends a Claude Code
**subscription's** usage limits rather than metered API credit, and an
unattended run of hundreds of units is what those limits are least suited to.
Route CI here on purpose or not at all.

The general rule: if your SDK needs anything beyond `pip install` — or ships
something surprising *inside* the wheel — say so here and in the workflow.
Either way the failure surfaces inside a step whose secret is correctly
configured, which is where nobody looks first.

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
- [ ] CI secret added to the one Execute step (a test asserts every declared
      provider's `api_key_env` is wired there)
- [ ] `queue_runner.py` and `cli.py` **untouched**

______________________________________________________________________

## 10. Worked example: the five shipped connectors

The fastest way to write the sixth is to read the ones that exist, in this
order — they are deliberately different from each other:

| Connector | SDK | What it demonstrates |
| --- | --- | --- |
| `groq.py` | `groq` | the baseline: per-status exception classes, `response_format` JSON mode, a lazy client |
| `nvidia.py` | `openai` | an OpenAI-compatible endpoint via `base_url`; **no** `response_format`, because not every NIM model accepts JSON mode |
| `mistral.py` | `mistralai` | a native SDK that resembles neither: one `SDKError`, `Retry-After` in a non-standard place, httpx errors escaping, and a `content` union |
| `anthropic_api.py` | `anthropic` | a native SDK with the openai exception shape but a **content-block, not choices** reply that must be flattened, **no** `response_format`, and a **required** `max_tokens` — read the installed SDK, do not assume it looks like groq's |
| `anthropic_oauth.py` | `claude_agent_sdk` | **the transport is a CLI subprocess, not an HTTP client** — `query()` spawns the Claude Code CLI. No `base_url`, no `api_key=`, no response object. Copy this one for any future non-HTTP provider |

`mistral.py` is the one to copy if your provider has its own SDK; `nvidia.py` if
it is OpenAI-compatible; `anthropic_api.py` if it is OpenAI-shaped but returns
content blocks rather than a single string; `anthropic_oauth.py` if it is not
an HTTP API at all.

### What the non-HTTP one has to solve that the others don't

Worth reading even if your provider *is* an HTTP API, because each of these is
a class of problem the first four connectors never meet:

- **The reply is a stream of message objects, not a completion.** `query()` is
  an async generator; the connector aggregates the `TextBlock`s into one string
  before returning, which the `Translator` protocol explicitly allows.
- **Errors arrive on two different paths.** Some are raised (`CLINotFoundError`
  from the spawn); others are *yielded* into the stream (a rejected
  `RateLimitEvent`, an `AssistantMessage.error`). The yielded ones are wrapped
  in an exception so `classify` — and therefore the runner's rate-limit gate —
  can act on them at all.
- **The typed exception can be erased in flight.** A failure mid-stream is
  round-tripped through the SDK's message channel and re-raised as a bare
  `Exception`, losing its class. The connector matches `type(exc) is Exception`
  *exactly*: a subclass is an unrecognised error and must stay terminal, or a
  bug in the connector gets retried three times.
- **A rate limit has no `Retry-After`.** `RateLimitInfo.resets_at` is a Unix
  timestamp, converted to seconds-from-now, and `None` when absent so the
  runner's own backoff carries it.
- **Concurrency means processes, not sockets.** `-c 8` would be eight Node
  runtimes, so the ceiling is enforced by a semaphore **inside the connector**
  — never by teaching the runner a provider's name (§5, spec §7 invariant 2).
- **A credential that shadows another.** `ANTHROPIC_API_KEY` in the environment
  makes the CLI bill the metered API instead of the subscription, silently. The
  connector refuses to run rather than scrubbing it: `options.env` merges over
  the inherited environment and cannot express a deletion, so a scrub would be
  a guess about how the CLI reads an empty string.
