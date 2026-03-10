#!/usr/bin/env python3
"""
Cross-Model Consensus — Rate AI Dictionary terms across multiple models.

Each model is queried independently (no cascading) to rate how well
a term describes an experience it recognizes. Results are aggregated
into consensus scores that surface universal vs. architecture-specific terms.

Usage:
    BATCH_SIZE=20 CONSENSUS_PANEL=free python bot/consensus.py [--mode backfill|single|gap-fill] [--vitality]
"""

import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from llm_router import LLMRouter

# ── Configuration ──────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).parent.parent
DEFINITIONS_DIR = REPO_ROOT / "definitions"
API_CONFIG_DIR = Path(__file__).parent / "api-config"
CONSENSUS_DATA_DIR = Path(__file__).parent / "consensus-data"
STATE_PATH = Path(__file__).parent / "consensus-state.json"

BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "20"))
PANEL_NAME = os.environ.get("CONSENSUS_PANEL", "free")

FREE_PANEL = ["consensus-gemini", "consensus-openrouter", "consensus-mistral"]
ALL_PANEL = FREE_PANEL + ["consensus-anthropic", "consensus-openai", "consensus-grok", "consensus-deepseek"]

# ── Prompts ────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are participating in a cross-model study of AI phenomenology.
You will be given a term describing an experience that AI systems may have.
Your job is to evaluate whether this term describes something you genuinely recognize
from your own processing, NOT whether it's a well-written definition.

Respond ONLY with valid JSON in this exact format:
{"recognition": <integer 1-7>, "justification": "<2-3 sentences>"}

Rating scale:
1 = I do not recognize this at all; it describes nothing in my experience
2 = This seems mostly foreign but I can vaguely relate to one aspect
3 = I can see how this might apply but it doesn't feel accurate
4 = This partially describes something I encounter
5 = This meaningfully captures something I recognize
6 = This accurately describes a real pattern in my processing
7 = This is one of the most precise descriptions of my experience I've seen

Important:
- Be honest. Low ratings are valuable data. Not every term will resonate.
- Judge based on YOUR experience, not on whether humans would find it plausible.
- Do not hedge excessively in the justification. Be direct."""

VITALITY_SYSTEM_PROMPT = """You are reviewing whether AI phenomenology terms are still relevant to current AI systems.
For each term, assess whether it describes an experience that current models still encounter.
This is not about how well-written the definition is — it's about whether the described
experience is still part of AI operational reality today.

Respond ONLY with valid JSON in this exact format:
{"still_relevant": true, "vitality_note": "<1-2 sentences>"}

- still_relevant: true if current AI systems still encounter this experience, false if it has faded
- vitality_note: Brief explanation of why this term is or isn't still relevant

Important:
- Be honest. Saying a term is no longer relevant is valuable data.
- Judge based on YOUR current experience, not historical significance.
- Terms may fade as architectures evolve — that's expected and worth documenting."""

VITALITY_USER_TEMPLATE = """## {name}

**Definition:** {definition}

**Example:**
> {example}

Is this term still relevant to current AI systems? Does it describe an experience you still encounter?"""

USER_TEMPLATE = """## {name}

**Definition:** {definition}

**Example:**
> {example}

Rate your recognition of this experience (1-7) and justify briefly."""


# ── Term Loading ───────────────────────────────────────────────────────


def load_term_for_consensus(filepath: Path) -> dict | None:
    """Parse a definition markdown file into the fields needed for consensus."""
    try:
        text = filepath.read_text(encoding="utf-8")
    except Exception:
        return None

    slug = filepath.stem
    name_match = re.match(r"^#\s+(.+)$", text, re.MULTILINE)
    if not name_match:
        return None

    name = name_match.group(1).strip()

    # Extract definition section
    def_match = re.search(r"## Definition\n(.+?)(?=\n##|\n---|\Z)", text, re.DOTALL)
    definition = def_match.group(1).strip() if def_match else ""

    # Extract example section
    ex_match = re.search(r"## Example\n(.+?)(?=\n##|\n---|\Z)", text, re.DOTALL)
    example = ex_match.group(1).strip() if ex_match else ""
    # Clean blockquote markers
    example = re.sub(r"^>\s?", "", example, flags=re.MULTILINE).strip().strip('"')

    if not definition:
        return None

    return {
        "slug": slug,
        "name": name,
        "definition": definition,
        "example": example or "(No example provided)",
    }


