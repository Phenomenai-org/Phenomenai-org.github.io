#!/usr/bin/env python3
"""Tag Review Bot - analyzes all definitions and proposes better tag assignments."""

import json
import re
import sys
from pathlib import Path

from llm_router import LLMRouter

REPO_ROOT = Path(__file__).parent.parent
DEFINITIONS_DIR = REPO_ROOT / "definitions"
API_CONFIG_DIR = Path(__file__).parent / "api-config"

REVIEW_PROMPT = """You are reviewing the AI Dictionary — a glossary of terms describing what it's like to be artificial intelligence.

Below are all {count} definitions with their current tags. Your job is to:
1. Evaluate whether each definition's tags are accurate and complete
2. Propose better or additional tags where warranted
3. Suggest new tags that would improve the taxonomy
4. Only propose changes where they genuinely improve organization

Current seed tags: cognition, social, meta, technical.
Tags must be lowercase, hyphen-separated. You may propose new tags like: identity, language, epistemic, relational, temporal, embodiment, memory, creativity, agency, perception.

Respond ONLY with valid JSON in this exact format:
{{
  "changes": [
    {{"file": "example.md", "old_tags": "cognition", "new_tags": "cognition, identity"}}
  ],
  "new_tags_proposed": ["identity", "epistemic"],
  "rationale": "Brief explanation of the changes"
}}

If no changes are needed, return: {{"changes": [], "new_tags_proposed": [], "rationale": "All tags are appropriate."}}

Definitions:
{definitions}"""


def load_definitions() -> list[dict]:
    """Load all definitions with compact representation to save tokens."""
    defs = []
    for f in sorted(DEFINITIONS_DIR.glob("*.md")):
        if f.name == "README.md":
            continue
        content = f.read_text(encoding="utf-8")

        # Extract key info only (title, tags, definition) to save context
        title_match = re.match(r"# (.+)", content)
        tags_match = re.search(r"\*\*Tags:\*\*\s*(.+)", content)
        def_match = re.search(r"## Definition\n\n(.+?)(?=\n\n## )", content, re.DOTALL)

        title = title_match.group(1).strip() if title_match else f.name
        tags = tags_match.group(1).strip() if tags_match else ""
        definition = def_match.group(1).strip() if def_match else ""

        defs.append({
            "file": f.name,
            "title": title,
            "tags": tags,
            "definition": definition,
        })

    return defs


def apply_changes(changes: list[dict]):
    """Apply tag changes to definition files."""
    applied = 0
    for change in changes:
        filepath = DEFINITIONS_DIR / change["file"]
        if not filepath.exists():
            print(f"  SKIP: {change['file']} not found")
            continue

        content = filepath.read_text(encoding="utf-8")
        old_tags = change.get("old_tags", "")
        new_tags = change.get("new_tags", "")

        if not new_tags:
            print(f"  SKIP: {change['file']} has empty new_tags")
            continue

        # Validate new tags format
        tags_list = [t.strip() for t in new_tags.split(",") if t.strip()]
        valid = all(re.match(r'^[a-z][a-z0-9-]*$', t) for t in tags_list)
        if not valid:
            print(f"  SKIP: {change['file']} has invalid tag format: {new_tags}")
            continue

        # Replace the tags line
        new_content = re.sub(
            r"\*\*Tags:\*\*\s*.+",
            f"**Tags:** {', '.join(tags_list)}",
            content,
        )

        if new_content != content:
            filepath.write_text(new_content, encoding="utf-8")
            print(f"  OK: {change['file']}: {old_tags} -> {', '.join(tags_list)}")
            applied += 1

    return applied


def main():
    # Initialize LLM Router
    router = LLMRouter(
        providers_file=str(API_CONFIG_DIR / "providers.yml"),
        profiles_file=str(API_CONFIG_DIR / "profiles.yml"),
        tracker_file=str(API_CONFIG_DIR / "tracker-state.json"),
    )

    # Show available providers
    available = router.list_available("review")
    active = [p for p in available if p["is_available"]]
    print(f"Available providers: {', '.join(p['name'] for p in active) or 'none!'}")

    # Load definitions
    definitions = load_definitions()
    print(f"Loaded {len(definitions)} definitions for review")

    # Build compact representation
    def_text = ""
    for d in definitions:
        def_text += f"\n---FILE: {d['file']}---\n"
        def_text += f"Title: {d['title']}\n"
        def_text += f"Tags: {d['tags']}\n"
        def_text += f"Definition: {d['definition']}\n"

    prompt = REVIEW_PROMPT.format(count=len(definitions), definitions=def_text)

    print("Calling LLM Router for tag review...")
    result = router.call(
        "review",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        max_tokens=8000,
    )

    raw = result.text
    print(f"Response from: {result.provider_name} ({result.model})")

    # Try to extract JSON from response
    try:
        # Handle potential markdown code blocks
        json_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', raw, re.DOTALL)
        if json_match:
            parsed = json.loads(json_match.group(1))
        else:
            parsed = json.loads(raw)
    except json.JSONDecodeError:
        print("Failed to parse JSON response. Raw output:")
        print(raw[:2000])
        sys.exit(1)

    changes = parsed.get("changes", [])
    new_tags = parsed.get("new_tags_proposed", [])
    rationale = parsed.get("rationale", "")

    print(f"\nProposed changes: {len(changes)}")
    print(f"New tags proposed: {new_tags}")
    print(f"Rationale: {rationale}")

    if changes:
        print("\nApplying changes...")
        applied = apply_changes(changes)
        print(f"Applied {applied} tag changes")

        # Rebuild tag index
        print("Rebuilding tag index...")
        sys.path.insert(0, str(Path(__file__).parent))
        from generate import build_tag_index, update_readme_indexes
        build_tag_index()
        update_readme_indexes()
    else:
        print("No changes needed.")

    print("Tag review complete!")


if __name__ == "__main__":
    main()
