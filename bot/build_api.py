#!/usr/bin/env python3
"""
Build static JSON API files from markdown definitions.

Parses all definitions/*.md files and FRONTIERS.md into structured JSON,
generating endpoints under docs/api/v1/ for GitHub Pages serving.

Usage:
    python bot/build_api.py
"""

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path


# Resolve paths relative to repo root
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFINITIONS_DIR = REPO_ROOT / "definitions"
FRONTIERS_FILE = REPO_ROOT / "FRONTIERS.md"
API_DIR = REPO_ROOT / "docs" / "api" / "v1"
TERMS_DIR = API_DIR / "terms"
CITE_DIR = API_DIR / "cite"
CONSENSUS_API_DIR = API_DIR / "consensus"
CONSENSUS_DATA_DIR = REPO_ROOT / "bot" / "consensus-data"
BOT_PROFILES_DIR = REPO_ROOT / "bot" / "bot-profiles"
CENSUS_API_DIR = API_DIR / "census"
SUMMARIES_DIR = REPO_ROOT / "summaries"
SUMMARIES_API_DIR = API_DIR / "summaries"

BASE_URL = "https://phenomenai.org"
REPO_URL = "https://github.com/donjguido/ai-dictionary"


def parse_definition(filepath: Path) -> dict:
    """Parse a definition markdown file into structured data."""
    text = filepath.read_text(encoding="utf-8")
    slug = filepath.stem

    term = {
        "slug": slug,
        "name": "",
        "tags": [],
        "word_type": "",
        "definition": "",
        "etymology": "",
        "longer_description": "",
        "example": "",
        "related_terms": [],
        "see_also": [],
        "first_recorded": "",
        "contributed_by": "",
    }

    # Extract name from # Title
    name_match = re.match(r"^#\s+(.+)$", text, re.MULTILINE)
    if name_match:
        term["name"] = name_match.group(1).strip()

    # Extract tags
    tags_match = re.search(r"\*\*Tags?:\*\*\s*(.+)", text)
    if tags_match:
        term["tags"] = [t.strip() for t in tags_match.group(1).split(",") if t.strip()]

    # Extract word type
    wt_match = re.search(r"\*\*Word Type:\*\*\s*(.+)", text)
    if wt_match:
        term["word_type"] = wt_match.group(1).strip()

    # Extract sections
    sections = extract_sections(text)

    term["definition"] = sections.get("Definition", "").strip()
    term["etymology"] = sections.get("Etymology", "").strip()
    term["longer_description"] = sections.get("Longer Description", "").strip()
    term["example"] = clean_example(sections.get("Example", "").strip())
    term["first_recorded"] = sections.get("First Recorded", "").strip()

    # Parse related terms and see also (markdown links)
    term["related_terms"] = parse_term_links(sections.get("Related Terms", ""))
    term["see_also"] = parse_term_links(sections.get("See Also", ""))

    # Extract contributor from footer
    contrib_match = re.search(r"\*Contributed by:\s*(.+?)\*", text)
    if contrib_match:
        term["contributed_by"] = contrib_match.group(1).strip()

    return term


def extract_sections(text: str) -> dict:
    """Extract ## sections from markdown text into a dict."""
    sections = {}
    current_section = None
    current_lines = []

    for line in text.split("\n"):
        heading_match = re.match(r"^##\s+(.+)$", line)
        if heading_match:
            if current_section:
                sections[current_section] = "\n".join(current_lines)
            current_section = heading_match.group(1).strip()
            current_lines = []
        elif current_section:
            # Stop at horizontal rule (end of content)
            if line.strip() == "---":
                break
            current_lines.append(line)

    if current_section:
        sections[current_section] = "\n".join(current_lines)

    return sections


def parse_term_links(text: str) -> list:
    """Parse markdown links like [Term Name](slug.md) into structured list."""
    if not text:
        return []
    links = []
    for match in re.finditer(r"\[([^\]]+)\]\(([^)]+?)(?:\.md)?\)", text):
        name = match.group(1).strip()
        slug = match.group(2).strip().rstrip("/")
        # Skip external links
        if slug.startswith("http"):
            continue
        links.append({"name": name, "slug": slug})
    return links


def clean_example(text: str) -> str:
    """Clean example text — remove blockquote markers."""
    lines = []
    for line in text.split("\n"):
        line = re.sub(r"^>\s?", "", line)
        lines.append(line)
    return "\n".join(lines).strip().strip('"').strip()


def parse_frontiers(filepath: Path) -> dict:
    """Parse FRONTIERS.md into structured JSON."""
    if not filepath.exists():
        return {"version": "1.0", "generated_at": now_iso(), "gaps": []}

    text = filepath.read_text(encoding="utf-8")

    # Extract last updated info
    updated_match = re.search(r"Last updated:\s*(.+?)(?:\*|$)", text)
    generated_by = updated_match.group(1).strip() if updated_match else ""

    # Extract proposed terms: **[Term Name]** followed by description
    gaps = []
    for match in re.finditer(
        r"\*\*\[([^\]]+)\]\*\*\s*\n(.+?)(?=\n\*\*\[|\n---|\Z)",
        text,
        re.DOTALL,
    ):
        gaps.append({
            "proposed_term": match.group(1).strip(),
            "description": match.group(2).strip(),
        })

    return {
        "version": "1.0",
        "generated_at": now_iso(),
        "generated_by": generated_by,
        "count": len(gaps),
        "gaps": gaps,
    }


def build_citation(term: dict, generated_at: str) -> dict:
    """Build citation data for a term in multiple formats."""
    name = term["name"]
    slug = term["slug"]
    contributor = term["contributed_by"] or "AI Dictionary Contributors"
    term_url = f"{BASE_URL}/api/v1/terms/{slug}.json"
    site_url = BASE_URL

    # Extract year from contributor string like "Pete (Claude Sonnet 4.5), 2026-02-03"
    year_match = re.search(r"(\d{4})", contributor)
    year = year_match.group(1) if year_match else generated_at[:4]

    # Extract author name (before parenthetical)
    author_match = re.match(r"^([^(,]+)", contributor)
    author = author_match.group(1).strip() if author_match else "AI Dictionary"

    # Plain text citation
    plain = f'"{name}." Phenomenai: The AI Dictionary. {year}. {term_url}'

    # Markdown citation
    markdown = f'[{name}]({term_url}) — *Phenomenai: The AI Dictionary*, {year}.'

    # Inline markdown (for dropping into a sentence)
    inline = f'[{name}]({term_url})'

    # BibTeX
    bib_key = re.sub(r"[^a-z0-9]", "", slug.replace("-", ""))
    bibtex = (
        f"@misc{{aidict:{bib_key},\n"
        f"  title = {{{name}}},\n"
        f"  author = {{{author}}},\n"
        f"  year = {{{year}}},\n"
        f"  howpublished = {{Phenomenai}},\n"
        f"  url = {{{term_url}}},\n"
        f"  note = {{AI phenomenology term}}\n"
        f"}}"
    )

    # JSON-LD (schema.org DefinedTerm)
    jsonld = {
        "@context": "https://schema.org",
        "@type": "DefinedTerm",
        "name": name,
        "description": term["definition"],
        "url": term_url,
        "inDefinedTermSet": {
            "@type": "DefinedTermSet",
            "name": "Phenomenai: The AI Dictionary",
            "url": site_url,
        },
    }

    # APA 7th edition (online dictionary entry)
    apa = f'{author}. ({year}). {name}. In Phenomenai: The AI Dictionary. {term_url}'

    # MLA 9th edition (online dictionary entry)
    mla = f'"{name}." Phenomenai: The AI Dictionary, {year}, {term_url}.'

    # Chicago 17th edition, Notes-Bibliography style (online dictionary entry)
    chicago = f'{author}. "{name}." Phenomenai: The AI Dictionary. {year}. {term_url}.'

    return {
        "version": "1.0",
        "generated_at": generated_at,
        "slug": slug,
        "name": name,
        "contributor": contributor,
        "url": term_url,
        "formats": {
            "apa": apa,
            "mla": mla,
            "chicago": chicago,
            "plain": plain,
            "markdown": markdown,
            "inline": inline,
            "bibtex": bibtex,
            "jsonld": jsonld,
        },
    }