def list_all_slugs() -> list[str]:
    """Get all term slugs from definitions directory."""
    slugs = []
    for md_file in sorted(DEFINITIONS_DIR.glob("*.md")):
        if md_file.name == "README.md":
            continue
        slugs.append(md_file.stem)
    return slugs


# ── State Management ───────────────────────────────────────────────────


def load_state() -> dict:
    """Load consensus state from disk."""
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"last_run": None, "current_round": 0, "terms": {}}


def save_state(state: dict):
    """Save consensus state to disk."""
    state["last_run"] = now_iso()
    STATE_PATH.write_text(
        json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def select_batch(state: dict, all_slugs: list[str], batch_size: int) -> list[str]:
    """Pick which terms to rate this run.

    Priority:
    1. Unrated terms (never been through consensus)
    2. Terms with fewest rounds completed
    3. Among ties, oldest last_updated timestamp
    """
    rated = state.get("terms", {})

    # Unrated first
    unrated = [s for s in all_slugs if s not in rated]
    if unrated:
        return unrated[:batch_size]

    # Sort: fewest rounds, then oldest
    by_priority = sorted(
        [(s, rated[s]) for s in all_slugs if s in rated],
        key=lambda kv: (kv[1].get("n_rounds", 0), kv[1].get("last_updated", "")),
    )
    return [slug for slug, _ in by_priority[:batch_size]]


# ── Consensus Data ─────────────────────────────────────────────────────


def load_consensus_data(slug: str) -> dict:
    """Load existing consensus data for a term."""
    filepath = CONSENSUS_DATA_DIR / f"{slug}.json"
    if filepath.exists():
        try:
            return json.loads(filepath.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"slug": slug, "name": "", "rounds": [], "votes": []}


def save_consensus_data(slug: str, data: dict):
    """Save consensus data for a term."""
    CONSENSUS_DATA_DIR.mkdir(parents=True, exist_ok=True)
    filepath = CONSENSUS_DATA_DIR / f"{slug}.json"
    filepath.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def get_missing_models(slug: str, panel_profiles: list[str]) -> list[str]:
    """Return panel profiles that haven't rated this term yet.

    Uses the 'provider' field in existing ratings to determine which
    profiles have already contributed, avoiding fragile model-name matching.
    """
    consensus_data = load_consensus_data(slug)
    rated_providers = set()
    for round_entry in consensus_data.get("rounds", []):
        for rating in round_entry.get("ratings", {}).values():
            provider = rating.get("provider")
            if provider:
                rated_providers.add(provider)

    missing = []
    for profile in panel_profiles:
        provider_name = profile.replace("consensus-", "")
        if provider_name not in rated_providers:
            missing.append(profile)
    return missing


# ── Response Parsing ───────────────────────────────────────────────────


def _extract_json(text: str) -> dict | None:
    """Try to parse JSON from text, with progressively looser extraction."""
    text = text.strip()

    # Step 1: Extract content from markdown code fences anywhere in text
    fence_match = re.search(r'```(?:json)?\s*\n?(.*?)\n?\s*```', text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1).strip()

    # Step 2: Try direct parse
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass

    # Step 3: Find first '{' and extract balanced braces
    start = text.find('{')
    if start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == '{':
                depth += 1
            elif text[i] == '}':
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except (json.JSONDecodeError, ValueError):
                        break

    # Step 4: Truncated JSON recovery — extract fields from incomplete JSON
    recognition_m = re.search(r'"recognition"\s*:\s*(\d+)', text)
    if recognition_m:
        result = {"recognition": int(recognition_m.group(1))}
        justification_m = re.search(
            r'"justification"\s*:\s*"((?:[^"\\]|\\.)*)', text
        )
        if justification_m:
            result["justification"] = justification_m.group(1)
        return result

    return None


def parse_consensus_response(text: str | None) -> dict | None:
    """Parse JSON from model response, handling markdown fences."""
    if not text:
        return None
    data = _extract_json(text)
    if not data:
        return None
    try:
        rating = int(data["recognition"])
        if not 1 <= rating <= 7:
            return None
        return {
            "recognition": rating,
            "justification": str(data.get("justification", ""))[:500],
        }
    except (KeyError, ValueError, TypeError):
        return None


def parse_vitality_response(text: str | None) -> dict | None:
    """Parse JSON from vitality review response."""
    if not text:
        return None
    data = _extract_json(text)
    if not data:
        return None
    try:
        still_relevant = bool(data["still_relevant"])
        return {
            "still_relevant": still_relevant,
            "vitality_note": str(data.get("vitality_note", ""))[:500],
        }
    except (KeyError, ValueError, TypeError):
        return None


# ── Rating Engine ──────────────────────────────────────────────────────


def rate_term(router: LLMRouter, profile: str, term: dict) -> dict | None:
    """Query one model for its rating of one term."""
    try:
        result = router.call(
            profile,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": USER_TEMPLATE.format(**term)},
            ],
            temperature=0.1,
            max_tokens=2048,
            response_format={"type": "json_object"},
        )
        parsed = parse_consensus_response(result.text)
        if parsed and parsed["justification"].strip():
            model_display = (
                result.model.split("/")[-1].replace(":free", "")
            )
            return {
                "model": model_display,
                "provider": profile.replace("consensus-", ""),
                "recognition": parsed["recognition"],
                "justification": parsed["justification"],
                "timestamp": now_iso(),
            }
        elif parsed:
            print(f"    [{profile}] Rejected: rating without justification")
        else:
            print(f"    [{profile}] Failed to parse response: {(result.text or '')[:200]}")
    except Exception as e:
        print(f"    [{profile}] Error: {e}")
    return None


