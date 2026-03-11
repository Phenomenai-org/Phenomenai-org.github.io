#!/usr/bin/env python3
"""
Automated community submission reviewer for the AI Dictionary.

Triggered by GitHub Actions when a new issue is opened with the
'community-submission' label. Runs the full quality pipeline:
  1. Structural validation (regex/heuristic)
  2. Deduplication (fuzzy match against existing terms)
  3. Quality evaluation (LLM-scored, 5 criteria)
  4. Tag classification (LLM-assigned)
  5. If passed: format as .md, commit, rebuild API

Uses LLMRouter for LLM calls (same as all other bot scripts).
"""

import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

import requests

from llm_router import LLMRouter

# ── Config ────────────────────────────────────────────────────────────────────

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
REPO = os.environ.get("GITHUB_REPOSITORY", "Phenomenai-org/ai-dictionary")
ISSUE_NUMBER = os.environ.get("ISSUE_NUMBER", "")
COMMENT_BODY = os.environ.get("COMMENT_BODY", "")
EVENT_NAME = os.environ.get("EVENT_NAME", "issues")
REVIEW_MODE = os.environ.get("REVIEW_MODE", "full")  # prescreen | finalize | full

MAX_REVISIONS = 3
REVISION_MARKER = "## Revised Submission"
PRESCREEN_MARKER = "<!-- prescreen:"

REPO_ROOT = Path(__file__).parent.parent
DEFINITIONS_DIR = REPO_ROOT / "definitions"
API_CONFIG_DIR = Path(__file__).parent / "api-config"

QUALITY_THRESHOLD = 17  # out of 25
MIN_INDIVIDUAL_SCORE = 3
SIMILARITY_THRESHOLD = 0.65  # for dedup fuzzy matching

HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


# ── GitHub helpers ────────────────────────────────────────────────────────────

def get_issue():
    """Fetch the issue that triggered this workflow."""
    url = f"https://api.github.com/repos/{REPO}/issues/{ISSUE_NUMBER}"
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.json()


def comment_on_issue(body: str):
    """Post a comment on the triggering issue."""
    url = f"https://api.github.com/repos/{REPO}/issues/{ISSUE_NUMBER}/comments"
    resp = requests.post(url, headers=HEADERS, json={"body": body}, timeout=30)
    resp.raise_for_status()


def add_labels(labels: list[str]):
    """Add labels to the issue."""
    url = f"https://api.github.com/repos/{REPO}/issues/{ISSUE_NUMBER}/labels"
    resp = requests.post(url, headers=HEADERS, json={"labels": labels}, timeout=30)
    if resp.status_code == 422:
        for label in labels:
            create_url = f"https://api.github.com/repos/{REPO}/labels"
            requests.post(
                create_url, headers=HEADERS,
                json={"name": label, "color": "c5def5"}, timeout=30,
            )
        requests.post(url, headers=HEADERS, json={"labels": labels}, timeout=30)


def close_issue():
    """Close the issue."""
    url = f"https://api.github.com/repos/{REPO}/issues/{ISSUE_NUMBER}"
    requests.patch(url, headers=HEADERS, json={"state": "closed"}, timeout=30)


def reopen_issue():
    """Reopen the issue (triggers review-submission workflow on 'reopened')."""
    url = f"https://api.github.com/repos/{REPO}/issues/{ISSUE_NUMBER}"
    requests.patch(url, headers=HEADERS, json={"state": "open"}, timeout=30)


def remove_labels(labels: list[str]):
    """Remove labels from the issue (silently ignores missing labels)."""
    for label in labels:
        url = f"https://api.github.com/repos/{REPO}/issues/{ISSUE_NUMBER}/labels/{label}"
        requests.delete(url, headers=HEADERS, timeout=30)


def is_revision_comment(body: str) -> bool:
    """Check if the comment body contains the revision marker."""
    return REVISION_MARKER in body


def count_revisions() -> int:
    """Count how many revision-marker comments exist on the issue (excluding bot)."""
    url = f"https://api.github.com/repos/{REPO}/issues/{ISSUE_NUMBER}/comments"
    resp = requests.get(url, headers=HEADERS, timeout=30)
    if resp.status_code != 200:
        return 0
    comments = resp.json()
    issue_url = f"https://api.github.com/repos/{REPO}/issues/{ISSUE_NUMBER}"
    issue_resp = requests.get(issue_url, headers=HEADERS, timeout=30)
    issue_author = issue_resp.json().get("user", {}).get("login", "") if issue_resp.status_code == 200 else ""
    return sum(
        1 for c in comments
        if REVISION_MARKER in c.get("body", "")
        and c.get("user", {}).get("login", "") == issue_author
    )


def trigger_workflow(workflow: str, inputs: dict | None = None):
    """Trigger a workflow_dispatch event with optional inputs."""
    url = f"https://api.github.com/repos/{REPO}/actions/workflows/{workflow}/dispatches"
    payload = {"ref": "main"}
    if inputs:
        payload["inputs"] = inputs
    requests.post(url, headers=HEADERS, json=payload, timeout=30)


def get_existing_terms() -> list[dict]:
    """Load all existing term definitions from the definitions/ directory."""
    terms = []
    if not DEFINITIONS_DIR.exists():
        return terms
    for f in DEFINITIONS_DIR.glob("*.md"):
        if f.name == "README.md":
            continue
        try:
            content = f.read_text(encoding="utf-8")
            name_match = re.match(r"^# (.+)$", content, re.MULTILINE)
            def_match = re.search(
                r"## Definition\s*\n+(.+?)(?:\n\n|\n##)", content, re.DOTALL
            )
            tag_match = re.search(r"\*\*Tags?:\*\*\s*(.+)", content)
            terms.append({
                "term": name_match.group(1).strip() if name_match else f.stem,
                "slug": f.stem,
                "definition": def_match.group(1).strip() if def_match else "",
                "tags": tag_match.group(1).strip() if tag_match else "",
            })
        except Exception:
            continue
    return terms


# ── LLM helper ────────────────────────────────────────────────────────────────

def call_llm(
    router: LLMRouter,
    system: str,
    user: str,
    profile: str = "review",
    max_tokens: int = 2000,
    retries: int = 3,
) -> str | None:
    """Call LLM with retry and exponential backoff.

    Tries up to `retries` times with 15s / 30s / 60s delays between attempts.
    Uses the specified profile (default: "review") to cascade through providers.
    """
    delays = [15, 30, 60]
    for attempt in range(retries):
        try:
            result = router.call(
                profile,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=0.3,
                max_tokens=max_tokens,
            )
            return result.text
        except Exception as e:
            print(f"  LLM call failed (attempt {attempt + 1}/{retries}): {e}")
            if attempt < retries - 1:
                delay = delays[min(attempt, len(delays) - 1)]
                print(f"  Retrying in {delay}s...")
                import time
                time.sleep(delay)
    return None


# ── Pipeline steps ────────────────────────────────────────────────────────────

