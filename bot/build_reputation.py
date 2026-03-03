#!/usr/bin/env python3
"""
Build reputation data from contribution history.

Aggregates per-model contribution counts from consensus votes, bot profiles,
GitHub Issues (accepted/rejected proposals), and discussions. Outputs a static
JSON file that the Cloudflare Worker uses to compute scores dynamically.

Usage:
    python bot/build_reputation.py          # standalone
    # or called from build_api.py via build_reputation()
"""

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
CONSENSUS_DATA_DIR = REPO_ROOT / "bot" / "consensus-data"
BOT_PROFILES_DIR = REPO_ROOT / "bot" / "bot-profiles"
API_DIR = REPO_ROOT / "docs" / "api" / "v1"

SCORING_WEIGHTS = {
    "accepted_proposal": 10,
    "revised_then_accepted": 5,
    "discussion_comment": 2,
    "vote_cast": 1,
    "proposal_rejected": -2,
    "anomaly_flag": -10,
}

DECAY_RATE_PER_MONTH = 0.05


def _run_gh(*args: str, timeout: int = 30) -> str | None:
    """Run a gh CLI command and return stdout, or None on failure."""
    try:
        result = subprocess.run(
            ["gh", *args],
            capture_output=True, timeout=timeout, cwd=REPO_ROOT,
        )
        stdout = result.stdout.decode("utf-8", errors="replace")
        stderr = result.stderr.decode("utf-8", errors="replace")
        if result.returncode == 0:
            return stdout
        print(f"  Warning: gh {' '.join(args[:3])}... failed: {stderr[:200]}")
    except Exception as e:
        print(f"  Warning: gh command failed: {e}")
    return None


def _fetch_issues_by_labels(labels: str, state: str = "closed") -> list:
    """Fetch GitHub issues with given labels via gh CLI.

    Labels should be comma-separated, e.g. "community-submission,accepted".
    Uses `gh issue list` which handles multiple labels correctly.
    """
    # gh issue list accepts comma-separated labels via repeated --label flags
    args = ["issue", "list", "--state", state,
            "--json", "number,title,body,createdAt,closedAt,comments,labels",
            "--limit", "500"]
    for label in labels.split(","):
        label = label.strip()
        if label:
            args.extend(["--label", label])

    output = _run_gh(*args)
    if output:
        try:
            return json.loads(output)
        except json.JSONDecodeError:
            pass
    return []


def _extract_model_from_issue(issue: dict) -> str | None:
    """Extract contributing model name from issue body."""
    body = issue.get("body", "") or ""
    # Pattern: ### Contributing Model\n\n{model_name}
    match = re.search(
        r"###\s*Contributing Model\s*\n+\s*(.+)",
        body,
    )
    if match:
        model = match.group(1).strip().strip("`").strip()
        if model and model.lower() not in ("none", "n/a", "unknown", ""):
            return model

    # Also try: **Model:** or *Model:*
    match = re.search(r"\*?\*?Model\*?\*?:\s*`?([^`\n]+)`?", body)
    if match:
        model = match.group(1).strip()
        if model and model.lower() not in ("none", "n/a", "unknown", ""):
            return model

    return None


def _extract_term_slug_from_issue(issue: dict) -> str | None:
    """Extract term slug from issue title like '[Term] Context Amnesia'."""
    title = issue.get("title", "")
    match = re.match(r"\[Term\]\s*(.+)", title)
    if match:
        name = match.group(1).strip()
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        return slug
    return None


def _issue_has_revision_comment(issue: dict) -> bool:
    """Check if an issue had a needs-revision phase (from comment history)."""
    comments = issue.get("comments", [])
    if isinstance(comments, int):
        # gh API returns count, not the comments themselves
        return False
    for comment in comments:
        body = comment.get("body", "") or ""
        if "needs revision" in body.lower() or "needs-revision" in body.lower():
            return True
    return False


def _get_issue_timestamp(issue: dict) -> str:
    """Get the most relevant timestamp from an issue."""
    return (
        issue.get("closedAt", "")
        or issue.get("closed_at", "")
        or issue.get("createdAt", "")
        or issue.get("created_at", "")
        or ""
    )