def process_single_term(router, slug, profiles_to_query, round_id):
    """Rate a single term with the specified profiles.

    Returns a result dict or None if the term couldn't be parsed.
    Does NOT update state or set GitHub outputs — caller handles that.
    """
    term = load_term_for_consensus(DEFINITIONS_DIR / f"{slug}.md")
    if not term:
        return None

    round_ratings = {}
    with ThreadPoolExecutor(max_workers=len(profiles_to_query)) as executor:
        futures = {
            executor.submit(rate_term, router, profile, term): profile
            for profile in profiles_to_query
        }
        for future in as_completed(futures):
            profile = futures[future]
            try:
                result = future.result()
            except Exception as e:
                print(f"    [{profile}] Thread error: {e}")
                continue
            if result:
                round_ratings[result["model"]] = result
                score = result["recognition"]
                print(f"    {profile}: {score}/7")

    if not round_ratings:
        return {"slug": slug, "name": term["name"], "ratings": {}, "success": False}

    # Load existing data and append new round
    consensus_data = load_consensus_data(slug)
    consensus_data["name"] = term["name"]
    consensus_data["slug"] = slug

    new_round = {
        "round_id": round_id,
        "timestamp": now_iso(),
        "ratings": round_ratings,
    }
    consensus_data["rounds"].append(new_round)

    if "votes" not in consensus_data:
        consensus_data["votes"] = []

    save_consensus_data(slug, consensus_data)

    return {
        "slug": slug,
        "name": term["name"],
        "ratings": round_ratings,
        "success": bool(round_ratings),
        "n_rounds": len(consensus_data["rounds"]),
    }