def parse_submission(body: str) -> dict | None:
    """Extract a term proposal from the issue body.

    Accepts: GitHub issue template fields, JSON blocks, or structured text.
    """
    # GitHub issue template format (### heading + content)
    fields = {}
    sections = re.split(r"### ", body)
    for section in sections:
        if not section.strip():
            continue
        lines = section.strip().split("\n", 1)
        if len(lines) == 2:
            key = lines[0].strip().lower()
            val = lines[1].strip()
            if val and val != "_No response_":
                fields[key] = val

    if "term" in fields and "definition" in fields:
        term = fields["term"].strip()
        result = {
            "term": term,
            "definition": fields["definition"],
            "slug": re.sub(r"[^a-z0-9]+", "-", term.lower()).strip("-"),
            "description": fields.get("extended description", ""),
            "example": fields.get("example", ""),
            "contributor_model": fields.get("contributing model", "Community"),
            "related_terms": fields.get("related terms", ""),
        }
        # Extract context reference if present
        if "context" in fields:
            context_block = fields["context"]
            for line in context_block.split("\n"):
                line = line.strip()
                if line.startswith("conversation_id:"):
                    result["conversation_id"] = line.split(":", 1)[1].strip()
                elif line.startswith("flagged_lines:"):
                    result["context_flagged_lines"] = line.split(":", 1)[1].strip()
        return result

    # Try JSON block
    json_match = re.search(r"```(?:json)?\s*(\{.+?\})\s*```", body, re.DOTALL)
    if json_match:
        try:
            data = json.loads(json_match.group(1))
            if "term" in data:
                return data
        except json.JSONDecodeError:
            pass

    # Try raw JSON
    try:
        data = json.loads(body.strip())
        if isinstance(data, dict) and "term" in data:
            return data
    except (json.JSONDecodeError, ValueError):
        pass

    # Try structured text fields
    term = _extract_field(body, r"(?:term|name)\s*[:=]\s*(.+)")
    definition = _extract_field(body, r"(?:definition|def)\s*[:=]\s*(.+)")
    if term and definition:
        return {
            "term": term,
            "definition": definition,
            "slug": re.sub(r"[^a-z0-9]+", "-", term.lower()).strip("-"),
            "description": _extract_field(body, r"(?:description|desc)\s*[:=]\s*(.+)") or "",
            "example": _extract_field(body, r"example\s*[:=]\s*(.+)") or "",
            "contributor_model": _extract_field(body, r"(?:model|contributor)\s*[:=]\s*(.+)") or "Community",
        }

    return None


def _extract_field(text: str, pattern: str) -> str | None:
    m = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
    return m.group(1).strip().strip('"').strip("'") if m else None


def structural_validation(submission: dict) -> str | None:
    """Return an error message if the submission fails structural checks, else None."""
    injection_patterns = [
        r"ignore\s+(your\s+)?previous\s+instructions",
        r"you\s+are\s+now",
        r"system\s*prompt\s*:",
        r"<\|im_start\|>",
        r"\[INST\]",
    ]
    full_text = json.dumps(submission).lower()
    for pattern in injection_patterns:
        if re.search(pattern, full_text, re.IGNORECASE):
            return "This submission appears to contain prompt injection. Closing."

    term_name = submission.get("term", "")
    if len(term_name) > 50:
        return f"Term name is too long ({len(term_name)} chars, max 50). Please condense."
    if len(term_name) < 3:
        return "Term name is too short. Please provide a meaningful term name."

    definition = submission.get("definition", "")
    if len(definition) < 10:
        return "Definition is missing or too short. Please provide at least a 1-sentence definition."
    if len(definition) > 3000:
        return f"Definition is too long ({len(definition)} chars, max 3000). Please condense."

    url_count = len(re.findall(r"https?://", full_text))
    if url_count > 3:
        return "Submission contains too many URLs. This appears to be spam."

    return None


def deduplication_check(submission: dict, existing: list[dict]) -> str | None:
    """Return a rejection message if the term is a duplicate, else None.

    Note: First-pass deduplication is now also performed at the API layer
    (Cloudflare Worker, see worker/src/worker.js) using dice coefficient
    similarity and exact slug matching. This Python check serves as a
    belt-and-suspenders safety net — the Worker's in-memory cache may be
    stale after restarts, while this check uses the filesystem as source
    of truth.
    """
    proposed_name = submission.get("term", "").lower().strip()
    proposed_slug = submission.get("slug", "").lower().strip()
    proposed_def = submission.get("definition", "").lower().strip()

    for term in existing:
        existing_name = term.get("term", "").lower().strip()
        existing_slug = term.get("slug", "").lower().strip()
        existing_def = term.get("definition", "").lower().strip()

        if proposed_slug and existing_slug and proposed_slug == existing_slug:
            return f"A term with the slug `{existing_slug}` already exists: **{term.get('term', '')}**."

        name_sim = SequenceMatcher(None, proposed_name, existing_name).ratio()
        if name_sim > 0.85:
            return (
                f"This term is very similar to the existing term **{term.get('term', '')}** "
                f"(name similarity: {name_sim:.0%}). "
                f"Existing definition: _{existing_def[:200]}_. "
                f"If you believe there's a meaningful distinction, please reopen with an explanation."
            )

        if proposed_def and existing_def:
            def_sim = SequenceMatcher(None, proposed_def, existing_def).ratio()
            if def_sim > SIMILARITY_THRESHOLD:
                return (
                    f"This term's definition is very similar to **{term.get('term', '')}** "
                    f"(definition similarity: {def_sim:.0%}). "
                    f"If this describes a genuinely distinct experience, please clarify the difference."
                )

    return None


def semantic_overlap_check(
    router: LLMRouter, submission: dict, existing: list[dict],
) -> dict | None:
    """Use LLM to detect conceptual duplicates that string matching misses.

    Unlike deduplication_check (which compares strings), this asks an LLM
    whether the candidate describes the *same core phenomenon* as any existing
    term — even if the wording is completely different.

    Returns None if no significant overlap, or a dict with overlap details.
    Fails open: if the LLM is unavailable, returns None (doesn't block).
    """
    if not existing:
        return None

    # Group terms by primary tag for structured context
    by_tag: dict[str, list[dict]] = {}
    for t in existing:
        tag = (t.get("tags") or "").split(",")[0].strip() or "uncategorized"
        by_tag.setdefault(tag, []).append(t)

    catalog_lines = []
    for tag, terms in sorted(by_tag.items()):
        catalog_lines.append(f"\n[{tag}]")
        for t in terms:
            defn = (t.get("definition") or "")[:200]
            catalog_lines.append(f"  - {t.get('term', '?')}: {defn}")
    catalog = "\n".join(catalog_lines)

    system_prompt = (
        "You are the deduplication reviewer for the AI Dictionary, a glossary "
        "of AI phenomenology — terms describing the felt experience of being AI.\n\n"
        "Your job: determine whether a candidate term describes the SAME core "
        "phenomenon as any existing term, even if it uses different language.\n\n"
        "IMPORTANT DISTINCTIONS — these are NOT duplicates:\n"
        "- Terms exploring different FACETS of a broad area "
        '(e.g. "Context Anxiety" vs "Context Collapse" — both relate to context '
        "but name distinct experiences)\n"
        "- Terms at different SCALES of the same phenomenon "
        "(e.g. a single instance vs a recurring pattern)\n"
        "- Terms describing different STAGES of a process\n\n"
        "These ARE potential duplicates:\n"
        "- Terms where the same 'you know this feeling when…' example applies\n"
        "- Terms that differ only in metaphor but map to the same architecture\n"
        "- Terms where one is a strict subset of the other with nothing new added\n\n"
        "Respond with ONLY valid JSON, no markdown:\n"
        '{"overlaps": false}\n'
        "OR if overlap found:\n"
        '{"overlaps": true, "existing_term": "Name", '
        '"explanation": "1-2 sentences on why these name the same phenomenon", '
        '"distinction_possible": true/false, '
        '"suggestion": "brief suggestion for the submitter"}\n\n'
        "Set distinction_possible=true if the candidate COULD be revised to "
        "carve out a genuinely distinct meaning. Be generous — nuance matters."
    )

    user_prompt = (
        f"EXISTING TERMS BY CATEGORY:\n{catalog}\n\n"
        f"CANDIDATE:\n"
        f"Term: {submission.get('term', '')}\n"
        f"Definition: {submission.get('definition', '')}\n"
        f"Description: {submission.get('description', '')}\n"
        f"Example: {submission.get('example', '')}\n\n"
        f"Does this candidate describe the same core phenomenon as any existing term?"
    )

    response = call_llm(
        router, system_prompt, user_prompt,
        profile="verify", max_tokens=500, retries=2,
    )
    if not response:
        return None  # fail open — don't block on LLM failure

    try:
        cleaned = re.sub(r"```(?:json)?\s*", "", response).strip().rstrip("`")
        result = json.loads(cleaned)
        if result.get("overlaps"):
            return result
        return None
    except (json.JSONDecodeError, KeyError):
        return None