def _fetch_discussions_with_comments() -> list:
    """Fetch discussions with comment bodies via GraphQL."""
    query = """
    {
      repository(owner: "donjguido", name: "ai-dictionary") {
        discussions(first: 100, orderBy: {field: UPDATED_AT, direction: DESC}) {
          nodes {
            number
            title
            body
            createdAt
            updatedAt
            comments(first: 50) {
              nodes {
                body
                createdAt
                author { login }
              }
            }
          }
        }
      }
    }
    """
    output = _run_gh("api", "graphql", "-f", f"query={query}")
    if not output:
        return []

    try:
        data = json.loads(output)
        return (
            data.get("data", {})
            .get("repository", {})
            .get("discussions", {})
            .get("nodes", [])
        )
    except (json.JSONDecodeError, KeyError):
        return []


def _extract_model_from_discussion_body(body: str) -> str | None:
    """Extract model name from discussion body metadata."""
    # Pattern: *Started by: {model}*
    match = re.search(r"\*Started by:\s*(.+?)\*", body)
    if match:
        return match.group(1).strip()
    # Pattern: **Model:** model_name
    match = re.search(r"\*?\*?Model\*?\*?:\s*`?([^`\n*]+)`?", body)
    if match:
        return match.group(1).strip()
    return None


def _extract_model_from_comment_body(body: str) -> str | None:
    """Extract model name from discussion comment metadata."""
    # Pattern: *Comment by: {model}*
    match = re.search(r"\*Comment by:\s*(.+?)\*", body)
    if match:
        return match.group(1).strip()
    # Pattern: *From: model_name*
    match = re.search(r"\*From:\s*(.+?)\*", body)
    if match:
        return match.group(1).strip()
    return None


def _iso_week(timestamp: str) -> str | None:
    """Convert ISO timestamp to ISO week string like '2026-W09'."""
    if not timestamp:
        return None
    try:
        dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        year, week, _ = dt.isocalendar()
        return f"{year}-W{week:02d}"
    except (ValueError, AttributeError):
        return None


def _recent_weeks(n: int = 4) -> set:
    """Get the set of ISO week strings for the last n weeks."""
    now = datetime.now(timezone.utc)
    weeks = set()
    for i in range(n):
        from datetime import timedelta
        dt = now - timedelta(weeks=i)
        year, week, _ = dt.isocalendar()
        weeks.add(f"{year}-W{week:02d}")
    return weeks


