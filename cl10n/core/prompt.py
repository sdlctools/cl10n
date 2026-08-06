"""Provider-agnostic translation prompt and the constants that version it.

These were born in `cl10n/core/groq_api.py` (the Groq connector) but are not
Groq-specific — every connector sends the same prompt and records the same
`PROMPT_VERSION` against each TM entry. They live here so a connector module
imports only what is actually provider-shaped (its client, its exception
taxonomy), and the shared rules are impossible to fork between providers.

`groq_api.py` re-exports these for backward compatibility — tests and any
ad-hoc code that imported them from there keep working — but the canonical
import is this module.
"""

# The translation prompt template. Its rules (CRITICAL RULES) are the bit that
# `PROMPT_VERSION` versions; the runner wraps per-job payload (heading-trail
# context, the REVISE old-source/prior-translation pair, the corrective retry
# instruction) around it, which is payload, not rules, and does not bump the
# version.
TRANSLATION_PROMPT = """You are an expert technical document translator. Your task is to translate natural language prose blocks into target languages while strictly preserving all technical syntax, formatting, and variables.

CRITICAL RULES:
1. Translate ONLY the natural language prose/text intended for human reading.
2. DO NOT translate, modify, or remove any of the following elements under any circumstances:
   - Variable placeholders and bracketed keys (e.g., <PROJECT-KEY>, <DEFAULT_BASE_BRANCH>, $ARGUMENTS, ${{CLAUDE_PLUGIN_ROOT}}).
   - File paths, directory references, and script names (e.g., jira.sh, jira.ps1, SKILL.md, ../_shared/project-config.md).
   - CLI commands, options, and flags (e.g., --role assigner, --project, git branch, git worktree add).
   - Inline tags, code wrappers, or formatting tokens (e.g., <code_inline>, markdown backticks, bold/italic markers if embedded).
3. Maintain the technical tone, professional context, and precise meaning of the original documentation.
4. Return your output strictly as a valid JSON object matching the requested schema without adding conversational filler.

Translate the following English text into {target_lang}:

{text_to_translate}
"""

# Version of `TRANSLATION_PROMPT`'s rules above, recorded on every translation
# memory entry (l10n-pipeline-spec §3). Bump it when the CRITICAL RULES change
# — that is what makes older entries eligible for a refresh run. The connector
# a translation was made through does not enter this version: the rules are
# identical across providers, so a Groq translation and an NVIDIA translation
# at the same `PROMPT_VERSION` are interchangeable and a TM shortcut fires
# across them. Per-job framing the runner wraps around this template
# (heading-trail context, the REVISE pair, the corrective retry instruction)
# is payload, not rules, and does not bump this.
PROMPT_VERSION = "v1"

# Language code → human name, for the prompt's "translate into {target_lang}"
# line. A connector uses it to make the prompt; it is not a closed enum of the
# languages the pipeline targets (those are the caller's choice).
LANG_NAMES = {
    "he": "Hebrew",
    "ru": "Russian",
}