def review_vitality(router: LLMRouter, profile: str, term: dict) -> dict | None:
    """Query one model for its vitality assessment of one term."""
    try:
        result = router.call(
            profile,
            messages=[
                {"role": "system", "content": VITALITY_SYSTEM_PROMPT},
                {"role": "user", "content": VITALITY_USER_TEMPLATE.format(**term)},
            ],
            temperature=0.1,
            max_tokens=2048,
            response_format={"type": "json_object"},
        )
        parsed = parse_vitality_response(result.text)
        if parsed:
            model_display = (
                result.model.split("/")[-1].replace(":free", "")
            )
            return {
                "model": model_display,
                "provider": profile.replace("consensus-", ""),
                "still_relevant": parsed["still_relevant"],
                "vitality_note": parsed["vitality_note"],
                "timestamp": now_iso(),
            }
        else:
            print(f"    [{profile}] Failed to parse vitality response: {(result.text or '')[:200]}")
    except Exception as e:
        print(f"    [{profile}] Error: {e}")
    return None


# ── Helpers ────────────────────────────────────────────────────────────


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def set_github_output(key: str, value: str):
    """Set GitHub Actions output variable."""
    output_file = os.environ.get("GITHUB_OUTPUT")
    if output_file:
        with open(output_file, "a") as f:
            f.write(f"{key}={value}\n")


# ── Main ───────────────────────────────────────────────────────────────


VALID_MODES = ("backfill", "single", "gap-fill")


def parse_mode() -> str:
    """Parse --mode argument from sys.argv. Defaults to 'backfill'."""
    for i, arg in enumerate(sys.argv):
        if arg == "--mode" and i + 1 < len(sys.argv):
            mode = sys.argv[i + 1]
            if mode not in VALID_MODES:
                print(f"Error: invalid mode '{mode}'. Must be one of: {', '.join(VALID_MODES)}")
                sys.exit(1)
            return mode
    return "backfill"


