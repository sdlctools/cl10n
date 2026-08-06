import os
import json
import asyncio
from typing import List, Dict, Any
from groq import AsyncGroq

# The provider-agnostic prompt, its version, and the language-name table live
# in `cl10n/core/prompt.py` (the pluggable-provider home, CLN-1). They are
# re-exported here so existing imports (`groq_api.PROMPT_VERSION`, etc.) keep
# working, but the canonical import is `cl10n.core.prompt`.
from cl10n.core.prompt import (  # noqa: F401  (re-export)
    TRANSLATION_PROMPT,
    PROMPT_VERSION,
    LANG_NAMES,
)

# The Groq client is built on first use, not at import: AsyncGroq raises when
# GROQ_API_KEY is unset, which would make this module unimportable on a machine
# without credentials — including CI, where the runner's tests stub the provider
# and must never need a live key.
_client: AsyncGroq | None = None


def get_client() -> AsyncGroq:
    global _client
    if _client is None:
        _client = AsyncGroq(api_key=os.environ.get("GROQ_API_KEY"))
    return _client

# Default model for the Groq connector. Mirrors the value in
# `cl10n/providers.toml` under `[providers.groq] default_model`; kept here too
# because this module predates the registry and the demo functions below use it.
DEFAULT_MODEL = "openai/gpt-oss-120b"

async def translate_text(text: str, target_lang: str) -> str:
    """
    Translate a single text block using Groq API.
    
    Args:
        text: The English text to translate
        target_lang: Target language code (he, ru)
    
    Returns:
        Translated text string
    """
    lang_name = LANG_NAMES.get(target_lang, target_lang)
    
    try:
        completion = await get_client().chat.completions.create(
            model=DEFAULT_MODEL,  # Best for translation tasks
            messages=[
                {
                    "role": "user",
                    "content": TRANSLATION_PROMPT.format(
                        target_lang=lang_name,
                        text_to_translate=text
                    )
                }
            ],
            temperature=0.1,  # Low temperature for consistent translations
            max_tokens=4096,
        )
        return completion.choices[0].message.content.strip()
    except Exception as e:
        print(f"Error translating to {target_lang}: {e}")
        return f"[TRANSLATION ERROR: {e}]"

async def translate_item(item: Dict[str, Any]) -> Dict[str, Any]:
    """
    Translate a single queue item into all missing languages.
    
    Args:
        item: Queue item with 'text_en' and 'missing_langs' fields
    
    Returns:
        Updated item with translations added
    """
    text_en = item.get("text_en", "")
    missing_langs = item.get("missing_langs", [])
    
    # Create a copy to avoid modifying the original
    translated_item = item.copy()
    
    # Translate to each missing language
    for lang in missing_langs:
        if lang in LANG_NAMES:
            translated_text = await translate_text(text_en, lang)
            translated_item[f"text_{lang}"] = translated_text
        else:
            print(f"Warning: Unsupported language code '{lang}' for item {item.get('hash', 'unknown')}")
            translated_item[f"text_{lang}"] = f"[UNSUPPORTED LANGUAGE: {lang}]"
    
    return translated_item

async def process_translation_file(input_file: str, output_file: str = None):
    """
    Process the entire translation file.
    
    Args:
        input_file: Path to input JSON file
        output_file: Path to output JSON file (optional, defaults to input_file + '.translated.json')
    """
    # Read input file
    with open(input_file, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    # Process each item in the queue
    translated_queue = []
    total_items = len(data.get("queue", []))
    
    print(f"Processing {total_items} items for translation...")
    
    for idx, item in enumerate(data.get("queue", []), 1):
        print(f"Translating item {idx}/{total_items} (hash: {item.get('hash', 'unknown')})")
        translated_item = await translate_item(item)
        translated_queue.append(translated_item)
    
    # Create output data structure
    output_data = {
        "status": data.get("status", "pending"),
        "queue": translated_queue
    }
    
    # Write output file
    if output_file is None:
        output_file = input_file.replace('.json', '.translated.json')
    
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)
    
    print(f"\n✅ Translation complete! Output saved to: {output_file}")
    return output_data

async def process_single_item(text: str, target_langs: List[str]) -> Dict[str, str]:
    """
    Utility function to translate a single text to multiple languages.
    
    Args:
        text: English text to translate
        target_langs: List of target language codes
    
    Returns:
        Dictionary mapping language codes to translated text
    """
    results = {}
    for lang in target_langs:
        if lang in LANG_NAMES:
            translated = await translate_text(text, lang)
            results[lang] = translated
        else:
            results[lang] = f"[UNSUPPORTED LANGUAGE: {lang}]"
    return results

# Main execution function
async def main():
    """
    Example usage of the translation script.
    """
    # Example 1: Process a complete JSON file
    input_file = "translations.json"  # Your input file path
    output_file = "translations.translated.json"
    
    # Check if input file exists
    if os.path.exists(input_file):
        await process_translation_file(input_file, output_file)
    else:
        print(f"Input file '{input_file}' not found. Using example data instead.")
        
        # Example 2: Process a single text (for testing)
        example_text = """name: jira-task-assigner
description: Turn a feature/task/bug description into Jira issues with matching git branches and worktrees, so the pieces can be worked on in parallel."""
        
        translations = await process_single_item(
            example_text,
            [ "he", "ru"]
        )
        
        print("\n--- Example Translation Results ---")
        print(f"Original (EN): {example_text}\n")
        for lang, translated in translations.items():
            print(f"{LANG_NAMES.get(lang, lang)}: {translated}\n")

if __name__ == "__main__":
    # Run the async main function
    asyncio.run(main())