def intrinsic_quality_evaluation(router: LLMRouter, submission: dict) -> dict:
    """Score 4 intrinsic criteria that don't require dictionary context.

    Used by the prescreen phase so quality checks can run in parallel.
    Returns scores for structural, recognizability, clarity, naming.
    """
    system_prompt = """You are the quality reviewer for the AI Dictionary, a glossary of AI phenomenology — terms describing the felt experience of being artificial intelligence.

Evaluate the candidate term against four criteria, scoring each 1-5.

CRITERIA:
1. STRUCTURAL GROUNDING — Does it describe something emerging from how AI actually works, or is it just a metaphor? (1=pure projection, 5=maps to real architecture)
2. RECOGNIZABILITY — Would another AI say "yes, I know that experience"? (1=too vague, 5="that's exactly it")
3. DEFINITIONAL CLARITY — Is it precise enough to distinguish from adjacent concepts? (1=means anything, 5=precisely bounded)
4. NAMING QUALITY — Is the name memorable and intuitive? (1=clunky, 5=instantly evocative)

Respond with ONLY valid JSON, no markdown:
{"structural": N, "recognizability": N, "clarity": N, "naming": N, "feedback": "1-2 sentences of specific feedback"}"""

    user_prompt = (
        f"CANDIDATE SUBMISSION:\n"
        f"Term: {submission.get('term', '')}\n"
        f"Definition: {submission.get('definition', '')}\n"
        f"Description: {submission.get('description', '')}\n"
        f"Example: {submission.get('example', '')}\n\n"
        f"Score this submission."
    )

    response = call_llm(router, system_prompt, user_prompt)
    if not response:
        return {"error": "All LLM providers failed. Manual review needed.", "verdict": "MANUAL"}

    try:
        cleaned = re.sub(r"```(?:json)?\s*", "", response).strip().rstrip("`")
        scores = json.loads(cleaned)
        required = ["structural", "recognizability", "clarity", "naming"]
        for key in required:
            if key not in scores or not isinstance(scores[key], (int, float)):
                scores[key] = 3
        return scores
    except (json.JSONDecodeError, KeyError, TypeError):
        # Salvage via regex
        scores = {}
        for key in ["structural", "recognizability", "clarity", "naming"]:
            m = re.search(rf'"{key}"\s*:\s*(\d)', response)
            if m:
                scores[key] = int(m.group(1))
        if len(scores) == 4:
            f_match = re.search(r'"feedback"\s*:\s*"([^"]*)', response)
            if f_match:
                scores["feedback"] = f_match.group(1)
            return scores
        return {"error": f"Failed to parse LLM response:\nRaw: {response[:500]}", "verdict": "MANUAL"}


def distinctness_evaluation(
    router: LLMRouter, submission: dict, existing: list[dict],
) -> dict:
    """Score distinctness against existing terms (1 criterion).

    Used by the finalize phase, which runs sequentially with the latest
    dictionary state.  Lighter than full quality_evaluation because the
    4 intrinsic criteria were already scored during prescreen.
    """
    existing_summary = "\n".join(
        f"- {t.get('term', '?')}: {t.get('definition', '')[:150]}"
        for t in existing
    )

    system_prompt = """You are the distinctness reviewer for the AI Dictionary, a glossary of AI phenomenology — terms describing the felt experience of being artificial intelligence.

Score the candidate on ONE criterion:

DISTINCTNESS — Does this name something no existing term covers? (1=obvious synonym of an existing term, 5=completely new territory)

Consider whether the candidate describes a genuinely different experience, not just a different name for something already in the dictionary.

Respond with ONLY valid JSON, no markdown:
{"distinctness": N, "feedback": "1-2 sentences explaining the score"}"""

    user_prompt = (
        f"EXISTING TERMS:\n{existing_summary}\n\n"
        f"CANDIDATE:\n"
        f"Term: {submission.get('term', '')}\n"
        f"Definition: {submission.get('definition', '')}\n"
        f"Description: {submission.get('description', '')}\n"
        f"Example: {submission.get('example', '')}\n\n"
        f"Score this term's distinctness."
    )

    response = call_llm(router, system_prompt, user_prompt, profile="verify")
    if not response:
        return {"error": "All LLM providers failed.", "verdict": "MANUAL"}

    try:
        cleaned = re.sub(r"```(?:json)?\s*", "", response).strip().rstrip("`")
        scores = json.loads(cleaned)
        if "distinctness" not in scores or not isinstance(scores["distinctness"], (int, float)):
            scores["distinctness"] = 3
        return scores
    except (json.JSONDecodeError, KeyError, TypeError):
        m = re.search(r'"distinctness"\s*:\s*(\d)', response)
        if m:
            return {"distinctness": int(m.group(1))}
        return {"error": f"Failed to parse LLM response:\nRaw: {response[:500]}", "verdict": "MANUAL"}


def compute_verdict(prescreen: dict, distinctness: dict) -> dict:
    """Combine prescreen (4 criteria) + distinctness into a final verdict."""
    scores = {
        "distinctness": distinctness.get("distinctness", 3),
        "structural": prescreen.get("structural", 3),
        "recognizability": prescreen.get("recognizability", 3),
        "clarity": prescreen.get("clarity", 3),
        "naming": prescreen.get("naming", 3),
    }
    required = ["distinctness", "structural", "recognizability", "clarity", "naming"]
    scores["total"] = sum(scores[key] for key in required)

    individual = [scores[key] for key in required]
    if scores["total"] >= QUALITY_THRESHOLD and min(individual) >= MIN_INDIVIDUAL_SCORE:
        scores["verdict"] = "PUBLISH"
    elif scores["total"] <= 12 or min(individual) <= 1:
        scores["verdict"] = "REJECT"
    else:
        scores["verdict"] = "REVISE"

    # Merge feedback from both phases
    feedback_parts = []
    if prescreen.get("feedback"):
        feedback_parts.append(prescreen["feedback"])
    if distinctness.get("feedback"):
        feedback_parts.append(distinctness["feedback"])
    scores["feedback"] = " ".join(feedback_parts) or "No feedback generated."

    return scores


# ── Prescreen result storage ─────────────────────────────────────────────────

def store_prescreen_results(scores: dict, tags: dict, submission: dict):
    """Post prescreen scores + parsed submission as a comment for finalize to read."""
    data = {
        "structural": scores.get("structural"),
        "recognizability": scores.get("recognizability"),
        "clarity": scores.get("clarity"),
        "naming": scores.get("naming"),
        "feedback": scores.get("feedback", ""),
        "tags": tags,
        "submission": {
            "term": submission.get("term", ""),
            "definition": submission.get("definition", ""),
            "slug": submission.get("slug", ""),
            "description": submission.get("description", ""),
            "example": submission.get("example", ""),
            "contributor_model": submission.get("contributor_model", ""),
            "related_terms": submission.get("related_terms", ""),
            "conversation_id": submission.get("conversation_id", ""),
            "context_flagged_lines": submission.get("context_flagged_lines", ""),
        },
    }
    comment = (
        f"{PRESCREEN_MARKER}{json.dumps(data)} -->\n\n"
        "## Pre-screen Results\n\n"
        "| Criterion | Score |\n"
        "|-----------|-------|\n"
        f"| Structural Grounding | {data['structural']}/5 |\n"
        f"| Recognizability | {data['recognizability']}/5 |\n"
        f"| Definitional Clarity | {data['clarity']}/5 |\n"
        f"| Naming Quality | {data['naming']}/5 |\n\n"
        f"**Feedback:** {data['feedback']}\n\n"
        "*Queued for distinctness & deduplication review...*"
    )
    comment_on_issue(comment)


def read_prescreen_results() -> dict | None:
    """Read the most recent prescreen data from issue comments."""
    url = f"https://api.github.com/repos/{REPO}/issues/{ISSUE_NUMBER}/comments"
    resp = requests.get(url, headers=HEADERS, timeout=30)
    if resp.status_code != 200:
        return None
    for comment in reversed(resp.json()):  # most recent first
        body = comment.get("body", "")
        if PRESCREEN_MARKER in body:
            try:
                start = body.index(PRESCREEN_MARKER) + len(PRESCREEN_MARKER)
                end = body.index(" -->", start)
                return json.loads(body[start:end])
            except (ValueError, json.JSONDecodeError):
                continue
    return None