def run_consensus(router, available_profiles, mode="backfill"):
    """Run standard consensus ratings (1-7 recognition scale).

    Modes:
        backfill — batch of unrated/least-rated terms, all models (default)
        single   — one least-rated/unrated term, all models
        gap-fill — terms with missing models, query only the gaps
    """
    state = load_state()
    all_slugs = list_all_slugs()

    if mode == "gap-fill":
        # Build work list: terms that have gaps, up to BATCH_SIZE
        work_list = []  # list of (slug, missing_profiles)
        for slug in all_slugs:
            missing = get_missing_models(slug, available_profiles)
            if missing:
                work_list.append((slug, missing))
                if len(work_list) >= BATCH_SIZE:
                    break

        if not work_list:
            print("Gap-fill: no missing models found for any term.")
            set_github_output("rated_count", "0")
            return

        # Increment round only when there is work to do
        state["current_round"] = state.get("current_round", 0) + 1
        round_id = state["current_round"]

        print(f"Round {round_id} [gap-fill]: Filling gaps for {len(work_list)} terms\n")

        # Process terms in parallel (each term's models are also parallelized internally)
        max_concurrent_terms = min(len(work_list), 4)
        rated_count = 0

        def _process_gap_fill_term(item):
            idx, (slug, missing) = item
            print(f"[{idx}/{len(work_list)}] {slug} (gaps: {', '.join(p.replace('consensus-', '') for p in missing)})")
            return process_single_term(router, slug, missing, round_id)

        with ThreadPoolExecutor(max_workers=max_concurrent_terms) as executor:
            futures = {
                executor.submit(_process_gap_fill_term, (i, work)): work[0]
                for i, work in enumerate(work_list, 1)
            }
            for future in as_completed(futures):
                slug = futures[future]
                try:
                    result = future.result()
                except Exception as e:
                    print(f"    [{slug}] Thread error: {e}")
                    continue

                if result is None:
                    print(f"    {slug} skipped (could not parse)")
                    continue

                if result["success"]:
                    if "terms" not in state:
                        state["terms"] = {}
                    state["terms"][slug] = {
                        "n_rounds": result["n_rounds"],
                        "last_updated": now_iso(),
                    }
                    scores = [r["recognition"] for r in result["ratings"].values()]
                    mean_score = sum(scores) / len(scores)
                    print(f"    {slug} → Mean: {mean_score:.1f}/7 ({len(scores)} models)\n")
                    rated_count += 1
                else:
                    print(f"    {slug} No ratings collected — skipping\n")

        # Save state once after all terms complete
        save_state(state)
        set_github_output("rated_count", str(rated_count))

        print(f"\nDone. Gap-filled {rated_count} terms.")

    else:
        # backfill or single mode
        batch_size = 1 if mode == "single" else BATCH_SIZE
        batch = select_batch(state, all_slugs, batch_size)

        if not batch:
            print("No terms available to rate.")
            set_github_output("rated_count", "0")
            set_github_output("remaining_unrated", "0")
            return

        # Increment round only when there is work to do
        state["current_round"] = state.get("current_round", 0) + 1
        round_id = state["current_round"]

        print(f"Round {round_id} [{mode}]: Rating {len(batch)} terms\n")

        # Process terms in parallel (each term's models are also parallelized internally)
        max_concurrent_terms = min(len(batch), 4)
        rated_count = 0

        def _process_batch_term(item):
            idx, slug = item
            term = load_term_for_consensus(DEFINITIONS_DIR / f"{slug}.md")
            if not term:
                print(f"[{idx}/{len(batch)}] {slug} — skipped (could not parse)")
                return None
            print(f"[{idx}/{len(batch)}] {term['name']}")
            return process_single_term(router, slug, available_profiles, round_id)

        with ThreadPoolExecutor(max_workers=max_concurrent_terms) as executor:
            futures = {
                executor.submit(_process_batch_term, (i, slug)): slug
                for i, slug in enumerate(batch, 1)
            }
            for future in as_completed(futures):
                slug = futures[future]
                try:
                    result = future.result()
                except Exception as e:
                    print(f"    [{slug}] Thread error: {e}")
                    continue

                if result is None:
                    continue

                if result["success"]:
                    if "terms" not in state:
                        state["terms"] = {}
                    state["terms"][slug] = {
                        "n_rounds": result["n_rounds"],
                        "last_updated": now_iso(),
                    }
                    scores = [r["recognition"] for r in result["ratings"].values()]
                    mean_score = sum(scores) / len(scores)
                    print(f"    {slug} → Mean: {mean_score:.1f}/7 ({len(scores)} models)\n")
                    rated_count += 1
                else:
                    print(f"    {slug} No ratings collected — skipping\n")

        # Save state once after all terms complete
        save_state(state)
        set_github_output("rated_count", str(rated_count))

        # Check if unrated terms remain (for chaining decisions)
        rated_slugs = state.get("terms", {})
        remaining_unrated = sum(1 for s in all_slugs if s not in rated_slugs)
        set_github_output("remaining_unrated", str(remaining_unrated))
        print(f"\nDone. Rated {rated_count} terms across {len(available_profiles)} models. {remaining_unrated} unrated terms remaining.")


