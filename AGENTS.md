# AGENTS.md

This repo is home for resarch scripts on markdown styled content translation, and continous localization.

We're normalizing markdown - converting it to ast tree, then detecting which branches or leaves need translation.
There is n working pipeline, our task is to research this operation.


## Scripting languages
* bash (globally available)
* python3 (use `venv/bin/python3` as interpreter — the venv is `venv/`, not `.venv/`)


## folders structure
* demos/  - some intermediate test scripts
* md - source markdown files (mostly taken from other projects)
* app - working examples, this where you put new files
* cl10n/ - the continuous-localization runtime (queue runner, state store,
  reassembly/render, its tests). New pipeline components go here, not in app/,
  so the runtime stays reviewable on its own.
* locales/ - rendered translations, `locales/<lang>/` mirroring `md/` (committed)
* l10n/ - pipeline state: `tm/<lang>.json` and `manifest.json` committed,
  `queue/` gitignored
* .claude/rules/ - design specs, auto-loaded when their `paths:` are touched


## change detection (which branches need translation)

`app/tree_diff.py` is the working implementation: it Merkle-hashes both AST
revisions, aligns each sibling level with LCS, and emits per-translation-unit
actions (REUSE / RECHECK / REVISE / TRANSLATE / RETIRE, plus COPY for changed
code fences and other opaque blocks) with heading-trail context and the inline
placeholders that must survive translation.

```bash
venv/bin/python3 app/tree_diff.py OLD.md NEW.md
```

The reasoning behind it — why tree edit distance is the wrong tool here, why the
translation unit is the smallest block owning an `inline` child rather than the
nearest changed parent, why XML belongs in the LLM payload but not in the diff,
and which fields must stay out of the hash — is in
[`.claude/rules/tree-diff-spec.md`](.claude/rules/tree-diff-spec.md). Read it
before changing hashing, segmentation, or the similarity thresholds.

## localization pipeline (spec + data contracts)

The full-pipeline architecture — TM lookup, queue, execution, placeholder
gate, reassembly, render, git policy — is specified in
[`.claude/rules/l10n-pipeline-spec.md`](.claude/rules/l10n-pipeline-spec.md),
with the three JSON data contracts (translation memory, queue, manifest) in
`app/schemas/*.schema.json` and validated worked examples from the real `md/`
corpus in `app/schemas/examples/`. Pipeline components must implement against
those schemas.

## cl10n/ — the localization runtime (step 7: executing the queue)

`cl10n/queue_runner.py` takes a queue file and drives it to completion against
Groq: bounded concurrency, per-job retry with exponential backoff and jitter,
an account-wide rate-limit gate, the placeholder gate, and translation-memory
writes with provenance. It resumes from wherever a previous run stopped —
kill it at any point and re-run it; `done` and `rejected` jobs are never
re-billed.

```bash
venv/bin/python3 cl10n/queue_runner.py l10n/queue/queue.json --dry-run  # plan only, no API
venv/bin/python3 cl10n/queue_runner.py l10n/queue/queue.json -c 8
venv/bin/python3 cl10n/build_queue.py --langs he,ru -o l10n/queue/queue.json
```

`GROQ_API_KEY` comes from the environment, or from `groq_creds.txt`
(gitignored) via `--creds-file`. `cl10n/l10n_store.py` holds the atomic-write,
queue and TM primitives; `cl10n/build_queue.py` is **dev scaffolding** that
plans the corpus against the empty document — it is not the pipeline's real
enqueue step, which belongs to its own sub-task.

The design — write orderings, resumption, retry classification, why the
rate-limit gate is account-wide rather than per-job, when `PROMPT_VERSION`
may be bumped, and what must not move into `cl10n/` — is in
[`.claude/rules/cl10n-runner-spec.md`](.claude/rules/cl10n-runner-spec.md).
Read it before changing concurrency, retries, persistence, or prompts.

Measured on the real corpus (30 units, `he`, free-tier account at 8000 TPM):
232s serially vs 156s at `-c 8`, and the shared rate-limit gate took the run
from 4 jobs lost to throttling down to 0.

## cl10n/ — reassembly and rendering (steps 10-11)

`cl10n/reassemble.py` turns a source document plus the translation memory into
`locales/<lang>/<mirrored path>`. It canonicalises the source, parses it once,
and replaces **only** each translation unit's `inline` content — so heading
levels, list nesting, table shape and code fences come from the source tree and
cannot be corrupted by a translation. A unit with no usable entry renders as
English (no entry, an empty translation, or one that lost a placeholder), and
every one of those is counted in the report rather than shipped silently. Each
render re-parses its own output and refuses to write a file whose block
structure moved.

```bash
venv/bin/python3 cl10n/reassemble.py --langs he,ru                    # md/**/*.md → locales/
venv/bin/python3 cl10n/reassemble.py --langs he md/skills/x/SKILL.md
venv/bin/python3 cl10n/reassemble.py --langs he,ru --dry-run --report l10n/render.json
```

`cl10n/placeholders.py` holds the placeholder-integrity rule, enforced both by
the runner (before a TM write) and here (before a splice).
`cl10n/pseudo_tm.py` is **dev scaffolding**: it writes a pseudolocalized TM so
the whole corpus can be rendered in `he` and `ru` without an API key — point it
at a scratch directory, never at a real `l10n/tm/`.

