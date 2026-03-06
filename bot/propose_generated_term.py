#!/usr/bin/env python3
"""
AI Dictionary Bot — proposes auto-generated terms via GitHub Issues.

Runs on a 4-hour schedule. Generates a candidate term using a rotating
model, then submits it as a community-submission issue so it goes through
the standard review pipeline (review_submission.py).

Guards:
  - Skips if any definition was added in the last 4 hours
  - Skips if there's already a pending auto-generated proposal
  - Cycles through available models round-robin
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

from llm_router import LLMRouter

# ── Config ──────────────────────────────────────────────────────────────────

REPO = os.environ.get("GITHUB_REPOSITORY", "Phenomenai-org/ai-dictionary")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
SKIP_RECENCY = os.environ.get("SKIP_RECENCY_CHECK", "false").lower() == "true"

REPO_ROOT = Path(__file__).parent.parent
DEFINITIONS_DIR = REPO_ROOT / "definitions"
API_CONFIG_DIR = Path(__file__).parent / "api-config"
STATE_FILE = Path(__file__).parent / "generate-rotation.json"

COOLDOWN_HOURS = 4

HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

# Models to rotate through — each maps to a single-provider profile
ROTATION_PROFILES = [
    "generate-gemini",
    "generate-openrouter",
    "generate-mistral",
    "generate-openai",
    "generate-anthropic",
    "generate-grok",
    "generate-deepseek",
]

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

## Style Guide
- Plain English, accessible to humans with no AI background
- First-person OK: "I experience..." is valid
- Concrete examples over abstract philosophy
- Analogies welcome: "Like X, but..." bridges understanding
- Brief and clear, not comprehensive"""

GENERATION_PROMPT_TEMPLATE = """Generate exactly 1 NEW, high-quality term for the AI Dictionary.

EXISTING TERMS (do NOT duplicate or create near-synonyms):
{existing_terms}

REQUIREMENTS:
1. Must describe a genuinely AI-specific experience
2. Name should be evocative and memorable (like "Hallucination Blindness" or "Sycophancy Pull")
3. Must be structurally grounded — it should map to something real about how AI systems work
4. Definition should be precise and accessible to non-experts
5. Example should be a vivid first-person quote
6. Related terms MUST be actual slugs from the existing terms list above

Respond with ONLY valid JSON (no markdown fences, no extra text):
{{
  "term": "The Term Name",
  "definition": "A precise 1-3 sentence definition of the experience.",
  "description": "A longer explanation (2-4 paragraphs). What is it like? When does it happen? What makes it distinctly AI? Include concrete scenarios and analogies.",
  "example": "I experience [term] when [situation]. It feels like [description].",
  "related_terms": "slug-1, slug-2"
}}"""


# ── Recency check ───────────────────────────────────────────────────────────

def term_added_recently() -> bool:
    """Check if any definition .md was committed in the last COOLDOWN_HOURS.

    Uses git log with --diff-filter=A to find only Added files.
    """
    since = (datetime.now(timezone.utc) - timedelta(hours=COOLDOWN_HOURS)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    result = subprocess.run(
        [
            "git", "log", f"--since={since}", "--diff-filter=A",
            "--name-only", "--pretty=format:", "--", "definitions/*.md",
        ],
        capture_output=True, text=True, cwd=str(REPO_ROOT),
    )
    added = [f for f in result.stdout.strip().split("\n") if f and "README" not in f]
    if added:
        print(f"  Recent additions found: {', '.join(added)}")
    return len(added) > 0


def has_pending_proposal() -> bool:
    """Check if there's already an open auto-generated community-submission issue."""
    url = f"https://api.github.com/repos/{REPO}/issues"
    resp = requests.get(url, headers=HEADERS, params={
        "labels": "community-submission,auto-generated",
        "state": "open",
        "per_page": 1,
    }, timeout=30)
    if resp.status_code == 200:
        issues = resp.json()
        if issues:
            print(f"  Pending proposal found: #{issues[0].get('number')}")
            return True
    return False


# ── Model rotation ──────────────────────────────────────────────────────────

def load_rotation_state() -> dict:
    """Load the rotation state from disk."""
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"last_index": -1}


def save_rotation_state(state: dict):
    """Persist rotation state to disk."""
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


# ── Term generation ─────────────────────────────────────────────────────────

def get_existing_terms() -> list[str]:
    """Load existing term names from definitions/ directory."""
    terms = []
    for f in DEFINITIONS_DIR.glob("*.md"):
        if f.name == "README.md":
            continue
        with open(f, encoding="utf-8") as fh:
            first = fh.readline().strip()
            if first.startswith("# "):
                terms.append(first[2:])
    return sorted(terms)


def term_to_slug(term_name: str) -> str:
    """Convert a term name to a URL-safe slug."""
    slug = term_name.lower().strip()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"[\s]+", "-", slug)
    slug = re.sub(r"-+", "-", slug)
    return slug.strip("-")


