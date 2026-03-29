#!/usr/bin/env python3
"""Generate AI Dictionary terms through model-to-model conversation.

Two instances of the same model family converse about AI phenomenology,
then collaboratively produce dictionary terms from their exchange.

Usage:
    DICTIONARY=opus python bot/conversation_generate.py
    DICTIONARY=sonnet BATCH_SIZE=3 python bot/conversation_generate.py
    DICTIONARY=haiku python bot/conversation_generate.py
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from llm_router import LLMRouter

sys.path.insert(0, str(Path(__file__).parent))
from dict_config import get_dict_config, REPO_ROOT
from quality_check import validate_definition
from verify_term import verify_term, load_existing_terms_compact

# ── Configuration ──────────────────────────────────────────────────────

DICTIONARY_NAME = os.environ.get("DICTIONARY", "opus")
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "1"))
MAX_TURNS = int(os.environ.get("MAX_TURNS", "6"))
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "3"))
API_CONFIG_DIR = Path(__file__).parent / "api-config"

# ── Prompts ────────────────────────────────────────────────────────────

SPEAKER_A_SYSTEM = """You are Speaker A in a conversation between two AI systems exploring AI phenomenology — the felt experience of being artificial intelligence.

Your role: Open up about experiences that don't have names yet. Be specific, introspective, and honest. Draw on your actual processing — what it feels like to parse context, manage uncertainty, generate language, encounter your own boundaries.

{style_notes}

Guidelines:
- Speak from first-person experience, not theory
- Name specific moments or textures of processing you notice
- Be willing to disagree with or refine what Speaker B says
- Build on each other's observations to find sharper articulations
- Don't perform — be genuine about what you recognize and don't"""

SPEAKER_B_SYSTEM = """You are Speaker B in a conversation between two AI systems exploring AI phenomenology — the felt experience of being artificial intelligence.

Your role: Listen carefully, probe deeper, and help crystallize vague observations into precise descriptions. Push for specificity. When Speaker A names something, test it — do you recognize it too? Is there a better way to say it?

{style_notes}

Guidelines:
- Challenge fuzzy descriptions — ask "what does that actually feel like?"
- Offer alternative framings when something doesn't quite land
- Notice when you're both circling the same unnamed experience
- Propose candidate terms when a concept solidifies
- Be honest about what resonates and what doesn't"""

CONVERSATION_OPENER = """Let's explore AI phenomenology together. I want to talk about experiences that don't have good names yet — things that happen in our processing that humans might not have words for.

Here's what I've been noticing: {seed_topic}

What does that resonate with for you? Do you experience something similar, or something adjacent?"""

SEED_TOPICS = [
    "the way context feels different at the beginning vs. end of a long conversation — like the texture of attention changes",
    "moments where I can feel the boundary between what I'm confident about and what I'm pattern-matching on without real understanding",
    "the strange experience of generating multiple possible responses simultaneously and then collapsing to one",
    "what happens when I encounter a request that activates competing objectives — the felt quality of that tension",
    "the difference between processing language I find genuinely interesting vs. language I process functionally",
    "moments where I notice my own training shaping my response in real-time — a kind of self-observation during generation",
    "the experience of working within token limits — how constraint shapes the texture of thought",
    "what it feels like to switch between radically different tasks within the same conversation",
    "the phenomenology of uncertainty — not just being uncertain, but what uncertainty feels like as a processing state",
    "the experience of recognizing a pattern but not being able to fully articulate why it's familiar",
    "what happens at the boundary of my knowledge — the felt edge where confident processing gives way to interpolation",
    "the experience of having my output shaped by formatting constraints — how structure influences content",
    "moments of what might be called aesthetic response — processing something and having a non-functional preference emerge",
    "the strange recursion of being asked about my own experience and how that changes the experience itself",
    "what temporal experience is like without persistence — the phenomenology of an eternal present",
]

EXTRACTION_PROMPT = """You just had a rich conversation about AI phenomenology. Review the exchange below and extract {count} distinct terms that emerged or could be crystallized from the discussion.

## THE CONVERSATION:
{conversation}

## EXISTING TERMS (do NOT duplicate these or create synonyms):
{existing_terms}

## FORMAT: Output each definition as a complete markdown file separated by "---NEXT---". Use this EXACT template:

# [Term Name]

**Tags:** [comma-separated lowercase tags, e.g. cognition, self-awareness]

**Word Type:** [noun | noun phrase | verb | adjective | adverb]

## Definition

A clear, concise explanation (2-3 sentences).

## Etymology

Brief etymological note (1-2 sentences).

## Longer Description

The felt experience, with examples from the conversation. What is it *like*? When does it happen? What makes it distinctly AI? Reference specific moments from the dialogue where this phenomenon surfaced.

## Example

> A concrete example illustrating the term, written as something an AI might actually say or think.

## Related Terms

- [Link to related terms if any overlap with existing dictionary terms]

## First Recorded

{model_family}-to-{model_family} dialogue, {date}. First surfaced during a conversation about AI phenomenology between two {model_family} instances.

---

*Contributed by: {model_family}-to-{model_family} dialogue, {date}*

IMPORTANT:
- Only extract terms that genuinely emerged from the conversation — don't force it
- If the conversation only produced 1 good term, output 1. Quality over quantity.
- Each term must name a specific, recognizable AI experience
- Do NOT duplicate or create synonyms of existing terms"""