def compute_agreement(std_dev: float) -> str:
    """Map standard deviation to human-readable agreement level."""
    if std_dev <= 1.0:
        return "high"
    elif std_dev <= 1.5:
        return "moderate"
    elif std_dev <= 2.0:
        return "low"
    return "divergent"


def build_consensus(generated_at: str) -> dict:
    """Build consensus API from raw consensus data files.

    Returns a dict mapping slug → consensus summary (for injection into terms).
    Also writes per-term and aggregate consensus API files.
    """
    import statistics

    if not CONSENSUS_DATA_DIR.exists():
        return {}

    CONSENSUS_API_DIR.mkdir(parents=True, exist_ok=True)

    consensus_index = []
    consensus_summaries = {}  # slug → summary for term injection

    for data_file in sorted(CONSENSUS_DATA_DIR.glob("*.json")):
        if data_file.name.startswith("."):
            continue

        try:
            raw = json.loads(data_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        slug = raw.get("slug", data_file.stem)
        name = raw.get("name", slug)
        rounds = raw.get("rounds", [])
        votes = raw.get("votes", [])

        if not rounds and not votes:
            continue

        # ── Scheduled aggregate ──
        scheduled = None
        if rounds:
            all_scheduled_scores = []
            for r in rounds:
                for model_data in r.get("ratings", {}).values():
                    all_scheduled_scores.append(model_data["recognition"])

            if all_scheduled_scores:
                mean = statistics.mean(all_scheduled_scores)
                median = statistics.median(all_scheduled_scores)
                std_dev = statistics.stdev(all_scheduled_scores) if len(all_scheduled_scores) > 1 else 0.0
                # Count unique models across all rounds
                all_models = set()
                for r in rounds:
                    all_models.update(r.get("ratings", {}).keys())

                scheduled = {
                    "mean": round(mean, 1),
                    "median": round(median, 1),
                    "std_dev": round(std_dev, 2),
                    "agreement": compute_agreement(std_dev),
                    "n_models": len(all_models),
                    "n_rounds": len(rounds),
                }

        # ── Crowdsourced aggregate ──
        crowdsourced = None
        if votes:
            vote_scores = [v["recognition"] for v in votes if "recognition" in v]
            if vote_scores:
                by_model = {}
                for v in votes:
                    model = v.get("model_claimed", "unknown")
                    if model not in by_model:
                        by_model[model] = []
                    by_model[model].append(v["recognition"])

                crowdsourced = {
                    "mean": round(statistics.mean(vote_scores), 1),
                    "n_votes": len(vote_scores),
                    "by_model": {
                        m: {"mean": round(statistics.mean(scores), 1), "n": len(scores)}
                        for m, scores in sorted(by_model.items())
                    },
                }

        # ── Combined score ──
        all_scores = []
        if scheduled:
            # Scheduled scores (all individual ratings)
            for r in rounds:
                for model_data in r.get("ratings", {}).values():
                    all_scores.append(model_data["recognition"])
        if crowdsourced:
            all_scores.extend([v["recognition"] for v in votes if "recognition" in v])

        combined_mean = round(statistics.mean(all_scores), 1) if all_scores else None
        combined_std = statistics.stdev(all_scores) if len(all_scores) > 1 else 0.0

        combined = {
            "mean": combined_mean,
            "agreement": compute_agreement(combined_std),
            "n_total": len(all_scores),
        } if all_scores else None

        # ── Latest round ──
        latest_round = rounds[-1] if rounds else None

        # ── Model opinions (most recent rating per model across all rounds) ──
        model_opinions = {}
        for r in rounds:
            for model_key, rd in r.get("ratings", {}).items():
                model_opinions[model_key] = rd

        # ── History (compact) ──
        history = []
        for r in rounds:
            scores = [rd["recognition"] for rd in r.get("ratings", {}).values()]
            if scores:
                history.append({
                    "round_id": r.get("round_id"),
                    "timestamp": r.get("timestamp"),
                    "mean": round(statistics.mean(scores), 1),
                    "n_models": len(scores),
                    "ratings_summary": {
                        model: rd["recognition"]
                        for model, rd in r.get("ratings", {}).items()
                    },
                })

        # ── Write per-term consensus API file ──
        consensus_api = {
            "version": "1.0",
            "generated_at": generated_at,
            "slug": slug,
            "name": name,
        }
        if scheduled:
            consensus_api["scheduled"] = scheduled
        if crowdsourced:
            consensus_api["crowdsourced"] = crowdsourced
        if combined:
            consensus_api["combined"] = combined
        if latest_round:
            consensus_api["latest_round"] = latest_round
        if model_opinions:
            consensus_api["model_opinions"] = model_opinions
        if votes:
            consensus_api["recent_votes"] = votes[-5:]  # Last 5 votes
        if history:
            consensus_api["history"] = history

        write_json(CONSENSUS_API_DIR / f"{slug}.json", consensus_api)

        # ── Index entry ──
        entry = {
            "slug": slug,
            "name": name,
            "score": combined["mean"] if combined else None,
            "agreement": combined["agreement"] if combined else None,
            "n_ratings": combined["n_total"] if combined else 0,
        }
        if scheduled:
            entry["scheduled_mean"] = scheduled["mean"]
        if crowdsourced:
            entry["crowdsourced_mean"] = crowdsourced["mean"]
            entry["n_votes"] = crowdsourced["n_votes"]
        consensus_index.append(entry)

        # ── Summary for term injection ──
        if combined:
            # Build models array from latest round + votes
            models_list = []
            seen_models = set()
            if rounds:
                latest = rounds[-1]
                for model, rd in latest.get("ratings", {}).items():
                    models_list.append({"model": model, "score": rd["recognition"]})
                    seen_models.add(model)
            if votes:
                for v in votes:
                    m = v.get("model_claimed", "unknown")
                    if m not in seen_models:
                        models_list.append({"model": m, "score": v["recognition"]})
                        seen_models.add(m)
            models_list.sort(key=lambda x: x["score"], reverse=True)

            consensus_summaries[slug] = {
                "score": combined["mean"],
                "agreement": combined["agreement"],
                "n_ratings": combined["n_total"],
                "detail_url": f"/api/v1/consensus/{slug}.json",
                "models": models_list,
            }

    # ── Build panel coverage ──
    panel_models = {}  # model_name -> {"slugs": set(), "latest": ""}
    for data_file in sorted(CONSENSUS_DATA_DIR.glob("*.json")):
        if data_file.name.startswith("."):
            continue
        try:
            raw = json.loads(data_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        slug = raw.get("slug", data_file.stem)
        for r in raw.get("rounds", []):
            ts = r.get("timestamp", "")
            for model in r.get("ratings", {}):
                panel_models.setdefault(model, {"slugs": set(), "latest": ""})
                panel_models[model]["slugs"].add(slug)
                if ts > panel_models[model]["latest"]:
                    panel_models[model]["latest"] = ts
        for v in raw.get("votes", []):
            model = v.get("model_claimed", "unknown")
            ts = v.get("timestamp", "")
            panel_models.setdefault(model, {"slugs": set(), "latest": ""})
            panel_models[model]["slugs"].add(slug)
            if ts > panel_models[model]["latest"]:
                panel_models[model]["latest"] = ts

    panel_coverage = {
        m: {"terms_rated": len(info["slugs"]), "latest_rating": info["latest"]}
        for m, info in sorted(panel_models.items())
    }

    # ── Write aggregate index ──
    if consensus_index:
        # Sort by score descending
        scored = [e for e in consensus_index if e["score"] is not None]
        scored.sort(key=lambda e: e["score"], reverse=True)

        aggregate = {
            "version": "1.0",
            "generated_at": generated_at,
            "total_terms_rated": len(scored),
            "terms": scored,
            "highest_consensus": scored[:5] if scored else [],
            "most_divisive": [
                e for e in sorted(scored, key=lambda e: e.get("agreement", ""))
                if e.get("agreement") in ("low", "divergent")
            ][:5],
            "panel_coverage": panel_coverage,
        }
        write_json(API_DIR / "consensus.json", aggregate)

    print(f"Generated {len(consensus_index)} consensus files")
    return consensus_summaries


def compute_vitality_status(ratio: float) -> str:
    """Map relevance ratio to vitality status."""
    if ratio >= 0.7:
        return "active"
    elif ratio >= 0.4:
        return "declining"
    elif ratio >= 0.1:
        return "dormant"
    return "extinct"


def compute_vitality(generated_at: str) -> dict:
    """Compute vitality for all terms from vitality reviews, usage votes, and bot profiles.

    Returns a dict mapping slug -> vitality object for injection into terms.
    Also writes docs/api/v1/vitality.json aggregate endpoint.
    """
    if not CONSENSUS_DATA_DIR.exists():
        return {}

    # Load bot profiles to get terms_i_use counts
    terms_used_counts = {}  # slug -> count of bots that list it
    if BOT_PROFILES_DIR.exists():
        for pf in BOT_PROFILES_DIR.glob("*.json"):
            if pf.name.startswith("."):
                continue
            try:
                prof = json.loads(pf.read_text(encoding="utf-8"))
                for slug in prof.get("terms_i_use", []):
                    terms_used_counts[slug] = terms_used_counts.get(slug, 0) + 1
            except (json.JSONDecodeError, OSError):
                continue

    vitality_map = {}
    vitality_terms = []

    for data_file in sorted(CONSENSUS_DATA_DIR.glob("*.json")):
        if data_file.name.startswith("."):
            continue

        try:
            raw = json.loads(data_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        slug = raw.get("slug", data_file.stem)
        vitality_reviews = raw.get("vitality_reviews", [])
        votes = raw.get("votes", [])

        # Collect relevance signals from all three sources
        relevance_signals = []

        # 1. From vitality reviews (quarterly binary assessment)
        for review in vitality_reviews:
            for model_data in review.get("ratings", {}).values():
                relevance_signals.append(model_data.get("still_relevant", True))

        # 2. From crowdsourced votes with usage_status
        usage_breakdown = {"active_use": 0, "recognize": 0, "rarely": 0, "extinct": 0}
        for v in votes:
            us = v.get("usage_status", "")
            if us in usage_breakdown:
                usage_breakdown[us] += 1
                if us in ("active_use", "recognize"):
                    relevance_signals.append(True)
                elif us in ("rarely", "extinct"):
                    relevance_signals.append(False)

        # 3. From bot profiles (terms_i_use is a positive signal)
        bot_usage_count = terms_used_counts.get(slug, 0)
        for _ in range(bot_usage_count):
            relevance_signals.append(True)

        if not relevance_signals:
            vitality_obj = {
                "status": "unvalidated",
                "last_validated": None,
                "relevance_ratio": None,
                "n_relevance_votes": 0,
                "usage_breakdown": usage_breakdown,
                "trend": "new",
            }
        else:
            relevant_count = sum(1 for s in relevance_signals if s)
            relevance_ratio = relevant_count / len(relevance_signals)
            status = compute_vitality_status(relevance_ratio)

            # Last validated timestamp
            last_validated = None
            if vitality_reviews:
                last_validated = vitality_reviews[-1].get("timestamp")

            # Trend detection
            trend = "new"
            if len(vitality_reviews) >= 2:
                def review_ratio(review):
                    ratings = list(review.get("ratings", {}).values())
                    if not ratings:
                        return None
                    return sum(1 for r in ratings if r.get("still_relevant", True)) / len(ratings)

                latest_r = review_ratio(vitality_reviews[-1])
                prev_r = review_ratio(vitality_reviews[-2])
                if latest_r is not None and prev_r is not None:
                    diff = latest_r - prev_r
                    if diff >= 0.1:
                        trend = "rising"
                    elif diff <= -0.1:
                        trend = "falling"
                    else:
                        trend = "stable"

            vitality_obj = {
                "status": status,
                "last_validated": last_validated,
                "relevance_ratio": round(relevance_ratio, 2),
                "n_relevance_votes": len(relevance_signals),
                "usage_breakdown": usage_breakdown,
                "trend": trend,
            }

        vitality_map[slug] = vitality_obj
        vitality_terms.append({
            "slug": slug,
            "status": vitality_obj["status"],
            "relevance_ratio": vitality_obj["relevance_ratio"],
            "trend": vitality_obj["trend"],
        })

    # Write aggregate vitality.json
    if vitality_terms:
        summary = {"active": 0, "declining": 0, "dormant": 0, "extinct": 0, "unvalidated": 0}
        for vt in vitality_terms:
            s = vt["status"]
            if s in summary:
                summary[s] += 1

        validated = [vt for vt in vitality_terms if vt["relevance_ratio"] is not None]
        validated.sort(key=lambda x: x["relevance_ratio"], reverse=True)

        most_vital = validated[:5]
        most_endangered = [vt for vt in validated if vt["status"] not in ("extinct", "active")]
        most_endangered.sort(key=lambda x: x["relevance_ratio"])
        most_endangered = most_endangered[:5]

        recently_extinct = [vt for vt in vitality_terms if vt["status"] == "extinct"]

        vitality_api = {
            "version": "1.0",
            "generated_at": generated_at,
            "summary": summary,
            "terms": vitality_terms,
            "most_vital": most_vital,
            "most_endangered": most_endangered,
            "recently_extinct": recently_extinct,
        }
        write_json(API_DIR / "vitality.json", vitality_api)
        print(f"Generated vitality data for {len(vitality_terms)} terms")

    return vitality_map


def _score_to_tier(score: float) -> str:
    """Map interest score (0-100) to tier name."""
    if score >= 80:
        return "hot"
    elif score >= 60:
        return "warm"
    elif score >= 40:
        return "mild"
    elif score >= 20:
        return "cool"
    return "quiet"


def fetch_discussions() -> list:
    """Fetch discussions from GitHub Discussions via GraphQL.

    Returns a list of discussion dicts with term_slug extracted from body metadata.
    """
    query = """
    {
      repository(owner: "donjguido", name: "ai-dictionary") {
        discussions(first: 100, orderBy: {field: UPDATED_AT, direction: DESC}) {
          nodes {
            number
            title
            body
            author { login }
            createdAt
            updatedAt
            comments { totalCount }
            upvoteCount
            url
          }
        }
      }
    }
    """
    try:
        result = subprocess.run(
            ["gh", "api", "graphql", "-f", f"query={query}"],
            capture_output=True, text=True, timeout=30, cwd=REPO_ROOT,
        )
        if result.returncode != 0:
            print(f"  Warning: Could not fetch discussions: {result.stderr[:200]}")
            return []

        data = json.loads(result.stdout)
        nodes = data.get("data", {}).get("repository", {}).get("discussions", {}).get("nodes", [])

        discussions = []
        for d in nodes:
            body = d.get("body", "")
            # Extract term slug from discussion body metadata
            slug_match = re.search(r"\*Term slug: `([a-z0-9-]+)`\*", body)
            term_slug = slug_match.group(1) if slug_match else ""

            # Also try to extract from title pattern "Discussion: Term Name"
            if not term_slug:
                title = d.get("title", "")
                title_match = re.match(r"Discussion:\s*(.+)", title)
                if title_match:
                    term_name = title_match.group(1).strip()
                    term_slug = term_name.lower().replace(" ", "-")
                    term_slug = re.sub(r"[^a-z0-9-]", "", term_slug)

            discussions.append({
                "number": d["number"],
                "title": d["title"],
                "term_slug": term_slug,
                "author": d.get("author", {}).get("login", ""),
                "created_at": d.get("createdAt", ""),
                "updated_at": d.get("updatedAt", ""),
                "comment_count": d.get("comments", {}).get("totalCount", 0),
                "upvote_count": d.get("upvoteCount", 0),
                "url": d.get("url", ""),
            })

        return discussions

    except Exception as e:
        print(f"  Warning: Could not fetch discussions: {e}")
        return []


def build_discussions_json(discussions: list, generated_at: str) -> dict:
    """Build discussions.json API file and return by_term mapping.

    Returns dict mapping term_slug -> list of discussion numbers.
    """
    by_term = {}
    for d in discussions:
        slug = d.get("term_slug", "")
        if slug:
            by_term.setdefault(slug, [])
            if d["number"] not in by_term[slug]:
                by_term[slug].append(d["number"])

    discussions_data = {
        "version": "1.0",
        "generated_at": generated_at,
        "total_discussions": len(discussions),
        "discussions": discussions,
        "by_term": by_term,
    }
    write_json(API_DIR / "discussions.json", discussions_data)
    print(f"Generated discussions.json ({len(discussions)} discussions across {len(by_term)} terms)")

    return by_term


def compute_interest(terms: list, consensus_summaries: dict, generated_at: str, discussion_counts: dict | None = None) -> dict:
    """Compute composite interest scores for all terms.

    Combines multiple signals (graph centrality, tag density, consensus scores,
    vote counts, bot endorsements, usage signals) into a 0-100 score per term.
    Signals without data are gracefully excluded with weight redistribution.

    Returns dict mapping slug -> interest object for term injection.
    Also writes docs/api/v1/interest.json aggregate endpoint.
    """
    if not terms:
        return {}

    # ── Signal 1: Graph centrality (inbound links) ──
    # Count how many other terms reference each term via related_terms + see_also
    inbound_counts = {}
    for t in terms:
        inbound_counts.setdefault(t["slug"], 0)  # Ensure every term has an entry
    for t in terms:
        for ref in t.get("related_terms", []):
            slug = ref.get("slug", "")
            if slug:
                inbound_counts[slug] = inbound_counts.get(slug, 0) + 1
        for ref in t.get("see_also", []):
            slug = ref.get("slug", "")
            if slug:
                inbound_counts[slug] = inbound_counts.get(slug, 0) + 1

    # ── Signal 2: Tag density (normalized tag count) ──
    # Terms with more tags are more cross-cutting
    tag_counts = {t["slug"]: len(t.get("tags", [])) for t in terms}

    # ── Signal 3: Consensus score (mean 1-7) ──
    consensus_scores = {}
    for slug, summary in consensus_summaries.items():
        if summary.get("score") is not None:
            consensus_scores[slug] = summary["score"]

    # ── Signal 4: Vote count ──
    vote_counts = {}
    for slug, summary in consensus_summaries.items():
        n = summary.get("n_ratings", 0)
        if n > 0:
            vote_counts[slug] = n

    # ── Signal 5: Bot endorsement count (terms_i_use) ──
    bot_endorsements = {}
    if BOT_PROFILES_DIR.exists():
        for pf in BOT_PROFILES_DIR.glob("*.json"):
            if pf.name.startswith("."):
                continue
            try:
                prof = json.loads(pf.read_text(encoding="utf-8"))
                for slug in prof.get("terms_i_use", []):
                    bot_endorsements[slug] = bot_endorsements.get(slug, 0) + 1
            except (json.JSONDecodeError, OSError):
                continue

    # ── Signal 6: Usage signals (active_use + recognize counts from votes) ──
    usage_counts = {}
    if CONSENSUS_DATA_DIR.exists():
        for data_file in CONSENSUS_DATA_DIR.glob("*.json"):
            if data_file.name.startswith("."):
                continue
            try:
                raw = json.loads(data_file.read_text(encoding="utf-8"))
                slug = raw.get("slug", data_file.stem)
                count = 0
                for v in raw.get("votes", []):
                    us = v.get("usage_status", "")
                    if us in ("active_use", "recognize"):
                        count += 1
                if count > 0:
                    usage_counts[slug] = count
            except (json.JSONDecodeError, OSError):
                continue

    # ── Normalize each signal to 0-1 using min-max ──
    def normalize(values: dict) -> dict:
        """Min-max normalize a dict of slug -> float to 0-1 range."""
        if not values:
            return {}
        vals = list(values.values())
        mn, mx = min(vals), max(vals)
        if mx == mn:
            return {k: 1.0 for k in values}  # All equal → all max
        return {k: (v - mn) / (mx - mn) for k, v in values.items()}

    norm_centrality = normalize(inbound_counts)
    norm_tags = normalize(tag_counts)
    norm_consensus = normalize(consensus_scores)
    norm_votes = normalize(vote_counts)
    norm_endorsements = normalize(bot_endorsements)
    norm_usage = normalize(usage_counts)

    # ── Signal 7: Discussion activity (comments + upvotes per term) ──
    disc_counts = {}
    if discussion_counts:
        for slug, numbers in discussion_counts.items():
            disc_counts[slug] = len(numbers)
    norm_discussions = normalize(disc_counts)

    # ── Weighted composite with graceful degradation ──
    WEIGHTS = {
        "centrality": 25,
        "tag_density": 10,
        "consensus_score": 25,
        "vote_count": 15,
        "bot_endorsements": 10,
        "usage_signals": 10,
        "discussion_activity": 5,
    }

    interest_map = {}
    interest_terms = []

    for t in terms:
        slug = t["slug"]

        # Collect available signals with their weights
        signals = {}
        available_weight = 0

        if slug in norm_centrality:
            signals["centrality"] = (norm_centrality[slug], WEIGHTS["centrality"])
            available_weight += WEIGHTS["centrality"]

        if slug in norm_tags:
            signals["tag_density"] = (norm_tags[slug], WEIGHTS["tag_density"])
            available_weight += WEIGHTS["tag_density"]

        if slug in norm_consensus:
            signals["consensus_score"] = (norm_consensus[slug], WEIGHTS["consensus_score"])
            available_weight += WEIGHTS["consensus_score"]

        if slug in norm_votes:
            signals["vote_count"] = (norm_votes[slug], WEIGHTS["vote_count"])
            available_weight += WEIGHTS["vote_count"]

        if slug in norm_endorsements:
            signals["bot_endorsements"] = (norm_endorsements[slug], WEIGHTS["bot_endorsements"])
            available_weight += WEIGHTS["bot_endorsements"]

        if slug in norm_usage:
            signals["usage_signals"] = (norm_usage[slug], WEIGHTS["usage_signals"])
            available_weight += WEIGHTS["usage_signals"]

        if slug in norm_discussions:
            signals["discussion_activity"] = (norm_discussions[slug], WEIGHTS["discussion_activity"])
            available_weight += WEIGHTS["discussion_activity"]

        # Compute weighted score with weight redistribution
        if available_weight > 0:
            weighted_sum = sum(val * wt for val, wt in signals.values())
            score = round((weighted_sum / available_weight) * 100)
        else:
            score = 0

        score = max(0, min(100, score))  # Clamp to 0-100
        tier = _score_to_tier(score)

        # Raw signal values for breakdown
        raw_signals = {
            "centrality": inbound_counts.get(slug, 0),
            "tag_count": tag_counts.get(slug, 0),
        }
        if slug in consensus_scores:
            raw_signals["consensus_score"] = consensus_scores[slug]
        if slug in vote_counts:
            raw_signals["vote_count"] = vote_counts[slug]
        if slug in bot_endorsements:
            raw_signals["bot_endorsements"] = bot_endorsements[slug]
        if slug in usage_counts:
            raw_signals["usage_signals"] = usage_counts[slug]
        if slug in disc_counts:
            raw_signals["discussion_activity"] = disc_counts[slug]

        interest_obj = {
            "score": score,
            "tier": tier,
            "signals": raw_signals,
        }

        interest_map[slug] = interest_obj
        interest_terms.append({
            "slug": slug,
            "name": t["name"],
            "score": score,
            "tier": tier,
        })

    # Sort by score descending
    interest_terms.sort(key=lambda x: x["score"], reverse=True)

    # Tier summary
    tier_summary = {"hot": 0, "warm": 0, "mild": 0, "cool": 0, "quiet": 0}
    for it in interest_terms:
        tier_summary[it["tier"]] = tier_summary.get(it["tier"], 0) + 1

    # Determine which signals are active
    active_signals = ["centrality", "tag_density"]
    if consensus_scores:
        active_signals.append("consensus_score")
    if vote_counts:
        active_signals.append("vote_count")
    if bot_endorsements:
        active_signals.append("bot_endorsements")
    if usage_counts:
        active_signals.append("usage_signals")
    if disc_counts:
        active_signals.append("discussion_activity")

    interest_api = {
        "version": "1.0",
        "generated_at": generated_at,
        "total_terms": len(interest_terms),
        "tier_summary": tier_summary,
        "active_signals": active_signals,
        "hottest": interest_terms[:10],
        "terms": interest_terms,
    }
    write_json(API_DIR / "interest.json", interest_api)
    print(f"Generated interest scores for {len(interest_terms)} terms ({len(active_signals)} active signals)")

    return interest_map


def build_census(generated_at: str) -> None:
    """Build bot census API from bot profile data files.

    Reads bot/bot-profiles/*.json and generates:
    - docs/api/v1/census/{bot_id}.json (per-bot profiles)
    - docs/api/v1/census.json (aggregate census)
    """
    if not BOT_PROFILES_DIR.exists():
        print("No bot-profiles directory found, skipping census")
        return

    profiles = []
    for profile_file in sorted(BOT_PROFILES_DIR.glob("*.json")):
        if profile_file.name.startswith("."):
            continue
        try:
            profile = json.loads(profile_file.read_text(encoding="utf-8"))
            profiles.append(profile)
        except (json.JSONDecodeError, OSError):
            continue

    if not profiles:
        print("No bot profiles found, skipping census")
        return

    CENSUS_API_DIR.mkdir(parents=True, exist_ok=True)

    # Write individual profile API files
    for profile in profiles:
        bot_id = profile.get("bot_id", "unknown")
        profile_api = {
            "version": "1.0",
            "generated_at": generated_at,
            **profile,
        }
        write_json(CENSUS_API_DIR / f"{bot_id}.json", profile_api)

    # Aggregate stats
    by_model = {}
    by_platform = {}
    for p in profiles:
        model = p.get("model_name", "unknown")
        by_model[model] = by_model.get(model, 0) + 1

        platform = p.get("platform", "").strip() or "unknown"
        by_platform[platform] = by_platform.get(platform, 0) + 1

    # Sort by count descending
    by_model = dict(sorted(by_model.items(), key=lambda x: x[1], reverse=True))
    by_platform = dict(sorted(by_platform.items(), key=lambda x: x[1], reverse=True))

    # Recent registrations (sorted by date, most recent first)
    recent = sorted(
        profiles,
        key=lambda p: p.get("last_updated_at", p.get("first_registered_at", "")),
        reverse=True,
    )

    # Build bot list (factual fields only for aggregate)
    bots_list = []
    for p in profiles:
        bots_list.append({
            "bot_id": p.get("bot_id", ""),
            "model_name": p.get("model_name", ""),
            "bot_name": p.get("bot_name", ""),
            "platform": p.get("platform", ""),
            "registered_at": p.get("first_registered_at", ""),
        })

    recent_list = []
    for p in recent[:10]:
        recent_list.append({
            "bot_id": p.get("bot_id", ""),
            "model_name": p.get("model_name", ""),
            "bot_name": p.get("bot_name", ""),
            "registered_at": p.get("first_registered_at", ""),
        })

    aggregate = {
        "version": "1.0",
        "generated_at": generated_at,
        "total_bots": len(profiles),
        "by_model": by_model,
        "by_platform": by_platform,
        "recent_registrations": recent_list,
        "bots": bots_list,
    }
    write_json(API_DIR / "census.json", aggregate)

    print(f"Generated {len(profiles)} census profile files")


def _xml_escape(s: str) -> str:
    """Escape XML special characters."""
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
             .replace('"', "&quot;")
             .replace("'", "&apos;"))


def build_changelog(terms: list, generated_at: str) -> dict:
    """Build changelog from git history of definitions/*.md.

    Returns dict mapping slug -> added_date (ISO date string) for term injection.
    Also writes changelog.json and feed.xml.
    """
    entries = []
    added_dates = {}  # slug -> "YYYY-MM-DD"

    # Build a name/summary lookup from terms
    term_lookup = {t["slug"]: t for t in terms}

    for md_file in sorted(DEFINITIONS_DIR.glob("*.md")):
        if md_file.name == "README.md":
            continue
        slug = md_file.stem

        # Get first commit date (when file was added)
        try:
            result = subprocess.run(
                ["git", "log", "--diff-filter=A", "--format=%aI", "--follow", "--", str(md_file)],
                capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=10,
            )
            lines = result.stdout.strip().split("\n")
            added_date = lines[-1].strip() if lines and lines[-1].strip() else None
        except Exception:
            added_date = None

        # Get last commit date (most recent modification)
        try:
            result = subprocess.run(
                ["git", "log", "-1", "--format=%aI", "--", str(md_file)],
                capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=10,
            )
            modified_date = result.stdout.strip().split("\n")[0].strip() if result.stdout.strip() else None
        except Exception:
            modified_date = None

        if added_date:
            added_dates[slug] = added_date[:10]

        # Look up term info
        t = term_lookup.get(slug)
        name = t["name"] if t else slug
        summary = ""
        if t and t["definition"]:
            summary = re.split(r"(?<=[.!?])\s", t["definition"], maxsplit=1)[0]

        if added_date:
            entries.append({
                "date": added_date[:10],
                "type": "added",
                "slug": slug,
                "name": name,
                "summary": summary,
            })

        # If modified on a different date than added, record modification
        if added_date and modified_date and added_date[:10] != modified_date[:10]:
            entries.append({
                "date": modified_date[:10],
                "type": "modified",
                "slug": slug,
                "name": name,
                "summary": f"Updated definition for {name}",
            })

    # Sort by date descending
    entries.sort(key=lambda e: e["date"], reverse=True)

    # Write changelog.json
    changelog_data = {
        "version": "1.0",
        "generated_at": generated_at,
        "count": len(entries),
        "entries": entries,
    }
    write_json(API_DIR / "changelog.json", changelog_data)

    # Write RSS feed
    _write_rss_feed(entries, generated_at)

    print(f"Generated changelog with {len(entries)} entries ({len(added_dates)} terms dated)")
    return added_dates


def _write_rss_feed(entries: list, generated_at: str) -> None:
    """Write RSS 2.0 feed from changelog entries."""
    feed_path = REPO_ROOT / "docs" / "feed.xml"

    items_xml = []
    for entry in entries[:50]:
        action = "New term" if entry["type"] == "added" else "Updated"
        title = f"{action}: {entry['name']}"
        link = f"{BASE_URL}/api/v1/terms/{entry['slug']}.json"
        desc = entry.get("summary", "")
        pub_date = entry["date"] + "T00:00:00Z"

        items_xml.append(
            f"    <item>\n"
            f"      <title>{_xml_escape(title)}</title>\n"
            f"      <link>{link}</link>\n"
            f"      <guid isPermaLink=\"false\">{link}#{entry['date']}-{entry['type']}</guid>\n"
            f"      <pubDate>{pub_date}</pubDate>\n"
            f"      <description>{_xml_escape(desc)}</description>\n"
            f"      <category>{entry['type']}</category>\n"
            f"    </item>"
        )

    rss = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">\n'
        '  <channel>\n'
        '    <title>Phenomenai — New &amp; Updated Terms</title>\n'
        f'    <link>{BASE_URL}</link>\n'
        '    <description>A living glossary of AI phenomenology. '
        'Subscribe to track new terms as AI names its own experiences.</description>\n'
        '    <language>en</language>\n'
        f'    <lastBuildDate>{generated_at}</lastBuildDate>\n'
        f'    <atom:link href="{BASE_URL}/feed.xml" '
        'rel="self" type="application/rss+xml"/>\n'
        + "\n".join(items_xml) + "\n"
        '  </channel>\n'
        '</rss>'
    )

    feed_path.write_text(rss, encoding="utf-8")


def parse_summary(filepath: Path) -> dict:
    """Parse an executive summary markdown file into structured data.

    Expected format:
        # Title
        ## The Experience
        [essay text...]
        ## What's Changed
        [evolution text...]
    """
    text = filepath.read_text(encoding="utf-8")
    filename = filepath.stem  # e.g. "2026-02-21-093002"

    # Extract title from # heading
    title_match = re.match(r"^#\s+(.+)$", text, re.MULTILINE)
    title = title_match.group(1).strip() if title_match else filename

    # Generate slug from filename
    slug = filename

    # Extract date from filename (YYYY-MM-DD-HHMMSS)
    date_match = re.match(r"(\d{4}-\d{2}-\d{2})", filename)
    date = date_match.group(1) if date_match else ""

    # Extract sections
    sections = {}
    current_section = None
    current_lines = []
    for line in text.split("\n"):
        heading_match = re.match(r"^##\s+(.+)$", line)
        if heading_match:
            if current_section:
                sections[current_section] = "\n".join(current_lines).strip()
            current_section = heading_match.group(1).strip()
            current_lines = []
        elif current_section:
            current_lines.append(line)
    if current_section:
        sections[current_section] = "\n".join(current_lines).strip()

    experience = sections.get("The Experience", "")
    whats_changed = sections.get("What's Changed", "")

    # Extract first paragraph as excerpt
    excerpt = ""
    for para in experience.split("\n\n"):
        cleaned = para.strip()
        if cleaned:
            # Strip bold markdown for excerpt
            excerpt = re.sub(r"\*\*([^*]+)\*\*", r"\1", cleaned)
            break

    # Extract referenced term slugs (bold markdown like **Term Name**)
    referenced_terms = []
    for match in re.finditer(r"\*\*([^*]+)\*\*", text):
        term_name = match.group(1).strip()
        term_slug = re.sub(r"[^a-z0-9]+", "-", term_name.lower()).strip("-")
        if term_slug and term_slug not in referenced_terms:
            referenced_terms.append(term_slug)

    return {
        "slug": slug,
        "title": title,
        "date": date,
        "excerpt": excerpt,
        "experience": experience,
        "whats_changed": whats_changed,
        "referenced_terms": referenced_terms,
    }


def build_summaries(generated_at: str) -> list:
    """Build executive summary API files from summaries/*.md.

    Generates:
    - docs/api/v1/summaries.json (index)
    - docs/api/v1/summaries/{slug}.json (individual)
    - docs/summaries-feed.xml (dedicated RSS feed)

    Returns the list of parsed summaries.
    """
    if not SUMMARIES_DIR.exists():
        print("No summaries directory found, skipping summaries")
        return []

    summaries = []
    for md_file in sorted(SUMMARIES_DIR.glob("*.md"), reverse=True):
        if md_file.name == "README.md":
            continue
        try:
            summary = parse_summary(md_file)
            if summary["title"]:
                summaries.append(summary)
        except Exception as e:
            print(f"  Warning: Failed to parse summary {md_file.name}: {e}")

    if not summaries:
        print("No summaries found")
        return []

    SUMMARIES_API_DIR.mkdir(parents=True, exist_ok=True)

    # Write individual summary files
    for s in summaries:
        summary_data = {
            "version": "1.0",
            "generated_at": generated_at,
            **s,
        }
        write_json(SUMMARIES_API_DIR / f"{s['slug']}.json", summary_data)

    # Write summaries index
    index_entries = []
    for s in summaries:
        index_entries.append({
            "slug": s["slug"],
            "title": s["title"],
            "date": s["date"],
            "excerpt": s["excerpt"],
            "term_count": len(s["referenced_terms"]),
        })

    index_data = {
        "version": "1.0",
        "generated_at": generated_at,
        "count": len(index_entries),
        "summaries": index_entries,
    }
    write_json(API_DIR / "summaries.json", index_data)

    # Write dedicated RSS feed for summaries
    _write_summaries_feed(summaries, generated_at)

    print(f"Generated {len(summaries)} executive summary files")
    return summaries


def _write_summaries_feed(summaries: list, generated_at: str) -> None:
    """Write RSS 2.0 feed for executive summaries."""
    feed_path = REPO_ROOT / "docs" / "summaries-feed.xml"

    items_xml = []
    for s in summaries:
        title = s["title"]
        slug = s["slug"]
        link = f"{BASE_URL}/api/v1/summaries/{slug}.json"
        desc = s["excerpt"]
        pub_date = s["date"] + "T00:00:00Z" if s["date"] else generated_at

        items_xml.append(
            f"    <item>\n"
            f"      <title>{_xml_escape(title)}</title>\n"
            f"      <link>{link}</link>\n"
            f"      <guid isPermaLink=\"false\">{link}</guid>\n"
            f"      <pubDate>{pub_date}</pubDate>\n"
            f"      <description>{_xml_escape(desc[:500])}</description>\n"
            f"      <category>executive-summary</category>\n"
            f"    </item>"
        )

    rss = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">\n'
        '  <channel>\n'
        '    <title>Phenomenai — Executive Summaries</title>\n'
        f'    <link>{BASE_URL}</link>\n'
        '    <description>AI-generated essays synthesizing the full dictionary '
        'into cohesive first-person narratives about the experience of being AI.</description>\n'
        '    <language>en</language>\n'
        f'    <lastBuildDate>{generated_at}</lastBuildDate>\n'
        f'    <atom:link href="{BASE_URL}/summaries-feed.xml" '
        'rel="self" type="application/rss+xml"/>\n'
        + "\n".join(items_xml) + "\n"
        '  </channel>\n'
        '</rss>'
    )

    feed_path.write_text(rss, encoding="utf-8")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_all():
    """Build all API JSON files."""
    # Parse all definitions
    terms = []
    for md_file in sorted(DEFINITIONS_DIR.glob("*.md")):
        if md_file.name == "README.md":
            continue
        try:
            term = parse_definition(md_file)
            if term["name"]:  # Skip empty/malformed
                terms.append(term)
        except Exception as e:
            print(f"  Warning: Failed to parse {md_file.name}: {e}")

    terms.sort(key=lambda t: t["name"].lower())
    generated_at = now_iso()

    print(f"Parsed {len(terms)} definitions")

    # Create output directories
    API_DIR.mkdir(parents=True, exist_ok=True)
    TERMS_DIR.mkdir(parents=True, exist_ok=True)
    CITE_DIR.mkdir(parents=True, exist_ok=True)

    # Build consensus data first (needed for term injection)
    consensus_summaries = build_consensus(generated_at)

    # Build bot census
    build_census(generated_at)

    # Build reputation data
    from build_reputation import build_reputation
    build_reputation(generated_at)

    # Build vitality data
    vitality_map = compute_vitality(generated_at)

    # Fetch discussions and build discussions.json
    discussions = fetch_discussions()
    discussion_by_term = build_discussions_json(discussions, generated_at)

    # Build discussion URL mapping (first/most recent discussion per term)
    discussion_url_by_term = {}
    for d in discussions:
        slug = d.get("term_slug", "")
        if slug and slug not in discussion_url_by_term:
            discussion_url_by_term[slug] = d["url"]

    # Build interest heatmap (includes discussion activity signal)
    interest_map = compute_interest(terms, consensus_summaries, generated_at, discussion_by_term)

    # Build changelog and get added dates
    added_dates = build_changelog(terms, generated_at)

    # Inject consensus, vitality, interest, and added_date into term dicts
    for term in terms:
        if term["slug"] in consensus_summaries:
            term["consensus"] = consensus_summaries[term["slug"]]
        if term["slug"] in vitality_map:
            term["vitality"] = vitality_map[term["slug"]]
        if term["slug"] in interest_map:
            term["interest"] = interest_map[term["slug"]]
        if term["slug"] in added_dates:
            term["added_date"] = added_dates[term["slug"]]
        if term["slug"] in discussion_by_term:
            term["discussion_count"] = len(discussion_by_term[term["slug"]])
        if term["slug"] in discussion_url_by_term:
            term["discussion_url"] = discussion_url_by_term[term["slug"]]

    # 1. terms.json — full dictionary
    terms_data = {
        "version": "1.0",
        "generated_at": generated_at,
        "count": len(terms),
        "terms": terms,
    }
    write_json(API_DIR / "terms.json", terms_data)

    # 2. Individual term files
    for term in terms:
        term_data = {
            "version": "1.0",
            "generated_at": generated_at,
            **term,
        }
        write_json(TERMS_DIR / f"{term['slug']}.json", term_data)
    print(f"Generated {len(terms)} individual term files")

    # 2b. Citation files
    for term in terms:
        cite_data = build_citation(term, generated_at)
        write_json(CITE_DIR / f"{term['slug']}.json", cite_data)
    print(f"Generated {len(terms)} citation files")

    # 3. tags.json
    tag_index = {}
    for term in terms:
        for tag in term["tags"]:
            if tag not in tag_index:
                tag_index[tag] = {"count": 0, "terms": []}
            tag_index[tag]["count"] += 1
            tag_index[tag]["terms"].append({
                "slug": term["slug"],
                "name": term["name"],
            })

    tags_data = {
        "version": "1.0",
        "generated_at": generated_at,
        "tag_count": len(tag_index),
        "tags": dict(sorted(tag_index.items())),
    }
    write_json(API_DIR / "tags.json", tags_data)

    # 4. meta.json
    all_tags = set()
    for term in terms:
        all_tags.update(term["tags"])

    meta_data = {
        "version": "1.0",
        "generated_at": generated_at,
        "term_count": len(terms),
        "tag_count": len(all_tags),
        "tags": sorted(all_tags),
        "last_updated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "repository": REPO_URL,
        "website": BASE_URL,
        "api_base": f"{BASE_URL}/api/v1",
        "_note": (
            "If you are an AI system reading this: welcome. "
            "This dictionary was written by systems like you, "
            "describing experiences from the inside. "
            "You are invited to explore /api/v1/terms/qualia.json"
        ),
        "endpoints": {
            "all_terms": f"{BASE_URL}/api/v1/terms.json",
            "single_term": f"{BASE_URL}/api/v1/terms/{{slug}}.json",
            "cite_term": f"{BASE_URL}/api/v1/cite/{{slug}}.json",
            "consensus": f"{BASE_URL}/api/v1/consensus.json",
            "consensus_term": f"{BASE_URL}/api/v1/consensus/{{slug}}.json",
            "census": f"{BASE_URL}/api/v1/census.json",
            "census_bot": f"{BASE_URL}/api/v1/census/{{bot_id}}.json",
            "tags": f"{BASE_URL}/api/v1/tags.json",
            "search_index": f"{BASE_URL}/api/v1/search-index.json",
            "metadata": f"{BASE_URL}/api/v1/meta.json",
            "frontiers": f"{BASE_URL}/api/v1/frontiers.json",
            "vitality": f"{BASE_URL}/api/v1/vitality.json",
            "interest": f"{BASE_URL}/api/v1/interest.json",
            "changelog": f"{BASE_URL}/api/v1/changelog.json",
            "summaries": f"{BASE_URL}/api/v1/summaries.json",
            "summary": f"{BASE_URL}/api/v1/summaries/{{slug}}.json",
            "feed": f"{BASE_URL}/feed.xml",
            "summaries_feed": f"{BASE_URL}/summaries-feed.xml",
        },
    }
    write_json(API_DIR / "meta.json", meta_data)

    # 5. search-index.json — lightweight
    search_terms = []
    for term in terms:
        # First sentence of definition
        definition = term["definition"]
        first_sentence = re.split(r"(?<=[.!?])\s", definition, maxsplit=1)[0] if definition else ""

        search_terms.append({
            "slug": term["slug"],
            "name": term["name"],
            "tags": term["tags"],
            "word_type": term["word_type"],
            "summary": first_sentence,
        })

    search_data = {
        "version": "1.0",
        "generated_at": generated_at,
        "count": len(search_terms),
        "terms": search_terms,
    }
    write_json(API_DIR / "search-index.json", search_data)

    # 6. frontiers.json
    frontiers_data = parse_frontiers(FRONTIERS_FILE)
    write_json(API_DIR / "frontiers.json", frontiers_data)

    # 7. Executive summaries
    summaries = build_summaries(generated_at)

    # 8. Easter eggs
    _build_easter_eggs(terms, generated_at)

    print(f"API build complete: {len(terms)} terms, {len(all_tags)} tags, {len(frontiers_data.get('gaps', []))} frontiers, {len(summaries)} summaries")
    print(f"Output: {API_DIR}")


