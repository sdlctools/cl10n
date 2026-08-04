# Integrating `cl10n` into another project

How to vendor this localization pipeline into a repository that is not this one.

Every command and transcript below was produced by following this document from
an empty directory into a working Hebrew localization, against the live
provider. Where something is a caveat rather than a step, it is because it bit
during that run.

For the day-to-day interface once you are set up, see
[`USERGUIDE.md`](USERGUIDE.md).

______________________________________________________________________

## 1. What you are actually copying

The pipeline is **eight runtime modules plus three engine modules**. It is not
a package; the modules are bare scripts that put their own directory and `app/`
on `sys.path`, so copying files is the installation procedure.

| Copy | From | Why |
| --- | --- | --- |
| `cl10n/*.py` | `cl10n/` | the runtime: CLI, runner, reassembly, ledger, store |
| `app/tree_diff.py` | `app/` | change detection: segmentation and Merkle hashing |
| `app/utils.py` | `app/` | the canonicalisation round-trip every hash is taken over |
| `app/groq_api.py` | `app/` | provider client and the translation prompt |
| `requirements.txt` | root | dependencies |

**Do not copy `cl10n/tests/`.** This is the one instruction people get wrong.
Those tests verify *the pipeline* against *this repository* — its `md/`
reference corpus, its `app/schemas/` contracts, its
`.github/workflows/cl10n.yml`. Dropped into a project with different content
they do not test your integration, they just fail. Running the suite in a fresh
project after copying only the runtime gives:

```
8 failed, 110 passed, 36 errors
```

with the errors resolving to four missing things that belong to the upstream
repository, not to yours:

| Missing | Errors | What it is |
| --- | --- | --- |
| `md/skills/**` | 24 | the upstream reference corpus the tests localize |
| `.github/workflows/cl10n.yml` | 9 | the workflow contract tests |
| `app/schemas/*.schema.json` | 6 | JSON Schema contracts — **tests only**, never read at runtime |
| `pytest.ini` | 1 | sets `asyncio_mode = auto` |

