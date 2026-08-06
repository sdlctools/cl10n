# Integrating `cl10n` into another project

How to add this localization pipeline to a repository that is not this one.

Every command and transcript below was produced by following this document from
an empty directory into a working Hebrew localization, against the live
provider. Where something is a caveat rather than a step, it is because it bit
during that run.

For the day-to-day interface once you are set up, see
[`USERGUIDE.md`](USERGUIDE.md). To point the pipeline at an LLM API none of the
shipped connectors covers, see [`PROVIDERS.md`](PROVIDERS.md).

______________________________________________________________________

## 1. Install it

`cl10n` is a package on PyPI. There is nothing to copy.

```bash
cd /path/to/your-project
python3 -m venv venv
venv/bin/pip install "cl10n[groq]"
```

That is the whole installation. It brings the pinned parsing stack, the
runtime, the provider registry and its connectors, and puts a **`cl10n`
console script** in `venv/bin/`.

Take the extra for the provider you route to — the connectors import their SDK
lazily, so the extras exist to save you downloading three vendors' clients to
use one:

| Extra | Installs | For |
| --- | --- | --- |
| `cl10n[groq]` | `groq` | the default provider |
| `cl10n[nvidia]` | `openai` | NVIDIA NIM's OpenAI-compatible endpoint (no OpenAI account) |
| `cl10n[mistral]` | `mistralai` | Mistral |
| `cl10n[all-providers]` | all three | when you switch between them, or don't know yet |

Add it to your project's own dependency file so the install is reproducible —
`requirements.txt`, `pyproject.toml`, whichever you use:

```
cl10n[groq]
```

**Pin `cl10n` itself if you pin anything.** The parsing stack inside it is
pinned exactly, because every unit hash in your translation memory is taken
over one specific parser configuration; a `cl10n` release that moved those pins
would move your hashes and re-translate your corpus at full price. Pinning
`cl10n==X.Y.Z` makes that a decision you take rather than one you receive.

When you *do* take a `cl10n` upgrade, the drift detector is the gate — it ships
in the wheel and needs no checkout:

```bash
venv/bin/python3 -m cl10n.compat_check
```

Green means the new version's parser agrees with the recorded baseline and your
memory is still valid. Red means the hashes moved: do not upgrade until you
understand why.

### What you do *not* install

