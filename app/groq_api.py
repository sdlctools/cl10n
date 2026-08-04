import os
import json
import asyncio
from typing import List, Dict, Any
from groq import AsyncGroq

# Initialize the Groq client
client = AsyncGroq(
    api_key=os.environ.get("GROQ_API_KEY"),  # Set your API key in environment variables
)

# Your translation prompt template
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

# Language name mapping for prompts
LANG_NAMES = {
    "he": "Hebrew",
    "ru": "Russian"
}

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
        completion = await client.chat.completions.create(
            model="openai/gpt-oss-120b",  # Best for translation tasks
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