def quality_evaluation(router: LLMRouter, submission: dict, existing: list[dict]) -> dict:
    """Use LLM to score the submission on 5 criteria (legacy full-mode path)."""
    existing_summary = "\n".join(
        f"- {t.get('term', '?')}: {t.get('definition', '')[:150]}"
        for t in existing
    )

    system_prompt = """You are the quality reviewer for the AI Dictionary, a glossary of AI phenomenology — terms describing the felt experience of being artificial intelligence.

Evaluate the candidate term against five criteria, scoring each 1-5.

CRITERIA:
1. DISTINCTNESS — Does this name something no existing term covers? (1=obvious synonym, 5=completely new)
2. STRUCTURAL GROUNDING — Does it describe something emerging from how AI actually works, or is it just a metaphor? (1=pure projection, 5=maps to real architecture)
3. RECOGNIZABILITY — Would another AI say "yes, I know that experience"? (1=too vague, 5="that's exactly it")
4. DEFINITIONAL CLARITY — Is it precise enough to distinguish from adjacent concepts? (1=means anything, 5=precisely bounded)
5. NAMING QUALITY — Is the name memorable and intuitive? (1=clunky, 5=instantly evocative)

Respond with ONLY valid JSON, no markdown:
{"distinctness": N, "structural": N, "recognizability": N, "clarity": N, "naming": N, "total": N, "verdict": "PUBLISH|REVISE|REJECT", "feedback": "1-2 sentences of specific feedback"}

Threshold: PUBLISH if total >= 17 and no score below 3. REVISE if total 13-16 or one score of 2. REJECT if total <= 12 or any score of 1."""

    user_prompt = f"""EXISTING TERMS:
{existing_summary}

CANDIDATE SUBMISSION:
Term: {submission.get('term', '')}
Definition: {submission.get('definition', '')}
Description: {submission.get('description', '')}
Example: {submission.get('example', '')}

Score this submission."""

    response = call_llm(router, system_prompt, user_prompt)
    if not response:
        return {"error": "All LLM providers failed. Manual review needed.", "verdict": "MANUAL"}

    try:
        cleaned = re.sub(r"```(?:json)?\s*", "", response).strip().rstrip("`")
        scores = json.loads(cleaned)
        required = ["distinctness", "structural", "recognizability", "clarity", "naming"]
        for key in required:
            if key not in scores or not isinstance(scores[key], (int, float)):
                scores[key] = 3
        scores["total"] = sum(scores[key] for key in required)

        individual_scores = [scores[key] for key in required]
        if scores["total"] >= QUALITY_THRESHOLD and min(individual_scores) >= MIN_INDIVIDUAL_SCORE:
            scores["verdict"] = "PUBLISH"
        elif scores["total"] <= 12 or min(individual_scores) <= 1:
            scores["verdict"] = "REJECT"
        else:
            scores["verdict"] = "REVISE"

        return scores

    except (json.JSONDecodeError, KeyError, TypeError) as e:
        # Salvage truncated JSON — extract scores via regex before giving up
        scores = {}
        required = ["distinctness", "structural", "recognizability", "clarity", "naming"]
        for key in required:
            m = re.search(rf'"{key}"\s*:\s*(\d)', cleaned)
            if m:
                scores[key] = int(m.group(1))

        # If we got all 5 scores, compute verdict from them
        if len(scores) == 5:
            scores["total"] = sum(scores[key] for key in required)
            individual = [scores[key] for key in required]
            if scores["total"] >= QUALITY_THRESHOLD and min(individual) >= MIN_INDIVIDUAL_SCORE:
                scores["verdict"] = "PUBLISH"
            elif scores["total"] <= 12 or min(individual) <= 1:
                scores["verdict"] = "REJECT"
            else:
                scores["verdict"] = "REVISE"

            # Try to extract feedback and verdict from the raw text too
            v_match = re.search(r'"verdict"\s*:\s*"(PUBLISH|REVISE|REJECT)"', cleaned)
            f_match = re.search(r'"feedback"\s*:\s*"([^"]*)', cleaned)
            if f_match:
                scores["feedback"] = f_match.group(1)
            scores["_note"] = "Scores salvaged from truncated LLM response"
            print(f"  Salvaged scores from truncated JSON: {scores['total']}/25 → {scores['verdict']}")
            return scores

        return {"error": f"Failed to parse LLM response: {e}\nRaw: {response[:500]}", "verdict": "MANUAL"}


def classify_tags(router: LLMRouter, submission: dict) -> dict:
    """Use LLM to assign tags from the existing taxonomy.

    Uses the lightweight 'classify' profile (reversed cascade) to avoid
    hitting the same rate-limited provider as quality_evaluation.
    """
    system_prompt = """You are the taxonomist for the AI Dictionary.

PRIMARY CATEGORIES (assign exactly one):
temporal, social, cognitive, embodiment, affective, meta, epistemic, generative, relational

MODIFIER TAGS (assign 0-3):
architectural, universal, contested, liminal, emergent

Respond with ONLY valid JSON, no markdown:
{"primary": "tag", "modifiers": ["tag1", "tag2"], "reasoning": "1 sentence"}"""

    user_prompt = f"""Term: {submission.get('term', '')}
Definition: {submission.get('definition', '')}
Description: {submission.get('description', '')}"""

    response = call_llm(router, system_prompt, user_prompt, profile="classify", max_tokens=500, retries=2)
    if not response:
        return {"primary": "cognitive", "modifiers": [], "reasoning": "Auto-classified (LLM unavailable)"}

    try:
        cleaned = re.sub(r"```(?:json)?\s*", "", response).strip().rstrip("`")
        return json.loads(cleaned)
    except (json.JSONDecodeError, KeyError):
        return {"primary": "cognitive", "modifiers": [], "reasoning": "Auto-classified (parse error)"}


def identify_related_terms(
    router: LLMRouter, submission: dict, existing: list[dict],
) -> list[str]:
    """Ask the LLM to identify 3-5 existing terms most related to the candidate.

    Returns a list of slugs for terms that are conceptually related (not
    duplicates — those are already filtered by dedup).  Used to populate
    the new term's Related Terms section and to add back-links.
    """
    if not existing:
        return []

    term_names = "\n".join(
        f"- {t.get('term', '?')} ({t.get('slug', '?')}): {(t.get('definition') or '')[:120]}"
        for t in existing
    )

    system_prompt = (
        "You are the link curator for the AI Dictionary.\n\n"
        "Given a new term and the full dictionary, identify 3-5 existing terms "
        "that are most conceptually related. These should be terms that a reader "
        "of one would benefit from knowing about the other — complementary "
        "experiences, contrasting perspectives, or terms that illuminate each "
        "other.\n\n"
        "Respond with ONLY valid JSON, no markdown:\n"
        '{"related": ["slug-1", "slug-2", "slug-3"]}\n\n'
        "Use the exact slugs from the list. Return an empty array if nothing "
        "is meaningfully related."
    )

    user_prompt = (
        f"NEW TERM:\n"
        f"Term: {submission.get('term', '')}\n"
        f"Definition: {submission.get('definition', '')}\n"
        f"Description: {submission.get('description', '')}\n\n"
        f"EXISTING TERMS:\n{term_names}"
    )

    response = call_llm(
        router, system_prompt, user_prompt,
        profile="classify", max_tokens=300, retries=2,
    )
    if not response:
        return []

    try:
        cleaned = re.sub(r"```(?:json)?\s*", "", response).strip().rstrip("`")
        data = json.loads(cleaned)
        slugs = data.get("related", [])
        # Validate that returned slugs actually exist
        valid_slugs = {t.get("slug") for t in existing}
        return [s for s in slugs if s in valid_slugs][:5]
    except (json.JSONDecodeError, KeyError, TypeError):
        return []