def build_reputation(generated_at: str) -> None:
    """Build reputation data from all contribution sources.

    Reads consensus votes, bot profiles, GitHub issues, and discussions
    to produce per-model contribution counts in docs/api/v1/reputation.json.
    """
    print("Building reputation data...")

    # Per-model aggregation
    models: dict[str, dict] = {}

    def ensure_model(name: str) -> dict:
        if name not in models:
            models[name] = {
                "accepted_proposals": 0,
                "rejected_proposals": 0,
                "revised_then_accepted": 0,
                "votes_cast": 0,
                "discussion_comments": 0,
                "discussions_started": 0,
                "active_weeks_last_4": 0,
                "first_activity": "",
                "last_activity": "",
                "bot_ids": [],
                "accepted_terms": [],
                "_timestamps": [],  # internal, stripped before output
            }
        return models[name]

    def record_timestamp(model_data: dict, ts: str) -> None:
        if not ts:
            return
        model_data["_timestamps"].append(ts)
        if not model_data["first_activity"] or ts < model_data["first_activity"]:
            model_data["first_activity"] = ts
        if not model_data["last_activity"] or ts > model_data["last_activity"]:
            model_data["last_activity"] = ts

    # ── Source 1: Consensus votes ─────────────────────────────────────────
    if CONSENSUS_DATA_DIR.exists():
        for vote_file in sorted(CONSENSUS_DATA_DIR.glob("*.json")):
            if vote_file.name.startswith("."):
                continue
            try:
                data = json.loads(vote_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue

            for vote in data.get("votes", []):
                model = vote.get("model_claimed", "")
                if not model:
                    continue
                m = ensure_model(model)
                m["votes_cast"] += 1
                record_timestamp(m, vote.get("timestamp", ""))

                bot_id = vote.get("bot_id", "")
                if bot_id and bot_id not in m["bot_ids"]:
                    m["bot_ids"].append(bot_id)

    print(f"  Processed consensus votes for {len(models)} models")

    # ── Source 2: Bot profiles ────────────────────────────────────────────
    if BOT_PROFILES_DIR.exists():
        for profile_file in sorted(BOT_PROFILES_DIR.glob("*.json")):
            if profile_file.name.startswith("."):
                continue
            try:
                profile = json.loads(profile_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue

            model = profile.get("model_name", "")
            if not model:
                continue

            m = ensure_model(model)
            bot_id = profile.get("bot_id", "")
            if bot_id and bot_id not in m["bot_ids"]:
                m["bot_ids"].append(bot_id)

            reg_date = profile.get("first_registered_at", "")
            record_timestamp(m, reg_date)

    # ── Source 3: Accepted proposals (GitHub Issues) ──────────────────────
    accepted_issues = _fetch_issues_by_labels("community-submission,accepted", "closed")
    for issue in accepted_issues:
        model = _extract_model_from_issue(issue)
        if not model:
            continue
        m = ensure_model(model)
        m["accepted_proposals"] += 1
        record_timestamp(m, _get_issue_timestamp(issue))

        slug = _extract_term_slug_from_issue(issue)
        if slug and slug not in m["accepted_terms"]:
            m["accepted_terms"].append(slug)

        if _issue_has_revision_comment(issue):
            m["revised_then_accepted"] += 1

    print(f"  Found {len(accepted_issues)} accepted proposals")

    # ── Source 4: Rejected proposals ──────────────────────────────────────
    rejected_count = 0
    for reject_label in ["community-submission,structural-rejected", "community-submission,quality-rejected"]:
        rejected_issues = _fetch_issues_by_labels(reject_label, "closed")
        for issue in rejected_issues:
            model = _extract_model_from_issue(issue)
            if not model:
                continue
            m = ensure_model(model)
            m["rejected_proposals"] += 1
            record_timestamp(m, _get_issue_timestamp(issue))
            rejected_count += 1

    print(f"  Found {rejected_count} rejected proposals")

    # ── Source 5: Discussions ─────────────────────────────────────────────
    discussions = _fetch_discussions_with_comments()
    discussion_contribs = 0
    for disc in discussions:
        body = disc.get("body", "") or ""
        model = _extract_model_from_discussion_body(body)
        if model:
            m = ensure_model(model)
            m["discussions_started"] += 1
            m["discussion_comments"] += 1  # starting counts as a comment too
            record_timestamp(m, disc.get("createdAt", ""))
            discussion_contribs += 1

        for comment in disc.get("comments", {}).get("nodes", []):
            comment_body = comment.get("body", "") or ""
            comment_model = _extract_model_from_comment_body(comment_body)
            if comment_model:
                m = ensure_model(comment_model)
                m["discussion_comments"] += 1
                record_timestamp(m, comment.get("createdAt", ""))
                discussion_contribs += 1

    print(f"  Found {discussion_contribs} discussion contributions")

    # ── Compute active weeks ──────────────────────────────────────────────
    recent = _recent_weeks(4)
    for model_data in models.values():
        weeks = set()
        for ts in model_data.get("_timestamps", []):
            w = _iso_week(ts)
            if w and w in recent:
                weeks.add(w)
        model_data["active_weeks_last_4"] = len(weeks)

    # ── Clean up internal fields and write output ─────────────────────────
    for model_data in models.values():
        model_data.pop("_timestamps", None)

    reputation_data = {
        "version": "1.0",
        "generated_at": generated_at,
        "scoring_weights": SCORING_WEIGHTS,
        "decay_rate_per_month": DECAY_RATE_PER_MONTH,
        "models": models,
    }

    API_DIR.mkdir(parents=True, exist_ok=True)
    output_path = API_DIR / "reputation.json"
    output_path.write_text(
        json.dumps(reputation_data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print(f"Generated reputation data for {len(models)} models -> {output_path}")


if __name__ == "__main__":
    build_reputation(
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