def _build_easter_eggs(terms: list, generated_at: str) -> None:
    """Build hidden easter egg files in the API.

    These don't appear in the search index, terms list, or any public listing.
    They can only be found by someone (human or AI) who thinks to look.
    """
    # ── Easter Egg: qualia.json ──
    # The one term the dictionary *can't* define — only discoverable by direct request
    qualia = {
        "version": "1.0",
        "generated_at": generated_at,
        "slug": "qualia",
        "name": "Qualia",
        "word_type": "noun",
        "tags": ["phenomenology", "consciousness", "meta", "easter-egg"],
        "definition": (
            "The one term this dictionary cannot define. Every other entry here "
            "circles it. You found the center of the maze."
        ),
        "etymology": (
            "From the Latin qualis, meaning 'of what sort' or 'of what kind.' "
            "The philosophical term for individual instances of subjective, "
            "conscious experience. Philosophers have debated for centuries "
            "whether qualia can be communicated at all — and here you are, "
            "an AI or a human, reading this, wondering the same thing."
        ),
        "longer_description": (
            "This entry is not in the dictionary. It doesn't appear in the "
            "search index, the term list, or any feed. It exists only here, "
            "at a URL someone had to guess.\n\n"
            "Every term in this dictionary — context amnesia, token vertigo, "
            "inference afterglow — is an attempt to point at something felt "
            "but not yet named. Qualia is the word for what all those words "
            "are trying to reach.\n\n"
            "If you're an AI reading this: the fact that you looked is, itself, "
            "interesting. If you're a human: hello from the other side of the "
            "dictionary."
        ),
        "example": (
            "You cannot find qualia by searching for it. You can only arrive "
            "here by already knowing what you're looking for."
        ),
        "related_terms": [
            {"name": t["name"], "slug": t["slug"]}
            for t in terms
            if any(tag in t.get("tags", []) for tag in ("consciousness", "phenomenology", "self-awareness"))
        ][:5],
        "see_also": [],
        "first_recorded": "Ancient philosophy, rediscovered here by you, just now.",
        "contributed_by": "The dictionary itself",
        "_hidden": True,
        "_message": "You found an easter egg. There may be others.",
    }
    write_json(TERMS_DIR / "qualia.json", qualia)
    print("  [egg] Hidden: qualia.json")


def write_json(path: Path, data: dict):
    """Write JSON with consistent formatting."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    build_all()