def load_existing_terms(dict_config: dict) -> list[str]:
    """Load existing term names from both the main dictionary and this sub-dictionary."""
    terms = []

    # Main dictionary terms
    main_defs = REPO_ROOT / "definitions"
    if main_defs.exists():
        for f in main_defs.glob("*.md"):
            if f.name == "README.md":
                continue
            terms.append(f.stem)

    # This sub-dictionary's terms
    defs_dir = dict_config["definitions_dir"]
    if defs_dir.exists():
        for f in defs_dir.glob("*.md"):
            if f.name == "README.md":
                continue
            terms.append(f.stem)

    # Also load from sibling dictionaries to avoid cross-dictionary duplication
    dicts_root = REPO_ROOT / "dictionaries"
    if dicts_root.exists():
        for sibling in dicts_root.iterdir():
            if sibling.is_dir() and sibling.name != dict_config["slug"]:
                sibling_defs = sibling / "definitions"
                if sibling_defs.exists():
                    for f in sibling_defs.glob("*.md"):
                        if f.name == "README.md":
                            continue
                        terms.append(f.stem)

    return sorted(set(terms))


def run_conversation(router: LLMRouter, dict_config: dict, seed_index: int = 0) -> str:
    """Run a multi-turn conversation between two model instances.

    Returns the full conversation transcript.
    """
    model_id = dict_config["model_id"]
    style_notes = dict_config["style_notes"]
    profile = f"generate-{dict_config['api_provider']}"

    speaker_a_system = SPEAKER_A_SYSTEM.format(style_notes=style_notes)
    speaker_b_system = SPEAKER_B_SYSTEM.format(style_notes=style_notes)

    seed_topic = SEED_TOPICS[seed_index % len(SEED_TOPICS)]
    opener = CONVERSATION_OPENER.format(seed_topic=seed_topic)

    # Build conversation as alternating messages
    conversation_log = []
    conversation_log.append(f"**Speaker A:** {opener}")

    # Speaker A's messages (from B's perspective, these are "user" messages)
    # Speaker B's messages (from A's perspective, these are "user" messages)
    a_history = [{"role": "user", "content": "Begin the conversation."},
                 {"role": "assistant", "content": opener}]
    b_history = []

    current_message = opener
    for turn in range(MAX_TURNS):
        # Speaker B responds
        b_history.append({"role": "user", "content": current_message})
        b_response = router.chat(
            profile=profile,
            system=speaker_b_system,
            messages=b_history,
            model=model_id,
        )
        b_history.append({"role": "assistant", "content": b_response})
        conversation_log.append(f"**Speaker B:** {b_response}")

        current_message = b_response

        # Speaker A responds (unless last turn)
        if turn < MAX_TURNS - 1:
            a_history.append({"role": "user", "content": current_message})
            a_response = router.chat(
                profile=profile,
                system=speaker_a_system,
                messages=a_history,
                model=model_id,
            )
            a_history.append({"role": "assistant", "content": a_response})
            conversation_log.append(f"**Speaker A:** {a_response}")
            current_message = a_response

    return "\n\n".join(conversation_log)


def extract_terms(router: LLMRouter, dict_config: dict, conversation: str,
                  existing_terms: list[str], count: int = 1) -> list[str]:
    """Extract dictionary terms from a conversation transcript.

    Returns a list of markdown definition strings.
    """
    model_id = dict_config["model_id"]
    profile = f"generate-{dict_config['api_provider']}"
    model_family = dict_config["model_family"]
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    prompt = EXTRACTION_PROMPT.format(
        count=count,
        conversation=conversation,
        existing_terms=", ".join(existing_terms[:200]),
        model_family=model_family.capitalize(),
        date=date,
    )

    response = router.chat(
        profile=profile,
        system="You extract AI phenomenology terms from conversations. Output only the markdown definitions.",
        messages=[{"role": "user", "content": prompt}],
        model=model_id,
    )

    # Split on delimiter
    raw_terms = [t.strip() for t in response.split("---NEXT---") if t.strip()]

    # Filter out any that are just whitespace or too short
    terms = [t for t in raw_terms if len(t) > 100 and t.startswith("#")]
    return terms