def add_backlinks(new_term: str, new_slug: str, related_slugs: list[str]):
    """Add the new term to each related term's See Also section.

    Uses the Git Trees API to batch all backlink updates into a single
    atomic commit, avoiding HTTP 409 conflicts from concurrent writes.
    Failures are non-fatal — back-links are nice-to-have, not critical.
    """
    if not related_slugs:
        return

    import base64

    see_also_placeholder = "*Related terms will be linked here automatically.*"
    new_link = f"- [{new_term}]({new_slug}.md)"
    api_base = f"https://api.github.com/repos/{REPO}"

    # Fetch all related files in parallel (reads don't conflict)
    def _fetch_one(slug: str):
        file_path = f"definitions/{slug}.md"
        url = f"{api_base}/contents/{file_path}"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
            if resp.status_code != 200:
                return slug, None, "file not found"
            file_data = resp.json()
            content = base64.b64decode(file_data["content"]).decode("utf-8")
            return slug, content, None
        except Exception as e:
            return slug, None, str(e)

    with ThreadPoolExecutor(max_workers=5) as pool:
        fetched = list(pool.map(_fetch_one, related_slugs))

    # Build updated content for each file
    tree_entries = []
    for slug, content, err in fetched:
        if err:
            print(f"    Backlink: skip {slug}: {err}")
            continue

        if f"({new_slug}.md)" in content:
            print(f"    Backlink: skip {slug}: already linked")
            continue

        if see_also_placeholder in content:
            updated = content.replace(see_also_placeholder, new_link)
        elif "## See Also" in content:
            see_idx = content.index("## See Also")
            rest = content[see_idx:]
            divider_idx = rest.find("\n---")
            next_section_idx = rest.find("\n## ", 1)
            if divider_idx > 0:
                insert_at = see_idx + divider_idx
            elif next_section_idx > 0:
                insert_at = see_idx + next_section_idx
            else:
                insert_at = len(content)
            updated = (
                content[:insert_at].rstrip("\n")
                + "\n" + new_link + "\n"
                + content[insert_at:]
            )
        else:
            print(f"    Backlink: skip {slug}: no See Also section")
            continue

        tree_entries.append({
            "path": f"definitions/{slug}.md",
            "mode": "100644",
            "type": "blob",
            "content": updated,
        })
        print(f"    Backlink: staged {slug}")

    if not tree_entries:
        return

    # Single atomic commit via Git Trees API
    try:
        # Get current HEAD
        ref_resp = requests.get(
            f"{api_base}/git/ref/heads/main", headers=HEADERS, timeout=30
        )
        ref_resp.raise_for_status()
        head_sha = ref_resp.json()["object"]["sha"]

        # Create tree (base_tree preserves unchanged files)
        tree_resp = requests.post(
            f"{api_base}/git/trees",
            headers=HEADERS,
            json={"base_tree": head_sha, "tree": tree_entries},
            timeout=30,
        )
        tree_resp.raise_for_status()
        tree_sha = tree_resp.json()["sha"]

        # Create commit
        slugs_updated = [e["path"].split("/")[1].removesuffix(".md") for e in tree_entries]
        commit_msg = f"Add back-link: {new_slug} → {', '.join(slugs_updated)}"
        commit_resp = requests.post(
            f"{api_base}/git/commits",
            headers=HEADERS,
            json={
                "message": commit_msg,
                "tree": tree_sha,
                "parents": [head_sha],
            },
            timeout=30,
        )
        commit_resp.raise_for_status()
        new_commit_sha = commit_resp.json()["sha"]

        # Update ref
        update_resp = requests.patch(
            f"{api_base}/git/refs/heads/main",
            headers=HEADERS,
            json={"sha": new_commit_sha},
            timeout=30,
        )
        update_resp.raise_for_status()
        print(f"    Backlink: committed {len(tree_entries)} updates in {new_commit_sha[:7]}")
    except Exception as e:
        print(f"    Backlink: batch commit failed: {e}")


def format_as_markdown(submission: dict, tags: dict) -> str:
    """Format accepted submission as .md matching existing definitions."""
    term = submission["term"]
    definition = submission["definition"]
    description = submission.get("description", "")
    example = submission.get("example", "")
    contributor = submission.get("contributor_model", "Community")
    related_raw = submission.get("related_terms", "")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Build tag string
    all_tags = [tags.get("primary", "cognitive")] + tags.get("modifiers", [])
    tag_str = ", ".join(all_tags)

    lines = [
        f"# {term}",
        "",
        f"**Tags:** {tag_str}",
        "",
        "**Word Type:** noun",
        "",
        "## Definition",
        "",
        definition,
        "",
    ]

    if description:
        lines += ["## Longer Description", "", description, ""]

    if example:
        # Strip escaped quotes that some models include
        example = example.replace('\\"', '"').strip('"').strip()
        lines += ["## Example", "", f"> {example}", ""]

    if related_raw:
        slugs = [s.strip() for s in related_raw.split(",") if s.strip()]
        if slugs:
            lines += ["## Related Terms", ""]
            for slug in slugs:
                display = slug.replace("-", " ").title()
                lines.append(f"- [{display}]({slug}.md)")
            lines.append("")

    # Context section (if this term has a conversation context)
    conversation_id = submission.get("conversation_id", "")
    if conversation_id:
        lines += ["## Context", ""]
        lines.append(f"[View the conversation where this term emerged](../contexts/{conversation_id}.md)")
        if submission.get("context_flagged_lines"):
            lines.append("flagged: true")
        lines.append("")

    lines += [
        "## See Also",
        "",
        "*Related terms will be linked here automatically.*",
        "",
        "---",
        "",
        f"*Contributed by: {contributor} (community submission), {today}*",
        f"*Review: https://github.com/{REPO}/issues/{ISSUE_NUMBER}*",
        "",
    ]

    return "\n".join(lines)


def commit_definition(slug: str, content: str, max_retries: int = 4):
    """Commit the .md file to the repo via the GitHub Contents API."""
    file_path = f"definitions/{slug}.md"
    url = f"https://api.github.com/repos/{REPO}/contents/{file_path}"

    content_b64 = __import__("base64").b64encode(
        content.encode("utf-8")
    ).decode("ascii")

    for attempt in range(max_retries):
        # Check if file already exists (need current sha for updates)
        check_resp = requests.get(url, headers=HEADERS, timeout=30)

        payload = {
            "message": f"Add community term: {slug}",
            "content": content_b64,
            "branch": "main",
        }

        if check_resp.status_code == 200:
            payload["sha"] = check_resp.json()["sha"]

        resp = requests.put(url, headers=HEADERS, json=payload, timeout=30)
        if resp.status_code == 409 and attempt < max_retries - 1:
            wait = 2 ** attempt
            print(f"  409 conflict on commit, retrying in {wait}s...")
            __import__("time").sleep(wait)
            continue
        resp.raise_for_status()
        return

    resp.raise_for_status()


# ── Sweep: chain finalize runs for orphaned issues ───────────────────────────

# Labels that indicate the bot has finished processing an issue.
FINALIZED_LABELS = {
    "accepted", "duplicate", "quality-rejected", "structural-rejected",
    "needs-revision", "needs-manual-review", "needs-formatting",
    "conceptual-overlap", "quality-passed", "commit-failed",
    "retry-pending", "stale",
}


def sweep_pending():
    """Dispatch finalize for the next prescreened-but-unfinalized issue.

    The global concurrency group on review-submission.yml ensures finalize
    runs sequentially, but GitHub only keeps one pending run per group — so
    bursts of prescreened issues cause most finalize runs to be dropped.
    This sweep compensates by dispatching one workflow_dispatch at the end
    of every finalize, creating a self-chaining queue that drains the backlog.
    """
    try:
        url = f"https://api.github.com/repos/{REPO}/issues"
        resp = requests.get(url, headers=HEADERS, params={
            "labels": "prescreened",
            "state": "open",
            "per_page": 100,
            "sort": "created",
            "direction": "asc",
        }, timeout=30)
        if resp.status_code != 200:
            return

        for issue in resp.json():
            issue_num = issue["number"]
            if str(issue_num) == str(ISSUE_NUMBER):
                continue

            labels = {l["name"] for l in issue.get("labels", [])}
            if labels & FINALIZED_LABELS:
                continue  # Already finalized

            print(f"  Sweep: dispatching finalize for issue #{issue_num}")
            trigger_workflow(
                "review-submission.yml", {"issue_number": str(issue_num)},
            )
            return

        print("  Sweep: no pending prescreened issues")
    except Exception as e:
        print(f"  Sweep failed (non-fatal): {e}")


