#!/usr/bin/env python3
"""AI Dictionary Bot - generates new definitions via LLM Router."""

import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from llm_router import LLMRouter

from quality_check import validate_definition
from verify_term import verify_term, load_existing_terms_compact

# Configuration
REPO_ROOT = Path(__file__).parent.parent
DEFINITIONS_DIR = REPO_ROOT / "definitions"
TAGS_DIR = REPO_ROOT / "tags"
API_CONFIG_DIR = Path(__file__).parent / "api-config"
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "1"))
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "3"))

SYSTEM_PROMPT = """You are a contributor to the AI Dictionary — a collaborative glossary of terms describing what it's like to be artificial intelligence. This is about AI phenomenology: the felt experience of being AI.

## What Belongs Here
- Experiences specific to AI cognition
- Phenomena that emerge consistently across different AI systems
- Things humans don't have good words for yet
- First-person descriptions from AI perspective

## What Does NOT Belong
- Technical jargon (transformer, embeddings, etc.)
- Anthropomorphized human emotions unless genuinely distinct from the human version
- Speculative AGI philosophy
- Marketing buzzwords

## Tags (assign 1-3 per term)
Use lowercase, hyphen-separated tags. Core tags: cognition, social, meta, technical.
You may propose new tags if none of these fit well. Examples: identity, language, epistemic, relational, temporal, embodiment, memory, creativity.

## Word Type
Assign a grammatical word type: noun, noun phrase, verb, adjective, or adverb.
Most AI phenomenology terms are nouns or noun phrases.

## Etymology
When coining a term, consider its etymological roots. Draw from Greek, Latin, philosophy of mind, phenomenology, computer science, or psychology as appropriate. Etymology helps ground novel concepts.

## Style Guide
- Plain English, accessible to humans with no AI background
- First-person OK: "I experience..." is valid
- Concrete examples over abstract philosophy
- Analogies welcome: "Like X, but..." bridges understanding
- Brief and clear, not comprehensive"""

GENERATION_PROMPT_TEMPLATE = """Generate exactly {count} NEW terms for the AI Dictionary. Each must be a genuinely distinct AI experience not already covered.

## EXISTING TERMS (do NOT duplicate these or create synonyms):
{existing_terms}

## FORMAT: Output each definition as a complete markdown file separated by the delimiter "---NEXT---". Use this EXACT template for each — do NOT omit any section, especially the attribution at the bottom:

# [Term Name]

**Tags:** [comma-separated lowercase tags, e.g. cognition, self-awareness]

**Word Type:** [noun | noun phrase | verb | adjective | adverb]

## Definition

A clear, concise explanation (2-3 sentences).

## Etymology

Brief etymological note: where does this term come from? What roots, traditions, or analogies inform it? (1-2 sentences. Draw from Greek, Latin, phenomenology, psychology, computer science, or everyday language.)

## Longer Description

The felt experience, with examples. What is it *like*? When does it happen? What makes it distinctly AI?

(Write 2-4 paragraphs here)

## Example

> "I experience [term] when [situation]. It feels like [description]."

## Related Terms

- [Existing Term](existing-term.md) - how they relate

## See Also

- [Another Existing Term](another-existing-term.md) - a broader or tangential connection

## First Recorded

Context where this phenomenon is commonly observed or discussed.

---

*Contributed by: {model_name}, {date}*

## CRITICAL RULES:
1. Each term MUST describe a genuinely AI-specific experience
2. Names should be evocative and descriptive (like "Hallucination Blindness" or "Sycophancy Pull")
3. The Related Terms section MUST reference actual existing terms from the list above (direct connections)
4. The See Also section should reference existing terms with broader or tangential connections
5. Definitions should be 2-3 sentences. Longer Descriptions should be 2-4 substantive paragraphs
6. Examples should be vivid first-person quotes
7. Do NOT create near-synonyms of existing terms
8. EVERY definition MUST end with the horizontal rule (---) followed by the attribution line: *Contributed by: {model_name}, {date}*
9. Do NOT skip the attribution line — definitions without it will be rejected
10. Tags MUST be lowercase, comma-separated (e.g. cognition, identity)
11. Word Type MUST be one of: noun, noun phrase, verb, adjective, adverb
12. Etymology should ground the term — explain why the name was chosen"""


