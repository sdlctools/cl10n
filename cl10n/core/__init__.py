"""The provider-agnostic core: parsing, change detection, and the prompt.

These four modules were `app/` before the package existed, and they are the
part of the pipeline the hashing contract runs through:

| Module | What it is |
| --- | --- |
| `cl10n.core.utils` | `make_parser` and the Markdown ⇄ AST round-trip. **Every unit hash in every translation memory is taken over this configuration** — see `cl10n/compat_check.py`. |
| `cl10n.core.tree_diff` | Merkle hashing, segmentation into translation units, and the per-unit action plan. |
| `cl10n.core.prompt` | `TRANSLATION_PROMPT`, `PROMPT_VERSION`, `LANG_NAMES` — provider-agnostic, which is why the memory is shared across providers. |
| `cl10n.core.groq_api` | The original Groq helper; kept for its `DEFAULT_MODEL` and its re-exports of the prompt. |

Nothing here imports a provider SDK, and nothing here writes state.
"""