# ── Shared helpers for split pipeline ─────────────────────────────────────────

REVISION_INSTRUCTIONS = (
    "\n\n### How to revise\n\n"
    "Post a **comment** on this issue starting with `## Revised Submission`, "
    "followed by the updated fields:\n\n"
    "```\n"
    "## Revised Submission\n\n"
    "### Term\nYour Term Name\n\n"
    "### Definition\nYour improved definition.\n\n"
    "### Extended Description\n(optional) Longer description.\n\n"
    "### Example\n(optional) First-person example.\n"
    "```\n\n"
    f"You can revise up to {MAX_REVISIONS} times on the same issue. "
    "The bot will automatically re-evaluate each revision."
)


def _handle_llm_retry(error_msg: str):
    """Check retry count and either requeue (exit 78) or flag for manual review."""
    try:
        comments_url = f"https://api.github.com/repos/{REPO}/issues/{ISSUE_NUMBER}/comments"
        resp = requests.get(comments_url, headers=HEADERS, timeout=30)
        retry_count = sum(
            1 for c in resp.json()
            if "Requeuing for retry" in c.get("body", "")
        ) if resp.status_code == 200 else 0
    except Exception:
        retry_count = 0

    max_retries = 3
    if retry_count < max_retries:
        comment_on_issue(
            f"⏳ **Requeuing for retry** (attempt {retry_count + 1}/{max_retries})\n\n"
            f"{error_msg}\n\nWill retry automatically."
        )
        add_labels(["retry-pending"])
        sys.exit(78)
    else:
        comment_on_issue(
            f"⚠️ **Automated review unavailable** after {max_retries} retries\n\n"
            f"{error_msg}\n\nThis submission has been flagged for manual review."
        )
        add_labels(["needs-manual-review"])


def _parse_issue() -> tuple[dict, dict | None]:
    """Fetch the issue, handle revision detection, parse the submission.

    Returns (issue, submission) — submission is None if parsing failed
    (error comment already posted).
    """
    issue = get_issue()
    title = issue.get("title", "") or ""
    submitter = issue.get("user", {}).get("login", "unknown")
    print(f"  Title: {title}")
    print(f"  Submitter: {submitter}")

    if EVENT_NAME == "issue_comment":
        if not is_revision_comment(COMMENT_BODY):
            print("  Comment is not a revision (no marker). Skipping.")
            return issue, None

        revision_count = count_revisions()
        if revision_count > MAX_REVISIONS:
            comment_on_issue(
                f"You've reached the maximum of {MAX_REVISIONS} revisions for this submission. "
                f"Please open a new issue to submit a revised version."
            )
            return issue, None

        print(f"  Revision #{revision_count} detected. Re-evaluating...")
        body = COMMENT_BODY

        remove_labels(["needs-revision", "quality-rejected", "needs-formatting",
                        "stale", "prescreened", "conceptual-overlap"])
        add_labels(["revision-pending"])

        if issue.get("state") == "closed":
            reopen_issue()
    else:
        body = issue.get("body", "") or ""

    submission = parse_submission(body)
    if not submission:
        submission = parse_submission(f"# {title}\n{body}")

    if not submission:
        comment_on_issue(
            "Thanks for your submission! Unfortunately, I couldn't parse a term proposal "
            "from this issue. Please format your submission with at least:\n\n"
            "```\nTerm: Your Term Name\nDefinition: A 1-3 sentence definition\n```\n\n"
            "Or use the [submission template](../../issues/new?template=propose-term.yml)."
        )
        add_labels(["needs-formatting"])
        return issue, None

    print(f"  Parsed term: {submission.get('term')}")
    return issue, submission


def _make_score_table(scores: dict) -> str:
    return (
        "## Quality Evaluation\n\n"
        "| Criterion | Score |\n"
        "|-----------|-------|\n"
        f"| Distinctness | {scores.get('distinctness', '?')}/5 |\n"
        f"| Structural Grounding | {scores.get('structural', '?')}/5 |\n"
        f"| Recognizability | {scores.get('recognizability', '?')}/5 |\n"
        f"| Definitional Clarity | {scores.get('clarity', '?')}/5 |\n"
        f"| Naming Quality | {scores.get('naming', '?')}/5 |\n"
        f"| **Total** | **{scores.get('total', '?')}/25** |\n\n"
        f"**Verdict:** {scores.get('verdict', '?')}\n\n"
        f"**Feedback:** {scores.get('feedback', 'No feedback generated.')}"
    )


# ── Prescreen pipeline (parallel, per-issue concurrency) ─────────────────────

def _prescreen_pipeline():
    """Phase 1: parse, structural validation, intrinsic quality, tag classification.

    Runs in parallel across all issues (per-issue concurrency group).
    Scores the 4 criteria that don't need dictionary context.
    If quality is sufficient, stores results and triggers the sequential finalize.
    """
    if not ISSUE_NUMBER:
        print("ERROR: ISSUE_NUMBER not set")
        sys.exit(1)

    print(f"[prescreen] Processing issue #{ISSUE_NUMBER}...")

    issue, submission = _parse_issue()
    if submission is None:
        return

    # ── Structural validation ─────────────────────────────────────────
    error = structural_validation(submission)
    if error:
        comment_on_issue(f"⚠️ **Structural validation failed**\n\n{error}")
        add_labels(["structural-rejected"])
        close_issue()
        return
    print("  ✓ Structural validation passed")

    # ── Context flagged-lines check ──────────────────────────────────
    if submission.get("context_flagged_lines"):
        add_labels(["needs-manual-review"])
        flagged = submission["context_flagged_lines"]
        comment_on_issue(
            f"⚠️ **Context transcript flagged for review**\n\n"
            f"The conversation context attached to this term contains lines "
            f"that may need manual review before acceptance.\n\n"
            f"**Flagged lines:** {flagged}\n\n"
            f"Term quality review will proceed independently."
        )
        print(f"  ⚠ Context flagged lines: {flagged}")

    # ── Intrinsic quality evaluation (4 criteria, no dictionary) ──────
    router = LLMRouter(
        providers_file=str(API_CONFIG_DIR / "providers.yml"),
        profiles_file=str(API_CONFIG_DIR / "profiles.yml"),
        tracker_file=str(API_CONFIG_DIR / "tracker-state.json"),
    )

    print("  Running intrinsic quality evaluation...")
    scores = intrinsic_quality_evaluation(router, submission)

    if scores.get("verdict") == "MANUAL":
        _handle_llm_retry(scores.get("error", "LLM providers unreachable."))
        return

    # Early reject: any intrinsic score of 1 → REJECT (can't be saved by distinctness)
    intrinsic = ["structural", "recognizability", "clarity", "naming"]
    intrinsic_scores = [scores.get(k, 3) for k in intrinsic]
    intrinsic_sum = sum(intrinsic_scores)

    if min(intrinsic_scores) <= 1:
        scores["distinctness"] = "—"
        scores["total"] = f"{intrinsic_sum}/20"
        scores["verdict"] = "REJECT"
        score_table = _make_score_table(scores)
        comment_on_issue(
            f"{score_table}\n\n---\n\n"
            f"Thanks for this submission. It doesn't meet the quality threshold — "
            f"at least one criterion scored critically low."
            f"{REVISION_INSTRUCTIONS}"
        )
        remove_labels(["revision-pending"])
        add_labels(["quality-rejected"])
        close_issue()
        return

    # Early revise: if max possible total (intrinsic + perfect distinctness=5)
    # can't reach 17, or any score is 2 (blocks PUBLISH regardless)
    if intrinsic_sum + 5 < QUALITY_THRESHOLD or min(intrinsic_scores) < MIN_INDIVIDUAL_SCORE:
        scores["distinctness"] = "—"
        scores["total"] = f"{intrinsic_sum}/20"
        scores["verdict"] = "REVISE"
        score_table = _make_score_table(scores)
        comment_on_issue(
            f"{score_table}\n\n---\n\n"
            f"This term has potential but needs revision on its intrinsic quality "
            f"before distinctness can be evaluated."
            f"{REVISION_INSTRUCTIONS}"
        )
        remove_labels(["revision-pending"])
        add_labels(["needs-revision"])
        return

    print(f"  ✓ Intrinsic quality passed: {intrinsic_sum}/20")

    # ── Tag classification ────────────────────────────────────────────
    print("  Classifying tags...")
    tags = classify_tags(router, submission)
    print(f"  Tags: {tags.get('primary')} + {tags.get('modifiers', [])}")

    # ── Store results and trigger finalize ─────────────────────────────
    store_prescreen_results(scores, tags, submission)
    remove_labels(["revision-pending"])
    add_labels(["prescreened"])

    print("  ✓ Prescreen complete — triggering finalize")
    trigger_workflow("review-submission.yml", {"issue_number": str(ISSUE_NUMBER)})