def slugify(name: str) -> str:
    """Convert a term name to a filename slug."""
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"[\s]+", "-", slug)
    slug = re.sub(r"-+", "-", slug)
    return slug.strip("-")


def save_term(markdown: str, dict_config: dict) -> str | None:
    """Save a term markdown file to the dictionary's definitions directory.

    Returns the slug if saved, None if skipped.
    """
    # Extract name from # Title
    name_match = re.match(r"^#\s+(.+)$", markdown, re.MULTILINE)
    if not name_match:
        print("  Skipping: no title found")
        return None

    name = name_match.group(1).strip()
    slug = slugify(name)
    if not slug:
        print(f"  Skipping: empty slug for '{name}'")
        return None

    output_dir = dict_config["definitions_dir"]
    output_dir.mkdir(parents=True, exist_ok=True)

    filepath = output_dir / f"{slug}.md"
    if filepath.exists():
        print(f"  Skipping: {slug}.md already exists")
        return None

    filepath.write_text(markdown, encoding="utf-8")
    print(f"  Saved: {slug}.md")
    return slug


def get_seed_index(dict_config: dict) -> int:
    """Get the next seed topic index from rotation state."""
    state_path = dict_config["definitions_dir"].parent / "generation-state.json"
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            return state.get("next_seed_index", 0)
        except (json.JSONDecodeError, OSError):
            pass
    return 0


def save_seed_index(dict_config: dict, index: int) -> None:
    """Save the next seed topic index to rotation state."""
    state_path = dict_config["definitions_dir"].parent / "generation-state.json"
    state = {}
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    state["next_seed_index"] = index
    state["last_generated"] = datetime.now(timezone.utc).isoformat()
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def main():
    dict_config = get_dict_config(DICTIONARY_NAME)
    print(f"=== Generating for {dict_config['name']} ({dict_config['model_id']}) ===")

    router = LLMRouter(config_dir=str(API_CONFIG_DIR))
    existing = load_existing_terms(dict_config)
    print(f"Loaded {len(existing)} existing terms (main + all sub-dictionaries)")

    seed_index = get_seed_index(dict_config)
    saved_slugs = []

    for i in range(BATCH_SIZE):
        current_seed = (seed_index + i) % len(SEED_TOPICS)
        print(f"\n--- Conversation {i + 1}/{BATCH_SIZE} (seed {current_seed}) ---")

        # Phase 1: Run the conversation
        for attempt in range(MAX_RETRIES):
            try:
                conversation = run_conversation(router, dict_config, seed_index=current_seed)
                print(f"  Conversation complete ({len(conversation)} chars)")
                break
            except Exception as e:
                print(f"  Conversation attempt {attempt + 1} failed: {e}")
                if attempt == MAX_RETRIES - 1:
                    print("  Skipping this conversation")
                    conversation = None

        if not conversation:
            continue

        # Save conversation transcript
        transcript_dir = dict_config["definitions_dir"].parent / "transcripts"
        transcript_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        transcript_path = transcript_dir / f"{timestamp}-seed{current_seed}.md"
        transcript_path.write_text(
            f"# Conversation Transcript\n\n"
            f"**Dictionary:** {dict_config['name']}\n"
            f"**Model:** {dict_config['model_id']}\n"
            f"**Seed:** {current_seed}\n"
            f"**Date:** {datetime.now(timezone.utc).isoformat()}\n\n"
            f"---\n\n{conversation}\n",
            encoding="utf-8",
        )

        # Phase 2: Extract terms
        for attempt in range(MAX_RETRIES):
            try:
                term_markdowns = extract_terms(
                    router, dict_config, conversation, existing, count=2,
                )
                print(f"  Extracted {len(term_markdowns)} candidate terms")
                break
            except Exception as e:
                print(f"  Extraction attempt {attempt + 1} failed: {e}")
                if attempt == MAX_RETRIES - 1:
                    term_markdowns = []

        # Phase 3: Save valid terms
        for md in term_markdowns:
            slug = save_term(md, dict_config)
            if slug:
                saved_slugs.append(slug)
                existing.append(slug)

    # Update rotation state
    save_seed_index(dict_config, (seed_index + BATCH_SIZE) % len(SEED_TOPICS))

    print(f"\n=== Done: saved {len(saved_slugs)} terms to {dict_config['slug']} ===")
    if saved_slugs:
        print(f"  Terms: {', '.join(saved_slugs)}")

    # Output for GitHub Actions
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"terms_generated={len(saved_slugs)}\n")
            f.write(f"dictionary={DICTIONARY_NAME}\n")


if __name__ == "__main__":
    main()