If you want to run the suite, run it in a checkout of the upstream repository,
which is where it means something. [Section 6](#6-verifying-the-copy) covers how
to verify *your* copy instead.

Also optional, and safe to delete: `cl10n/build_queue.py` and
`cl10n/pseudo_tm.py` are development scaffolding, not pipeline components.

______________________________________________________________________

## 2. Copy the pipeline in

```bash
UPSTREAM=/path/to/markdown-localization
PROJECT=/path/to/your-project

mkdir -p "$PROJECT"/{cl10n,app,md}
cp "$UPSTREAM"/cl10n/*.py        "$PROJECT/cl10n/"
cp "$UPSTREAM"/app/tree_diff.py  "$PROJECT/app/"
cp "$UPSTREAM"/app/utils.py      "$PROJECT/app/"
cp "$UPSTREAM"/app/groq_api.py   "$PROJECT/app/"
cp "$UPSTREAM"/requirements.txt  "$PROJECT/"
```

If your project already has a `requirements.txt`, merge rather than overwrite —
and **keep the exact pins**, they are load-bearing:

```
markdown-it-py==4.2.0
mdit-py-plugins==0.6.1
mdformat==1.0.0
mdformat-gfm==1.0.0
mdformat-frontmatter==2.1.2
linkify-it-py==2.1.0
groq
```

Every unit hash in your translation memory is taken over these five packages in
one specific configuration. A minor upgrade has twice taught markdown-it-py to
parse a construct mdformat cannot render — task lists, then GitHub alerts —
and a construct that cannot be rendered cannot be localized. A change that does
*not* raise is worse: it silently alters the canonical form, every hash moves,
and your next run re-translates the whole corpus at full price while orphaning
the memory you already paid for.

So also copy `cl10n/compat_check.py`, `cl10n/compat-baseline.json` and
`cl10n/tests/fixtures/kitchen-sink.md` — the drift detector is the gate for
ever moving one of those pins:

```bash
venv/bin/python3 cl10n/compat_check.py
```

This is the one part of `cl10n/tests/` worth taking with you; the rest tests
the upstream repository (see [section 1](#1-what-you-are-actually-copying)).

`pytest`, `pytest-asyncio`, `jsonschema` and `pyyaml` are for the upstream test
suite only — skip them if you are not copying the tests.

## 3. Set the project up

```bash
cd "$PROJECT"
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

The pipeline assumes `venv/`, not `.venv/`, in its documentation and workflow —
if you use something else, adjust the interpreter path everywhere.

Add to `.gitignore`:

```gitignore
venv/
__pycache__/
l10n/queue/          # per-run state, never committed
groq_creds.txt       # provider key
```

Both entries matter. A committed queue file ships transient state and causes
exactly the merge conflicts the per-language memory files are designed to
avoid, and a committed key is a leaked key.

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

```bash
echo 'GROQ_API_KEY="gsk_..."' > groq_creds.txt

venv/bin/python3 cl10n/cli.py status --langs he    # 0%
venv/bin/python3 cl10n/cli.py plan   --langs he
venv/bin/python3 cl10n/cli.py run    --dry-run     # check the bill first
venv/bin/python3 cl10n/cli.py run    -c 4
venv/bin/python3 cl10n/cli.py render --langs he
venv/bin/python3 cl10n/cli.py status --langs he    # 100%
```

A real transcript, from the run that validated this document. The corpus was one
file containing a heading, two paragraphs, a link, an inline code span and a
two-column table — seven translation units:

```
$ venv/bin/python3 cl10n/cli.py status --langs he
1 document(s) under md, 7 translation unit(s)

he: 0/7 units translated (0.0%), 0 fallback(s), 1 document(s) needing a render
   * [not rendered] md/guide.md: 0/7

$ venv/bin/python3 cl10n/cli.py plan --langs he
PLAN 20260804T145625Z-82bded — 1 document(s), 1 language(s)
  TRANSLATE=7  REVISE=0  RECHECK=0  REUSE=0  COPY=0  RETIRE=0
  7 job(s) → l10n/queue/queue.json  {'he': 7}
  0 unit(s) already in the translation memory, 0 flagged for recheck
  render required: yes

$ venv/bin/python3 cl10n/cli.py run -c 4
7 jobs — 7 done, 0 rejected, 0 already terminal
8 API call(s), 0 translation-memory hit(s) in 29.5s
failures by kind: rate_limit=1

$ venv/bin/python3 cl10n/cli.py render --langs he
locales/he/guide.md [he]: 7/7 units translated

1 file(s) rendered across 1 language(s); 0 English fallback(s), 0 placeholder violation(s)

$ venv/bin/python3 cl10n/cli.py status --langs he
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

## 6. Verifying the copy

Do not reach for the upstream unit tests. The pipeline verifies itself on your
content in three ways, and all three ran in the transcript above.

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
venv/bin/python3 cl10n/cli.py plan --langs he
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

Copy `.github/workflows/cl10n.yml` from upstream and change four things:

| Setting | Where | Change to |
| --- | --- | --- |
| trigger branch | `on.push.branches` | your default branch |
| watched paths | `on.push.paths` | your corpus, e.g. `docs/**` |
| languages | `env.LANGS` | your language list |
| corpus root | the `plan` / `render` / `status` steps | add `--md-root docs` if not `md` |

Add `GROQ_API_KEY` as a repository secret under **Settings → Secrets and
variables → Actions**.

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
venv/bin/python3 cl10n/cli.py plan   --md-root docs --langs fr
venv/bin/python3 cl10n/cli.py run    -c 8
venv/bin/python3 cl10n/cli.py render --md-root docs --out-dir i18n --langs fr
venv/bin/python3 cl10n/cli.py status --md-root docs --out-dir i18n --langs fr
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
venv/bin/python3 cl10n/cli.py render --md-root docs   --out-dir i18n/docs   --langs fr
venv/bin/python3 cl10n/cli.py render --md-root guides --out-dir i18n/guides --langs fr
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

- [ ] `cl10n/*.py` and the three `app/` modules copied; `cl10n/tests/` **not** copied
- [ ] dependencies merged into `requirements.txt` and installed into `venv/`
- [ ] `l10n/queue/` and `groq_creds.txt` in `.gitignore`
- [ ] project is a git repository and the corpus is committed

Verification, in order:

- [ ] `status` runs and reports your documents with a plausible unit count
- [ ] `run --dry-run` shows the call count you expect before you spend anything
- [ ] one rendered file read by eye: code spans, links, tables, heading levels intact
- [ ] the one-sentence edit produces exactly one `REVISE` job

Shipping:

- [ ] `locales/`, `l10n/tm/` and `l10n/manifest.json` committed together
- [ ] workflow adapted, `GROQ_API_KEY` secret added, first run triggered manually

## 11. Integration troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `ModuleNotFoundError: tree_diff` | `app/` modules not copied, or `cl10n/` moved away from its sibling `app/` | keep the `cl10n/` + `app/` layout; the path prelude assumes it |
| `no source markdown found under md` | corpus is elsewhere | `--md-root <dir>`, on every command |
| every document plans as new, every run | manifest missing, or a different `--manifest` per command | pass the same path everywhere; check `l10n/manifest.json` exists |
| everything shows `[not rendered]` | `--out-dir` differs between `render` and `status` | pass the same flags to both |
| `GROQ_API_KEY is not set` | no env var, and no `groq_creds.txt` in the working directory | export it, or `run --creds-file <path>` |
| upstream tests fail after copying | they test the upstream repo, not yours | don't copy them — see [section 1](#1-what-you-are-actually-copying) |
| `pytest` reports `async def functions are not natively supported` | `pytest.ini` with `asyncio_mode = auto` not copied | only relevant if you copied the tests |
| GC deleted another root's translations | two corpus roots with **separate** manifests sharing one memory | share **one** manifest across roots — see [section 8](#8-a-different-corpus-layout) |