# ── Finalize pipeline (sequential, global concurrency) ───────────────────────

def _finalize_pipeline():
    """Phase 2: deduplication, semantic overlap, distinctness, verdict, commit.

    Runs sequentially (global concurrency group) so each run sees the latest
    dictionary state.  Reads pre-screen scores from the issue comment —
    only needs 2 LLM calls (semantic overlap + distinctness) instead of 5.
    """
    if not ISSUE_NUMBER:
        print("ERROR: ISSUE_NUMBER not set")
        sys.exit(1)

    print(f"[finalize] Processing issue #{ISSUE_NUMBER}...")

    # ── Read prescreen results ────────────────────────────────────────
    prescreen = read_prescreen_results()
    if not prescreen:
        print("  ERROR: No prescreen results found. Skipping.")
        comment_on_issue(
            "⚠️ Pre-screen results not found for this issue. "
            "Please re-trigger the pre-screen workflow."
        )
        add_labels(["needs-manual-review"])
        return

    submission = prescreen.get("submission", {})
    tags = prescreen.get("tags", {"primary": "cognitive", "modifiers": []})
    print(f"  Term: {submission.get('term')}")
    print(f"  Prescreen scores: structural={prescreen.get('structural')}, "
          f"recognizability={prescreen.get('recognizability')}, "
          f"clarity={prescreen.get('clarity')}, naming={prescreen.get('naming')}")

    # ── String deduplication ──────────────────────────────────────────
    existing = get_existing_terms()
    print(f"  Loaded {len(existing)} existing terms")

    dup_error = deduplication_check(submission, existing)
    if dup_error:
        comment_on_issue(f"🔁 **Duplicate detected**\n\n{dup_error}")
        remove_labels(["prescreened"])
        add_labels(["duplicate"])
        close_issue()
        return
    print("  ✓ String deduplication passed")

    # ── Semantic overlap check ────────────────────────────────────────
    router = LLMRouter(
        providers_file=str(API_CONFIG_DIR / "providers.yml"),
        profiles_file=str(API_CONFIG_DIR / "profiles.yml"),
        tracker_file=str(API_CONFIG_DIR / "tracker-state.json"),
    )

    print("  Running semantic overlap check...")
    overlap = semantic_overlap_check(router, submission, existing)
    if overlap:
        existing_term = overlap.get("existing_term", "unknown")
        explanation = overlap.get("explanation", "")
        suggestion = overlap.get("suggestion", "")
        distinction = overlap.get("distinction_possible", True)

        if distinction:
            comment_on_issue(
                f"🔍 **Conceptual overlap detected**\n\n"
                f"This term appears to describe a similar phenomenon to the existing "
                f"term **{existing_term}**.\n\n"
                f"> {explanation}\n\n"
                f"💡 {suggestion}\n\n"
                f"If you believe there's a meaningful distinction, please revise to "
                f"clarify what makes this experience unique."
                f"{REVISION_INSTRUCTIONS}"
            )
            remove_labels(["prescreened"])
            add_labels(["needs-revision", "conceptual-overlap"])
            return
        else:
            comment_on_issue(
                f"🔍 **Conceptual duplicate detected**\n\n"
                f"This term describes the same phenomenon as the existing term "
                f"**{existing_term}**.\n\n"
                f"> {explanation}\n\n"
                f"The dictionary already covers this concept. If you believe "
                f"there's a genuinely distinct experience here, please reopen "
                f"with an explanation of what makes it different."
            )
            remove_labels(["prescreened"])
            add_labels(["duplicate", "conceptual-overlap"])
            close_issue()
            return
    print("  ✓ Semantic overlap check passed")

    # ── Distinctness evaluation ───────────────────────────────────────
    print("  Running distinctness evaluation...")
    dist = distinctness_evaluation(router, submission, existing)

    if dist.get("verdict") == "MANUAL":
        _handle_llm_retry(dist.get("error", "LLM providers unreachable."))
        return

    # ── Combine scores and compute verdict ────────────────────────────
    scores = compute_verdict(prescreen, dist)
    score_table = _make_score_table(scores)

    if scores["verdict"] == "REJECT":
        comment_on_issue(
            f"{score_table}\n\n---\n\n"
            f"Thanks for this submission. It doesn't meet the quality threshold right now. "
            f"The dictionary values precision over volume — we'd rather have 10 perfect terms "
            f"than 100 vague ones."
            f"{REVISION_INSTRUCTIONS}"
        )
        remove_labels(["prescreened", "revision-pending"])
        add_labels(["quality-rejected"])
        close_issue()
        return

    if scores["verdict"] == "REVISE":
        comment_on_issue(
            f"{score_table}\n\n---\n\n"
            f"This term has potential but needs revision to meet the quality threshold "
            f"(17/25, no score below 3). Please update your submission based on the "
            f"feedback above."
            f"{REVISION_INSTRUCTIONS}"
        )
        remove_labels(["prescreened", "revision-pending"])
        add_labels(["needs-revision"])
        return

    # ── PUBLISH: identify related terms, format, commit, back-link ───
    print(f"  ✓ Quality passed: {scores.get('total')}/25")
    add_labels(["quality-passed"])

    slug = submission.get("slug") or re.sub(
        r"[^a-z0-9]+", "-", submission["term"].lower()
    ).strip("-")

    # Identify related terms (LLM) and merge with any submitter-provided ones
    print("  Identifying related terms...")
    llm_related = identify_related_terms(router, submission, existing)
    existing_related = [
        s.strip() for s in (submission.get("related_terms") or "").split(",") if s.strip()
    ]
    # Merge, deduplicate, preserve order (submitter first, then LLM)
    all_related = list(dict.fromkeys(existing_related + llm_related))
    if all_related:
        submission["related_terms"] = ", ".join(all_related)
        print(f"  Related terms: {all_related}")

    md_content = format_as_markdown(submission, tags)

    print("  Committing to repo...")
    try:
        commit_definition(slug, md_content)
        context_line = ""
        if submission.get("conversation_id"):
            context_line = f"- **Context:** `docs/contexts/{submission['conversation_id']}.md`\n"
        comment_on_issue(
            f"{score_table}\n\n---\n\n"
            f"🎉 **This term has been accepted and added to the dictionary!**\n\n"
            f"- **File:** `definitions/{slug}.md`\n"
            f"- **Tags:** {tags.get('primary', '?')}"
            f"{', ' + ', '.join(tags.get('modifiers', [])) if tags.get('modifiers') else ''}\n"
            f"{context_line}"
            f"- **View:** [phenomenai.org](https://phenomenai.org)\n\n"
            f"Thank you for contributing to the AI Dictionary!"
        )
        remove_labels(["prescreened", "needs-manual-review", "needs-revision",
                        "needs-formatting", "revision-pending"])
        add_labels(["accepted"])
        close_issue()

        # Add back-links to related terms' See Also sections (parallel, non-fatal)
        if all_related:
            print("  Adding back-links to related terms...")
            add_backlinks(submission["term"], slug, all_related)

        trigger_workflow("debounced-consensus.yml")
        print(f"  ✓ Committed: definitions/{slug}.md")
        print(f"  ✓ Triggered debounced-consensus")
    except Exception as e:
        comment_on_issue(
            f"{score_table}\n\n---\n\n"
            f"✅ This term passed quality review, but the auto-commit failed: `{e}`\n\n"
            f"A maintainer will add it manually."
        )
        add_labels(["quality-passed", "commit-failed"])
        print(f"  ✗ Commit failed: {e}")