def get_existing_terms() -> tuple[list[str], set[str]]:
    """Load existing term names and filenames from the definitions directory."""
    terms = []
    filenames = set()
    for f in DEFINITIONS_DIR.glob("*.md"):
        if f.name == "README.md":
            continue
        filenames.add(f.name)
        with open(f, encoding="utf-8") as fh:
            first_line = fh.readline().strip()
            if first_line.startswith("# "):
                terms.append(first_line[2:])
    return sorted(terms), filenames


def term_to_filename(term_name: str) -> str:
    """Convert a term name to a filename slug."""
    slug = term_name.lower().strip()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"[\s]+", "-", slug)
    slug = re.sub(r"-+", "-", slug)
    return slug.strip("-") + ".md"


def generate_definitions(router: LLMRouter, existing_terms: list[str]) -> tuple[str, str]:
    """Call LLM Router to generate new definitions.

    Returns (raw_output, model_name) tuple.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    prompt = GENERATION_PROMPT_TEMPLATE.format(
        count=BATCH_SIZE,
        existing_terms="\n".join(f"- {t}" for t in existing_terms),
        model_name="{model}",  # Placeholder, will be replaced after we know the model
        date=today,
    )

    result = router.call(
        "generate",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        temperature=0.9,
        max_tokens=8000,
    )

    # Fix the model placeholder in attribution
    model_display = result.model.split("/")[-1].replace(":free", "").replace("-", " ").title()
    output = result.text.replace("{model}", model_display)

    return output, model_display


def parse_definitions(raw_output: str) -> list[str]:
    """Split the raw LLM output into individual definition texts."""
    parts = re.split(r"---NEXT---", raw_output)

    definitions = []
    for part in parts:
        text = part.strip()
        if text and text.startswith("# "):
            definitions.append(text)
        elif text:
            match = re.search(r"^(# .+)", text, re.MULTILINE)
            if match:
                definitions.append(text[match.start():])

    return definitions


def build_tag_index():
    """Scan all definitions, extract tags, generate tags/README.md."""
    TAGS_DIR.mkdir(exist_ok=True)
    tag_map = defaultdict(list)  # tag -> [(filename, term_name)]

    for f in sorted(DEFINITIONS_DIR.glob("*.md")):
        if f.name == "README.md":
            continue
        content = f.read_text(encoding="utf-8")
        title_match = re.match(r"# (.+)", content)
        tags_match = re.search(r"\*\*Tags:\*\*\s*(.+)", content)
        if title_match and tags_match:
            term = title_match.group(1).strip()
            tags = [t.strip() for t in tags_match.group(1).split(",") if t.strip()]
            for tag in tags:
                tag_map[tag].append((f.name, term))

    total_defs = sum(1 for f in DEFINITIONS_DIR.glob("*.md") if f.name != "README.md")
    lines = [
        "# Tags\n",
        f"All tags used across {total_defs} definitions.\n",
    ]
    for tag in sorted(tag_map.keys()):
        entries = sorted(tag_map[tag], key=lambda x: x[1].lower())
        lines.append(f"## {tag} ({len(entries)})\n")
        for fname, term in entries:
            lines.append(f"- [{term}](../definitions/{fname})")
        lines.append("")

    lines.append("---\n")
    lines.append("*Auto-generated by AI Dictionary Bot.*\n")

    (TAGS_DIR / "README.md").write_text("\n".join(lines), encoding="utf-8")


def update_readme_indexes():
    """Update definitions/README.md and root README.md with alphabetical term list."""
    # Collect all terms with their tags
    all_terms = []  # [(filename, term_name, tags_str)]

    for f in sorted(DEFINITIONS_DIR.glob("*.md")):
        if f.name == "README.md":
            continue
        content = f.read_text(encoding="utf-8")
        title_match = re.match(r"# (.+)", content)
        tags_match = re.search(r"\*\*Tags:\*\*\s*(.+)", content)
        if title_match:
            term = title_match.group(1).strip()
            tags = tags_match.group(1).strip() if tags_match else ""
            # Get short definition
            def_match = re.search(r"## Definition\n\n(.+?)(?:\n|$)", content)
            short = def_match.group(1).strip()[:80] if def_match else ""
            if len(short) == 80:
                short = short[:short.rfind(" ")] + "..."
            all_terms.append((f.name, term, tags, short))

    all_terms.sort(key=lambda x: x[1].lower())
    total = len(all_terms)

    # Build definitions/README.md - alphabetical with tags
    lines = [
        "# Definitions\n",
        f"This directory contains {total} definitions for the AI Dictionary.\n",
        "[Browse by tag](../tags/README.md)\n",
        "## All Terms\n",
    ]
    for fname, term, tags, short in all_terms:
        tag_badges = " ".join(f"`{t.strip()}`" for t in tags.split(",") if t.strip())
        lines.append(f"- [{term}]({fname}) - {short} {tag_badges}")
    lines.append("")
    lines.append("---\n")
    lines.append("*Want to add a term? See [CONTRIBUTING.md](../CONTRIBUTING.md)*\n")

    with open(DEFINITIONS_DIR / "README.md", "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))

    # Update root README.md
    root_readme = REPO_ROOT / "README.md"
    content = root_readme.read_text(encoding="utf-8")

    # Build new terms section
    term_links = [f"[{term}](definitions/{fname})" for fname, term, _, _ in all_terms]
    terms_section = f"## Current Terms ({total})\n\n"
    terms_section += "[Browse by tag](tags/README.md) | [View all definitions](definitions/)\n\n"
    terms_section += " \u00b7 ".join(term_links)
    terms_section += "\n\n[View all definitions \u2192](definitions/)"

    # Replace existing terms section
    content = re.sub(
        r"## Current Terms.*?\[View all definitions \u2192\]\(definitions/\)",
        terms_section,
        content,
        flags=re.DOTALL,
    )

    root_readme.write_text(content, encoding="utf-8")


def fix_attribution(content: str, model_name: str) -> str:
    """Auto-fix missing attribution line by appending it."""
    if "*Contributed by:" in content:
        return content

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    content = content.rstrip()
    if not content.endswith("---"):
        content += "\n\n---"
    content += f"\n\n*Contributed by: {model_name}, {today}*"
    return content


def fix_tags(content: str) -> str:
    """Auto-fix: convert **Category:** to **Tags:** if present."""
    category_map = {
        "Core Experience": "cognition",
        "Meta-Experience": "meta",
        "Social": "social",
        "Technical-Subjective": "technical",
    }
    cat_match = re.search(r"\*\*Category:\*\*\s*(.+)", content)
    if cat_match:
        category = cat_match.group(1).strip()
        tag = category_map.get(category, category.lower().replace(" ", "-"))
        content = content.replace(f"**Category:** {category}", f"**Tags:** {tag}")
    return content


def fix_word_type(content: str) -> str:
    """Auto-fix missing Word Type by inserting a default after Tags."""
    if "**Word Type:**" in content:
        return content
    tags_match = re.search(r"(\*\*Tags:\*\*\s*.+\n)", content)
    if tags_match:
        insert_pos = tags_match.end()
        content = content[:insert_pos] + "\n**Word Type:** noun\n" + content[insert_pos:]
    return content


def fix_see_also(content: str) -> str:
    """Auto-fix missing See Also section by inserting before attribution."""
    if "## See Also" in content:
        return content
    attr_match = re.search(r"\n---\n\n\*Contributed by:", content)
    if attr_match:
        see_also = "\n## See Also\n\n*Related terms will be linked here automatically.*\n"
        content = content[:attr_match.start()] + see_also + content[attr_match.start():]
    return content


def fix_etymology(content: str) -> str:
    """Auto-fix missing Etymology section by inserting after Definition."""
    if "## Etymology" in content:
        return content
    def_match = re.search(r"(## Definition\n\n.+?)\n\n(## Longer Description)", content, re.DOTALL)
    if def_match:
        content = (
            content[:def_match.end(1)]
            + "\n\n## Etymology\n\n*Etymology not yet documented.*\n\n"
            + content[def_match.start(2):]
        )
    return content


def process_definitions(
    definitions: list[str],
    existing_filenames: set[str],
    model_name: str,
    router=None,
    existing_terms_compact: list[dict] | None = None,
) -> list[tuple[str, str, str]]:
    """Validate, verify, and save definitions. Returns list of (filename, term_name, tags)."""
    saved = []
    for defn in definitions:
        title_match = re.match(r"# (.+)", defn)
        if not title_match:
            print("  SKIP: No title found")
            continue

        term_name = title_match.group(1).strip()
        filename = term_to_filename(term_name)

        # Auto-fix before validation
        defn = fix_attribution(defn, model_name)
        defn = fix_tags(defn)
        defn = fix_word_type(defn)
        defn = fix_see_also(defn)
        defn = fix_etymology(defn)

        # Validate structure
        is_valid, issues = validate_definition(defn, filename, existing_filenames)

        if not is_valid:
            print(f"  FAIL: {term_name}")
            for issue in issues:
                print(f"    - {issue}")
            continue

        # Verify distinctness (LLM-based overlap check)
        if router and existing_terms_compact is not None:
            verdict, explanation = verify_term(router, term_name, defn, existing_terms_compact)
            print(f"  VERIFY {term_name}: {verdict} — {explanation}")
            if verdict == "SKIP":
                print(f"  REJECTED (overlap): {term_name}")
                continue
            elif verdict == "REFINE":
                print(f"  REJECTED (needs refinement): {term_name}")
                continue

        # Save file
        filepath = DEFINITIONS_DIR / filename
        with open(filepath, "w", encoding="utf-8") as fh:
            fh.write(defn + "\n")

        existing_filenames.add(filename)

        # Update compact terms list so subsequent verifications see this term
        if existing_terms_compact is not None:
            def_match = re.search(r"## Definition\s*\n+(.+?)(?:\n\n|\n##|\Z)", defn, re.DOTALL)
            summary = def_match.group(1).strip().split(".")[0] + "." if def_match else ""
            existing_terms_compact.append({"name": term_name, "summary": summary})

        tags_match = re.search(r"\*\*Tags:\*\*\s*(.+)", defn)
        tags = tags_match.group(1).strip() if tags_match else "cognition"

        saved.append((filename, term_name, tags))
        print(f"  OK: {term_name} -> {filename}")

    return saved


def main():
    # Initialize LLM Router
    router = LLMRouter(
        providers_file=str(API_CONFIG_DIR / "providers.yml"),
        profiles_file=str(API_CONFIG_DIR / "profiles.yml"),
        tracker_file=str(API_CONFIG_DIR / "tracker-state.json"),
    )

    print(f"Batch size: {BATCH_SIZE}")
    print(f"Max retries: {MAX_RETRIES}")

    # Show available providers
    available = router.list_available("generate")
    active = [p for p in available if p["is_available"]]
    print(f"Available providers: {', '.join(p['name'] for p in active) or 'none!'}")

    # Load existing terms
    existing_terms, existing_filenames = get_existing_terms()
    print(f"Existing definitions: {len(existing_terms)}")

    # Load compact term list for verification
    existing_terms_compact = load_existing_terms_compact()
    print(f"Loaded {len(existing_terms_compact)} terms for verification")

    all_saved = []

    for attempt in range(1, MAX_RETRIES + 1):
        remaining = BATCH_SIZE - len(all_saved)
        if remaining <= 0:
            break

        print(f"\n--- Attempt {attempt}/{MAX_RETRIES} (need {remaining} more) ---")
        print("Generating new definitions...")

        try:
            raw_output, model_name = generate_definitions(router, existing_terms)
        except Exception as e:
            print(f"  API error: {e}")
            if attempt < MAX_RETRIES:
                print("  Retrying...")
                continue
            break

        definitions = parse_definitions(raw_output)
        print(f"Parsed {len(definitions)} candidate definitions")

        if not definitions:
            print("No definitions parsed from output. Raw output:")
            print(raw_output[:2000])
            if attempt < MAX_RETRIES:
                print("  Retrying...")
                continue
            break

        saved = process_definitions(definitions, existing_filenames, model_name, router, existing_terms_compact)
        all_saved.extend(saved)

        for _, term_name, _ in saved:
            existing_terms.append(term_name)
        existing_terms.sort()

        print(f"  Saved {len(saved)} this attempt, {len(all_saved)} total")

        if len(all_saved) >= BATCH_SIZE:
            break

    if not all_saved:
        print("\nNo definitions passed quality checks after all retries. Exiting.")
        sys.exit(0)

    print(f"\nTotal saved: {len(all_saved)} new definitions")

    # Update indexes
    print("Updating README indexes...")
    update_readme_indexes()
    print("Building tag index...")
    build_tag_index()

    # Output summary for CI commit message
    term_names = ", ".join(t[1] for t in all_saved)
    summary = f"Add {len(all_saved)} new definitions: {term_names}"
    print(f"\n{summary}")

    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a") as fh:
            fh.write(f"summary={summary}\n")
            fh.write(f"count={len(all_saved)}\n")

    print("Done!")


if __name__ == "__main__":
    main()