The design — why the splice is the only mutation, why the fallback is the
*absence* of one, the render-time placeholder gate, the table-cell newline
hazard and why RTL needed no special handling — is in
[`.claude/rules/cl10n-reassembly-spec.md`](.claude/rules/cl10n-reassembly-spec.md).

## cl10n/ — the orchestrator CLI and the CI workflow (steps 3-6, 10-12)

`cl10n/cli.py` is the pipeline's single entry point — the same four
subcommands for a human and for CI, and no bootstrap mode: a first-time
translation is an incremental update whose previous revision recovers empty.

```bash
venv/bin/python3 cl10n/cli.py plan   --langs he,ru      # manifest + git blobs → queue
venv/bin/python3 cl10n/cli.py run    l10n/queue/queue.json -c 8
venv/bin/python3 cl10n/cli.py render --langs he,ru      # TM → locales/, manifest, RETIRE GC
venv/bin/python3 cl10n/cli.py status --langs he,ru      # coverage per language
```

`plan` is the real enqueue step: it recovers each document's last-localized
revision through `l10n/manifest.json` + `git cat-file blob`, diffs, and lets
the **translation memory decide** what becomes a job — so nothing is ever
translated twice, and a rejected unit (English fallback) is re-enqueued until
it lands. `cl10n/manifest.py` owns the ledger and RETIRE garbage collection;
`cl10n/ci_report.py` renders the run reports into the PR body.

`.github/workflows/cl10n.yml` runs `plan → run → render` on every push to the
default branch that touches `md/**`, and opens/updates a PR from the fixed
branch `cl10n/translations` — never a direct push. An interrupted or
rate-limited run loses nothing: the resume state is the translation memory
(committed, plus the open PR branch and a crash artifact, folded in with
`plan --restore-tm`), and the next run pays only for what is missing.

The design — why the TM rather than the queue is the resume state, the
enqueue rule, the workflow's concurrency and secrets decisions — is in
[`.claude/rules/cl10n-cli-spec.md`](.claude/rules/cl10n-cli-spec.md).

[`cl10n/USERGUIDE.md`](cl10n/USERGUIDE.md) is the long-form companion: every
flag of every subcommand, real output with the numbers explained, and the
worked flows — first localization, adding a language, day-to-day updates,
integrating the pipeline into another project, plus a cookbook and a
troubleshooting table. Read the spec for *why*, the guide for *how*.

## tests

```bash
venv/bin/python3 -m pytest                                            # full suite
venv/bin/python3 -m pytest cl10n/tests/test_queue_runner.py -k NAME   # one test
```

Provider access is stubbed throughout — no test needs an API key, and none
makes a network call. The reassembly tests run against the real `md/` corpus
with a pseudolocalized memory, so they cover both target languages end to end.
The orchestrator tests build a real throwaway git repo per test and drive the
full `plan → run → render` cycle, including a mid-run kill and resume.

## python libs in use
W're dealing with complex markdown structires (gfm compatible) and  hardly rely on mdformat, merkdown-it-py packages, and their plugins.. See how to process markdown to ast and vice versa.
For required libs see `requirements.txt`

```python

from markdown_it import MarkdownIt
from mdformat.renderer import MDRenderer
import mdformat.plugins




def markdown_to_ast(raw_markdown) -> str:
    """
    Parses Markdown into AST tokens.
    """
    # 1. Initialize parser and the required plugin list
    md = MarkdownIt("gfm-like2")
    md.options["linkify"] = False
    md.options["parser_extension"] = []
    
    

    # 2. Dynamically load EVERY installed mdformat plugin (GFM, tables, frontmatter, etc.)
    for plugin in mdformat.plugins.PARSER_EXTENSIONS.values():
        if plugin not in md.options["parser_extension"]:
            md.options["parser_extension"].append(plugin)
            plugin.update_mdit(md)

    # 3. Generate the AST tokens
    tokens = md.parse(raw_markdown)
    return tokens


def ast_to_markdown(tokens) -> str:
    """
    Parses Markdown into AST tokens.
    """
    # 1. Initialize parser and the required plugin list
    md = MarkdownIt("gfm-like2")
    md.options["linkify"] = False
    md.options["parser_extension"] = []

    # 2. Dynamically load EVERY installed mdformat plugin (GFM, tables, frontmatter, etc.)
    for plugin in mdformat.plugins.PARSER_EXTENSIONS.values():
        if plugin not in md.options["parser_extension"]:
            md.options["parser_extension"].append(plugin)
            plugin.update_mdit(md)

    # 3. Generate the AST tokens
    
    options = dict(md.options)
     #options["mdformat"] = {"wrap": "keep"}
     #options["mdformat"] = {"wrap": 80}
 
    options["mdformat"] = {
         "number": True,  # Enables consecutive numbering for ordered lists
         "wrap": "keep",  # Retains your semantic line breaks
         "compact_tables": True,
         #"linkify" : False
    }
 
 
     
    # NOTE: Do NOT overwrite options["parser_extension"] here!

    # 5. Render AST directly back to Markdown (NO HTML!)
    renderer = MDRenderer()
    final_markdown = renderer.render(tokens, options, {})

    return final_markdown


def normalize_markdown(src, dst) -> str:
    with open(src, "r", encoding="utf-8") as f:
        raw_markdown = f.read()

    final_markdown = ast_to_markdown(markdown_to_ast(raw_markdown))
    #final_markdown = make_tables_compact(final_markdown)

    # 6. Save to disk
    with open(dst, "w", encoding="utf-8") as f:
        f.write(final_markdown)

```