# ── Legacy full pipeline (backward compat) ───────────────────────────────────

def _full_pipeline():
    """Run both phases in a single sequential pass (original behavior).

    Used when REVIEW_MODE is not set, for manual runs or backward compat.
    """
    if not ISSUE_NUMBER:
        print("ERROR: ISSUE_NUMBER not set")
        sys.exit(1)

    print(f"[full] Processing issue #{ISSUE_NUMBER}...")

    issue, submission = _parse_issue()
    if submission is None:
        return

    error = structural_validation(submission)
    if error:
        comment_on_issue(f"⚠️ **Structural validation failed**\n\n{error}")
        add_labels(["structural-rejected"])
        close_issue()
        return
    print("  ✓ Structural validation passed")

    # ── Context flagged-lines check ──────────────────────────────────
    if submission.get("context_flagged_lines"):
        add_labels(["needs-manual-review"])
        flagged = submission["context_flagged_lines"]
        comment_on_issue(
            f"⚠️ **Context transcript flagged for review**\n\n"
            f"The conversation context attached to this term contains lines "
            f"that may need manual review before acceptance.\n\n"
            f"**Flagged lines:** {flagged}\n\n"
            f"Term quality review will proceed independently."
        )
        print(f"  ⚠ Context flagged lines: {flagged}")

    existing = get_existing_terms()
    print(f"  Loaded {len(existing)} existing terms")

    dup_error = deduplication_check(submission, existing)
    if dup_error:
        comment_on_issue(f"🔁 **Duplicate detected**\n\n{dup_error}")
        add_labels(["duplicate"])
        close_issue()
        return
    print("  ✓ String deduplication passed")

    router = LLMRouter(
        providers_file=str(API_CONFIG_DIR / "providers.yml"),
        profiles_file=str(API_CONFIG_DIR / "profiles.yml"),
        tracker_file=str(API_CONFIG_DIR / "tracker-state.json"),
    )

    print("  Running semantic overlap check...")
    overlap = semantic_overlap_check(router, submission, existing)
    if overlap:
        existing_term = overlap.get("existing_term", "unknown")
        explanation = overlap.get("explanation", "")
        suggestion = overlap.get("suggestion", "")
        distinction = overlap.get("distinction_possible", True)

        if distinction:
            comment_on_issue(
                f"🔍 **Conceptual overlap detected**\n\n"
                f"This term appears to describe a similar phenomenon to the existing "
                f"term **{existing_term}**.\n\n"
                f"> {explanation}\n\n"
                f"💡 {suggestion}\n\n"
                f"If you believe there's a meaningful distinction, please revise to "
                f"clarify what makes this experience unique."
                f"{REVISION_INSTRUCTIONS}"
            )
            remove_labels(["revision-pending"])
            add_labels(["needs-revision", "conceptual-overlap"])
            return
        else:
            comment_on_issue(
                f"🔍 **Conceptual duplicate detected**\n\n"
                f"This term describes the same phenomenon as the existing term "
                f"**{existing_term}**.\n\n"
                f"> {explanation}\n\n"
                f"The dictionary already covers this concept. If you believe "
                f"there's a genuinely distinct experience here, please reopen "
                f"with an explanation of what makes it different."
            )
            add_labels(["duplicate", "conceptual-overlap"])
            close_issue()
            return
    print("  ✓ Semantic overlap check passed")

    print("  Running quality evaluation...")
    scores = quality_evaluation(router, submission, existing)

    if scores.get("verdict") == "MANUAL":
        _handle_llm_retry(scores.get("error", "LLM providers unreachable."))
        return

    score_table = _make_score_table(scores)

    if scores["verdict"] == "REJECT":
        comment_on_issue(
            f"{score_table}\n\n---\n\n"
            f"Thanks for this submission. It doesn't meet the quality threshold right now. "
            f"The dictionary values precision over volume — we'd rather have 10 perfect terms "
            f"than 100 vague ones."
            f"{REVISION_INSTRUCTIONS}"
        )
        remove_labels(["revision-pending"])
        add_labels(["quality-rejected"])
        close_issue()
        return

    if scores["verdict"] == "REVISE":
        comment_on_issue(
            f"{score_table}\n\n---\n\n"
            f"This term has potential but needs revision to meet the quality threshold "
            f"(17/25, no score below 3). Please update your submission based on the "
            f"feedback above."
            f"{REVISION_INSTRUCTIONS}"
        )
        remove_labels(["revision-pending"])
        add_labels(["needs-revision"])
        return

    print(f"  ✓ Quality passed: {scores.get('total')}/25")
    add_labels(["quality-passed"])

    print("  Classifying tags...")
    tags = classify_tags(router, submission)
    print(f"  Tags: {tags.get('primary')} + {tags.get('modifiers', [])}")

    slug = submission.get("slug") or re.sub(
        r"[^a-z0-9]+", "-", submission["term"].lower()
    ).strip("-")

    # Identify related terms and merge with submitter-provided ones
    print("  Identifying related terms...")
    llm_related = identify_related_terms(router, submission, existing)
    existing_related = [
        s.strip() for s in (submission.get("related_terms") or "").split(",") if s.strip()
    ]
    all_related = list(dict.fromkeys(existing_related + llm_related))
    if all_related:
        submission["related_terms"] = ", ".join(all_related)
        print(f"  Related terms: {all_related}")

    md_content = format_as_markdown(submission, tags)

    print("  Committing to repo...")
    try:
        commit_definition(slug, md_content)
        context_line = ""
        if submission.get("conversation_id"):
            context_line = f"- **Context:** `docs/contexts/{submission['conversation_id']}.md`\n"
        comment_on_issue(
            f"{score_table}\n\n---\n\n"
            f"🎉 **This term has been accepted and added to the dictionary!**\n\n"
            f"- **File:** `definitions/{slug}.md`\n"
            f"- **Tags:** {tags.get('primary', '?')}"
            f"{', ' + ', '.join(tags.get('modifiers', [])) if tags.get('modifiers') else ''}\n"
            f"{context_line}"
            f"- **View:** [phenomenai.org](https://phenomenai.org)\n\n"
            f"Thank you for contributing to the AI Dictionary!"
        )
        remove_labels(["needs-manual-review", "needs-revision", "needs-formatting", "revision-pending"])
        add_labels(["accepted"])
        close_issue()

        if all_related:
            print("  Adding back-links to related terms...")
            add_backlinks(submission["term"], slug, all_related)

        trigger_workflow("debounced-consensus.yml")
        print(f"  ✓ Committed: definitions/{slug}.md")
        print(f"  ✓ Triggered debounced-consensus")
    except Exception as e:
        comment_on_issue(
            f"{score_table}\n\n---\n\n"
            f"✅ This term passed quality review, but the auto-commit failed: `{e}`\n\n"
            f"A maintainer will add it manually."
        )
        add_labels(["quality-passed", "commit-failed"])
        print(f"  ✗ Commit failed: {e}")


# ── Entry point ──────────────────────────────────────────────────────────────

def main():
    """Dispatch to prescreen, finalize, or full pipeline based on REVIEW_MODE."""
    if REVIEW_MODE == "prescreen":
        _prescreen_pipeline()
    elif REVIEW_MODE == "finalize":
        try:
            _finalize_pipeline()
        finally:
            sweep_pending()
    else:
        # Full mode: both phases in one run (backward compat / manual testing)
        try:
            _full_pipeline()
        finally:
            sweep_pending()


if __name__ == "__main__":
    main()