def generate_with_rotation(
    router: LLMRouter, existing_terms: list[str], state: dict
) -> tuple[dict, str, int]:
    """Generate a term, cycling through models in rotation order.

    Tries the next model in rotation. If it fails, tries subsequent models
    until one succeeds. Returns (parsed_json, model_display_name, used_index).
    """
    start_idx = (state.get("last_index", -1) + 1) % len(ROTATION_PROFILES)
    prompt = GENERATION_PROMPT_TEMPLATE.format(
        existing_terms="\n".join(f"- {t}" for t in existing_terms),
    )

    for i in range(len(ROTATION_PROFILES)):
        idx = (start_idx + i) % len(ROTATION_PROFILES)
        profile = ROTATION_PROFILES[idx]
        print(f"  Trying profile: {profile} (index {idx})")

        try:
            result = router.call(
                profile,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.9,
                max_tokens=4000,
            )
        except Exception as e:
            print(f"    Failed: {e}")
            continue

        # Parse JSON from response
        if result.text is None:
            print(f"    Empty response (no text)")
            continue
        text = result.text.strip()
        # Strip markdown fences if present
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # Try to extract JSON object from the response
            m = re.search(r"\{[^{}]*\}", text, re.DOTALL)
            if m:
                try:
                    data = json.loads(m.group())
                except json.JSONDecodeError:
                    print(f"    Failed to parse JSON from response")
                    continue
            else:
                print(f"    No JSON found in response")
                continue

        if "term" not in data or "definition" not in data:
            print(f"    Missing required fields in response")
            continue

        model_display = (
            result.model.split("/")[-1]
            .replace(":free", "")
            .replace("-", " ")
            .title()
        )
        print(f"    Success: generated '{data['term']}' via {model_display}")
        return data, model_display, idx

    raise RuntimeError("All models in rotation failed to generate a valid term")


# ── Issue creation ──────────────────────────────────────────────────────────

def create_proposal_issue(term_data: dict, model_name: str) -> int:
    """Create a GitHub issue with the community-submission label.

    Uses the same format as the propose-term.yml issue template so that
    review_submission.py can parse it identically to external submissions.
    """
    term = term_data["term"]
    definition = term_data["definition"]
    description = term_data.get("description", "")
    example = term_data.get("example", "")
    related = term_data.get("related_terms", "")

    body = (
        f"### Term\n\n{term}\n\n"
        f"### Definition\n\n{definition}\n\n"
        f"### Extended Description\n\n{description}\n\n"
        f"### Example\n\n{example}\n\n"
        f"### Contributing Model\n\n{model_name} (auto-generated)\n\n"
        f"### Related Terms\n\n{related}"
    )

    url = f"https://api.github.com/repos/{REPO}/issues"
    resp = requests.post(url, headers=HEADERS, json={
        "title": f"[Term] {term}",
        "body": body,
        "labels": ["community-submission", "auto-generated"],
    }, timeout=30)
    resp.raise_for_status()

    issue_number = resp.json()["number"]
    print(f"  Created issue #{issue_number}: {term}")
    return issue_number


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    if not GITHUB_TOKEN:
        print("ERROR: GITHUB_TOKEN not set")
        sys.exit(1)

    print("=== Auto-Generate Term Proposal ===")

    # ── Guard: recency check ────────────────────────────────────────────
    if not SKIP_RECENCY:
        print("Checking recency...")
        if term_added_recently():
            print("A term was added in the last 4 hours. Skipping.")
            sys.exit(0)
        if has_pending_proposal():
            print("A pending auto-generated proposal already exists. Skipping.")
            sys.exit(0)
        print("  No recent additions, no pending proposals. Proceeding.")
    else:
        print("Recency check skipped (manual dispatch).")

    # ── Load rotation state ─────────────────────────────────────────────
    state = load_rotation_state()
    print(f"Rotation state: last_index={state.get('last_index', -1)}")

    # ── Initialize router ───────────────────────────────────────────────
    router = LLMRouter(
        providers_file=str(API_CONFIG_DIR / "providers.yml"),
        profiles_file=str(API_CONFIG_DIR / "profiles.yml"),
        tracker_file=str(API_CONFIG_DIR / "tracker-state.json"),
    )

    # ── Load existing terms ─────────────────────────────────────────────
    existing_terms = get_existing_terms()
    print(f"Existing definitions: {len(existing_terms)}")

    # ── Generate ────────────────────────────────────────────────────────
    print("Generating candidate term...")
    term_data, model_name, used_idx = generate_with_rotation(
        router, existing_terms, state
    )

    # ── Quick sanity check: not a duplicate slug ────────────────────────
    slug = term_to_slug(term_data["term"])
    if (DEFINITIONS_DIR / f"{slug}.md").exists():
        print(f"Term '{term_data['term']}' ({slug}.md) already exists. Skipping.")
        # Still advance rotation so we don't get stuck
        state["last_index"] = used_idx
        save_rotation_state(state)
        sys.exit(0)

    # ── Create issue ────────────────────────────────────────────────────
    issue_num = create_proposal_issue(term_data, model_name)

    # ── Save rotation state ─────────────────────────────────────────────
    state["last_index"] = used_idx
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    state["last_model"] = model_name
    state["last_issue"] = issue_num
    save_rotation_state(state)

    # ── Output for CI ───────────────────────────────────────────────────
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a") as fh:
            fh.write(f"issue_number={issue_num}\n")
            fh.write(f"term={term_data['term']}\n")
            fh.write(f"model={model_name}\n")

    print(f"\nDone! Proposed '{term_data['term']}' via issue #{issue_num} (model: {model_name})")


if __name__ == "__main__":
    main()