**Not the tests.** `cl10n/tests/` is excluded from the wheel on purpose. Those
tests verify *the pipeline* against *its own repository* — its `md/` reference
corpus, its `.github/workflows/cl10n.yml`, a throwaway git repo per test.
Dropped into a project with different content they would not test your
integration, they would just fail. [Section 6](#6-verifying-your-setup) is how you
verify *your* setup instead.

**Not a corpus.** `md/`, `locales/` and `l10n/` in the upstream repository are
its own content. Yours are yours.

______________________________________________________________________

## 2. Vendoring — the fallback, not the path

Copying the source in still works, and there are two reasons to: an air-gapped
build with no PyPI access, or a fork whose connectors you are actively editing.
Neither is the normal case, and both cost you the upgrade path — you now
maintain a copy.

If you must:

```bash
UPSTREAM=/path/to/cl10n
PROJECT=/path/to/your-project

cp -r "$UPSTREAM/cl10n" "$PROJECT/cl10n"
rm -rf "$PROJECT/cl10n/tests"          # they test the upstream repo, not yours
```

The whole directory, because since the package layout everything the runtime
needs lives inside it: `cl10n/core/` (the parser, the segmenter, the prompt),
`cl10n/providers/` plus `providers.toml`, `cl10n/schemas/`,
`cl10n/compat-baseline.json` and `cl10n/fixtures/kitchen-sink.md`. Copying
`cl10n/*.py` takes none of those — it is a glob over files, and every one of
them is in a subdirectory or is not a `.py`. **Copy the directory, not the
glob.**

You still need the dependencies, and the parsing stack pins are load-bearing:

```
markdown-it-py==4.2.0
mdit-py-plugins==0.6.1
mdformat==1.0.0
mdformat-gfm==1.0.0
mdformat-frontmatter==2.1.2
linkify-it-py==2.1.0
groq          # only the provider(s) you route to
```

Every unit hash in your translation memory is taken over those six packages in
one specific configuration. A minor upgrade has twice taught markdown-it-py to
parse a construct mdformat cannot render — task lists, then GitHub alerts —
and a construct that cannot be rendered cannot be localized. A change that does
*not* raise is worse: it silently alters the canonical form, every hash moves,
and your next run re-translates the whole corpus at full price while orphaning
the memory you already paid for. Run `python3 -m cl10n.compat_check` before
moving any of them.

A vendored copy is imported as the package `cl10n` from your project root, so
`python3 -m cl10n.cli` works but the `cl10n` console script does not exist —
there is no installed distribution to provide it. Substitute
`venv/bin/python3 -m cl10n.cli` for `venv/bin/cl10n` everywhere below.

## 3. Set the project up

The pipeline assumes `venv/`, not `.venv/`, in its documentation and workflow —
if you use something else, adjust the interpreter path everywhere.

Add to `.gitignore`:

```gitignore
venv/
__pycache__/
l10n/queue/          # per-run state, never committed
*creds*              # provider keys — no extension filter, see below
```

The last two matter most. A committed queue file ships transient state and
causes exactly the merge conflicts the per-language memory files are designed to
avoid, and a committed key is a leaked key.

**Use a wide glob for the key files, not one filename per provider.** Each
provider declares its own creds file in `providers.toml`, and their names do not
share a separator — `groq_creds.txt`, but `nvidia-nim-creds.txt` and
`mistral-creds.txt`. A pattern matching only one style leaves the others
untracked but *unignored*, which is one `git add -A` away from publishing a key.

**And do not filter on `.txt`.** Editing a creds file leaves
`.mistral-creds.txt.swp` — a vim swap file holding the key in plain text that no
`*.txt` pattern matches. Both of these were near-misses during development,
which is why the pattern is just `*creds*`. Verify with
`git check-ignore -v <file>` rather than assuming.

**Your project must be a git repository.** The pipeline recovers each
document's previously localized revision through `git cat-file blob`. Without
git it still runs, but every document is planned against the empty document
for ever, so you never get the cheaper `REVISE` path.

## 4. Point it at your Markdown

Default corpus root is `md/`. Put your Markdown there, or keep it where it is
and pass `--md-root` to every command — see [section 8](#8-a-different-corpus-layout).

```bash
git add -A && git commit -m "add localization pipeline"
```

**Commit your corpus before you render.** See
[section 9](#9-why-committing-matters-precisely) for exactly what goes wrong if
you do not; it is subtler than "it breaks".

## 5. First localization

Supply the provider key, then run the same four commands you will run for ever
after. There is no initialization mode — a first localization is an incremental
update that happens to find everything missing.

The key you need is the one belonging to the provider you will run. With no
`--provider` flag that is the registry default, `groq`; `cl10n/providers.toml`
lists every declared provider and the environment variable each expects.

```bash
echo 'GROQ_API_KEY="gsk_..."' > groq_creds.txt
# or, to run NVIDIA instead:
#   echo 'NVIDIA_NIM_API_KEY="nvapi-..."' > nvidia-nim-creds.txt
#   ...and add --provider nvidia to the `run` command below

venv/bin/cl10n status --langs he    # 0%
venv/bin/cl10n plan   --langs he
venv/bin/cl10n run    --dry-run     # check the bill first
venv/bin/cl10n run    -c 4
venv/bin/cl10n render --langs he
venv/bin/cl10n status --langs he    # 100%
```

A real transcript, from the run that validated this document. The corpus was one
file containing a heading, two paragraphs, a link, an inline code span and a
two-column table — seven translation units:

```
$ venv/bin/cl10n status --langs he
1 document(s) under md, 7 translation unit(s)

he: 0/7 units translated (0.0%), 0 fallback(s), 1 document(s) needing a render
   * [not rendered] md/guide.md: 0/7

$ venv/bin/cl10n plan --langs he
PLAN 20260804T145625Z-82bded — 1 document(s), 1 language(s)
  TRANSLATE=7  REVISE=0  RECHECK=0  REUSE=0  COPY=0  RETIRE=0
  7 job(s) → l10n/queue/queue.json  {'he': 7}
  0 unit(s) already in the translation memory, 0 flagged for recheck
  render required: yes

$ venv/bin/cl10n run -c 4
7 jobs — 7 done, 0 rejected, 0 already terminal
8 API call(s), 0 translation-memory hit(s) in 29.5s
failures by kind: rate_limit=1

$ venv/bin/cl10n render --langs he
locales/he/guide.md [he]: 7/7 units translated

1 file(s) rendered across 1 language(s); 0 English fallback(s), 0 placeholder violation(s)

$ venv/bin/cl10n status --langs he
he: 7/7 units translated (100.0%), 0 fallback(s), 0 document(s) needing a render
     md/guide.md: 7/7
```

Note `8 API call(s)` for 7 jobs with `rate_limit=1`: one request was throttled
and retried automatically. That is the expected shape of a real run, not a
problem.

Then commit the three outputs **together**:

```bash
git add locales l10n/tm l10n/manifest.json
git commit -m "l10n: first Hebrew localization"
```

One commit, because the manifest asserts that a revision is localized;
committing it without the locale files it describes leaves the repository
claiming something the tree does not show.

## 6. Verifying your setup

Do not reach for the upstream unit tests — they are not in the wheel, and they
test the upstream repository. The pipeline verifies itself on your content in
three ways, and all three ran in the transcript above.

**The renderer verifies its own output.** `render` re-parses every file it
writes and refuses to write one whose block structure moved. A render that
reports `0 placeholder violation(s)` and no `STRUCTURE MISMATCH` is a
structural guarantee, not a hope.

**Read one rendered file.** This is the check worth doing by eye, once:

```markdown
# הצטרפות

הפעל `npm install` תחילה, ואז קרא את [הערות התקנה](https://example.com/setup).

| צעד | נדרש |
| -- | -- |
| התקן | כן |

הליך השחזור מתועד בנפרד.
```

Confirm the inline code span, the link URL, the table's column count and the
heading level all survived. They are preserved by construction — structure comes
from the source tree and is never re-derived from translated text — but seeing
it once is what makes that believable.

**Prove the incremental path.** This is the property the whole design exists
for, and it takes thirty seconds to demonstrate on your own corpus:

```bash
sed -i 's/documented separately/documented in the appendix/' md/guide.md
git commit -qam "edit one sentence"
venv/bin/cl10n plan --langs he
```

```
  TRANSLATE=0  REVISE=1  RECHECK=0  REUSE=6  COPY=0  RETIRE=0
  1 job(s) → l10n/queue/queue.json  {'he': 1}
  6 unit(s) already in the translation memory, 0 flagged for recheck
```

One job, not seven, and it is a `REVISE` carrying the old English and the old
Hebrew. `run` then reports `1 API call(s)`. If you see seven jobs here, your
manifest is not being read — check that you are running from the repository root
and passing the same `--manifest` every time.

## 7. Automating it with GitHub Actions

Copy `.github/workflows/cl10n.yml` from upstream and change five things:

| Setting | Where | Change to |
| --- | --- | --- |
| trigger branch | `on.push.branches` | your default branch |
| watched paths | `on.push.paths` | your corpus, e.g. `docs/**` |
| languages | `env.LANGS` | your language list |
| corpus root | the `plan` / `render` / `status` steps | add `--md-root docs` if not `md` |
| **install step** | `Install dependencies` | `venv/bin/pip install "cl10n[groq]"` — upstream installs the checkout it lives in (`.[all-providers]`), which is not what your repository holds |

Add the key for the provider your workflow runs as a repository secret under
**Settings → Secrets and variables → Actions** — `GROQ_API_KEY` for the default,
`NVIDIA_NIM_API_KEY` for NVIDIA. The workflow binds every declared provider's
secret to the single Execute step; a secret you have not created arrives as an
empty string and is simply never read, because the runner only consults the
active provider's variable. **Add only the ones you actually use.**

Leave these three alone unless you know exactly why you are changing them. Each
fails silently rather than loudly:

- **`permissions: actions: read`** — the crash-artifact recovery reads the
  previous run's artifact through the artifacts REST API. Declaring a
  `permissions` block sets every scope you do *not* list to `none`, so removing
  this line makes the recovery step 403 and report "no artifact", which is
  indistinguishable from the ordinary first-run case. Uploading keeps working,
  so the layer looks alive while contributing nothing.
- **`concurrency: {group: cl10n, cancel-in-progress: false}`** — two rapid
  pushes must queue rather than race for the same memory files.
- **no `pull_request` trigger** — the workflow must never run in a context that
  holds the provider key while executing code from a fork.

Trigger it once by hand with **workflow_dispatch** before relying on the push
trigger, so the first run is one you are watching. It opens a pull request from
the branch `cl10n/translations` rather than pushing to your default branch.

## 8. A different corpus layout

Nothing requires `md/` or `locales/`. For a docs site:

```bash
venv/bin/cl10n plan   --md-root docs --langs fr
venv/bin/cl10n run    -c 8
venv/bin/cl10n render --md-root docs --out-dir i18n --langs fr
venv/bin/cl10n status --md-root docs --out-dir i18n --langs fr
```

`docs/sub/intro.md` renders to `i18n/fr/sub/intro.md` — the mirror preserves
everything below the root.

Pass the same `--md-root` and `--out-dir` to **every** command in the cycle.
`status` looks for rendered files exactly where `render` would have written
them, so disagreeing flags make it report everything as `[not rendered]` while
the files sit happily on disk.

### Two corpus roots: share the manifest

If you localize **two separate roots** in one repository, they must share **one
manifest**. This is the opposite of what seems natural, and getting it wrong
destroys translations silently.

```bash
# Correct: one ledger, one memory, two roots.
venv/bin/cl10n render --md-root docs   --out-dir i18n/docs   --langs fr
venv/bin/cl10n render --md-root guides --out-dir i18n/guides --langs fr
```

Garbage collection deletes a memory entry when no file **in the ledger it was
given** still references its hash. `referenced` is the union over the *whole*
manifest, so with one shared manifest, re-rendering `docs` sees `guides`'
hashes and leaves them alone. Verified:

```
after both cycles, TM sources: ['Docs', 'Guides', 'Only in docs.', 'Only in guides.']
after re-rendering docs only:  ['Docs', 'Guides', 'Only in docs.', 'Only in guides.']
```

Give each root its own manifest while they share a memory and every render
collects the other root's translations. The same experiment, changing only that:

```
after both cycles, TM sources: ['Guides', 'Only in guides.']     ← docs already gone
after re-rendering docs only:  []                                 ← everything gone
```

`docs`' entries were collected the moment `guides` was rendered against a ledger
that had never heard of them. If you genuinely need separate manifests, give
each root its own `--tm-dir` as well so the two never share state, or pass
`--no-gc` and accept that dead entries accumulate.

## 9. Why committing matters, precisely

The usual advice is "commit your corpus first". That is right, but the reason is
narrower than it sounds, and knowing it saves you from chasing a non-problem.

**Editing without committing, then planning, is fine.** `plan` compares against
the last *localized* revision recorded in the manifest, not against your working
tree's git status. An uncommitted edit diffs correctly:

```
  TRANSLATE=0  REVISE=1  RECHECK=0  REUSE=6  COPY=0  RETIRE=0
```

**Rendering while uncommitted is what degrades.** `render` records
`git hash-object` of the file as it is on disk. That computes a SHA without
storing the object, so if the content was never committed the blob is not in the
object database:

```
manifest source_blob: d1c18880b460582a756398f22aed6b5b4ce19567
  blob is NOT in the object database (never committed)
```

The next `plan` cannot read it and falls back to the empty-document path — every
unit reads as new:

```
  TRANSLATE=7  REVISE=0  RECHECK=0  REUSE=0  COPY=0  RETIRE=0
  0 job(s) → l10n/queue/queue.json
```

Note `0 job(s)`. **This costs nothing** — the translation memory still holds
every unit, so nothing is re-translated and nothing is re-billed. This is the
degenerate case the pipeline is designed to survive, and it is also what happens
in a shallow clone or after a history rewrite.

What you lose is only the `REVISE` upgrade: on the *next* real edit, that unit
is a fresh `TRANSLATE` with no old source and no prior translation to guide the
model, so the result is less consistent with its neighbours. Commit before you
render and you keep it. Forget once and nothing is broken.

## 10. Checklist

Setup:

- [ ] `pip install "cl10n[<your provider>]"` into `venv/`, and `venv/bin/cl10n`
      runs
- [ ] `cl10n` added (and pinned) in your own dependency file
- [ ] `l10n/queue/` and a wide `*creds*` glob in `.gitignore`
- [ ] project is a git repository and the corpus is committed

Verification, in order:

- [ ] `status` runs and reports your documents with a plausible unit count
- [ ] `run --dry-run` shows the call count you expect before you spend anything
- [ ] one rendered file read by eye: code spans, links, tables, heading levels intact
- [ ] the one-sentence edit produces exactly one `REVISE` job

Shipping:

- [ ] `locales/`, `l10n/tm/` and `l10n/manifest.json` committed together
- [ ] workflow adapted, the secret for **your** provider added, first run
      triggered manually

## 11. Integration troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `cl10n: command not found` | the package is not installed in the environment you are calling from, or you vendored it (which provides no console script) | `venv/bin/pip install "cl10n[groq]"`, and call `venv/bin/cl10n`; vendored copies use `venv/bin/python3 -m cl10n.cli` |
| `ModuleNotFoundError: cl10n` | same, seen from a `python -m` invocation | as above |
| `ModuleNotFoundError: cl10n.core` / `cl10n.providers` | a vendored copy taken with `cp cl10n/*.py` — a file glob takes no subdirectories | copy the whole `cl10n/` directory — see [section 2](#2-vendoring--the-fallback-not-the-path) |
| `ModuleNotFoundError: groq` (or `openai`, `mistralai`) | the provider SDK is not installed — only `run` needs it, so `plan`/`render`/`status` look healthy first | install the matching extra: `pip install "cl10n[groq]"` |
| `no source markdown found under md` | corpus is elsewhere | `--md-root <dir>`, on every command |
| every document plans as new, every run | manifest missing, or a different `--manifest` per command | pass the same path everywhere; check `l10n/manifest.json` exists |
| everything shows `[not rendered]` | `--out-dir` differs between `render` and `status` | pass the same flags to both |
| `<KEY> is not set` | no env var, and no creds file for the **selected** provider, in the working directory | export it, or `run --creds-file <path>`; the name in the message is that provider's `api_key_env` |
| `unknown provider 'x'` | `--provider` or a `provider:` model prefix names something absent from `providers.toml` | the error lists what is declared |
| `compat_check` red after a `cl10n` upgrade | the new release moved the parsing stack, so every unit hash moved | pin the previous `cl10n`; do not re-record the baseline to make it pass |
| GC deleted another root's translations | two corpus roots with **separate** manifests sharing one memory | share **one** manifest across roots — see [section 8](#8-a-different-corpus-layout) |
