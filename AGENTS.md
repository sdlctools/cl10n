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

Not built yet: reassembly (splicing translated `inline` content back into the
tree and rendering through `ast_to_markdown`).

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