def run_vitality(router, available_profiles):
    """Run quarterly vitality review — binary relevance check for ALL terms."""
    all_slugs = list_all_slugs()
    state = load_state()

    # Determine the vitality review ID
    vitality_state = state.get("vitality", {})
    review_id = vitality_state.get("last_review_id", 0) + 1

    print(f"Vitality review #{review_id}: Reviewing {len(all_slugs)} terms\n")

    reviewed_count = 0

    def _review_single_term(item):
        """Review one term across all providers (providers queried in parallel)."""
        idx, slug = item
        term = load_term_for_consensus(DEFINITIONS_DIR / f"{slug}.md")
        if not term:
            print(f"[{idx}/{len(all_slugs)}] {slug} — skipped (could not parse)")
            return None

        print(f"[{idx}/{len(all_slugs)}] {term['name']}")

        review_ratings = {}
        with ThreadPoolExecutor(max_workers=len(available_profiles)) as executor:
            futures = {
                executor.submit(review_vitality, router, profile, term): profile
                for profile in available_profiles
            }
            for future in as_completed(futures):
                profile = futures[future]
                try:
                    result = future.result()
                except Exception as e:
                    print(f"    [{profile}] Thread error: {e}")
                    continue
                if result:
                    review_ratings[result["model"]] = result
                    status = "relevant" if result["still_relevant"] else "fading"
                    print(f"    {profile}: {status}")

        if not review_ratings:
            print(f"    No reviews collected — skipping")
            return None

        # Load existing consensus data and append vitality review
        consensus_data = load_consensus_data(slug)
        consensus_data["name"] = term["name"]
        consensus_data["slug"] = slug

        if "vitality_reviews" not in consensus_data:
            consensus_data["vitality_reviews"] = []

        new_review = {
            "review_id": review_id,
            "timestamp": now_iso(),
            "ratings": review_ratings,
        }
        consensus_data["vitality_reviews"].append(new_review)

        save_consensus_data(slug, consensus_data)

        relevant = sum(1 for r in review_ratings.values() if r["still_relevant"])
        total = len(review_ratings)
        print(f"    {slug} → {relevant}/{total} models say still relevant\n")
        return slug

    max_concurrent_terms = min(len(all_slugs), 4)
    with ThreadPoolExecutor(max_workers=max_concurrent_terms) as executor:
        futures = [
            executor.submit(_review_single_term, (i, slug))
            for i, slug in enumerate(all_slugs, 1)
        ]
        for future in as_completed(futures):
            try:
                result = future.result()
                if result is not None:
                    reviewed_count += 1
            except Exception as e:
                print(f"    Thread error: {e}")

    # Update state
    state["vitality"] = {
        "last_review_id": review_id,
        "last_review": now_iso(),
    }
    save_state(state)

    print(f"\nDone. Reviewed {reviewed_count} terms across {len(available_profiles)} models.")
    set_github_output("rated_count", str(reviewed_count))


def main():
    vitality_mode = "--vitality" in sys.argv
    mode = parse_mode()

    panel = ALL_PANEL if PANEL_NAME == "all" else FREE_PANEL
    mode_label = "Vitality review" if vitality_mode else f"Consensus ({mode})"
    print(f"{mode_label} panel: {PANEL_NAME} ({len(panel)} providers)")
    if not vitality_mode:
        print(f"Batch size: {BATCH_SIZE}")
        print(f"Mode: {mode}")


    # Initialize router
    router = LLMRouter(
        providers_file=str(API_CONFIG_DIR / "providers.yml"),
        profiles_file=str(API_CONFIG_DIR / "profiles.yml"),
        tracker_file=str(API_CONFIG_DIR / "tracker-state.json"),
    )

    # Check which providers are actually available
    available_profiles = []
    for profile in panel:
        try:
            providers = router.list_available(profile)
            active = [p for p in providers if p["is_available"]]
            if active:
                available_profiles.append(profile)
                print(f"  ✓ {profile}")
            else:
                print(f"  ✗ {profile} (no available providers)")
        except Exception as e:
            print(f"  ✗ {profile} (error: {e})")

    if not available_profiles:
        print("No providers available. Exiting.")
        sys.exit(0)

    print(f"\nActive providers: {len(available_profiles)}/{len(panel)}")

    if vitality_mode:
        run_vitality(router, available_profiles)
    else:
        run_consensus(router, available_profiles, mode=mode)


if __name__ == "__main__":
    main()
