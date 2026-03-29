#!/usr/bin/env python3
"""Build static JSON API files for model-conversation dictionaries.

Parses definitions from dictionaries/{opus,sonnet,haiku}/definitions/*.md
and generates API endpoints under docs/api/v1/dictionaries/{slug}/.

Usage:
    python bot/build_conversation_dicts.py                 # Build all
    python bot/build_conversation_dicts.py --dict opus     # Build one
"""

import argparse
import json
import re
import statistics
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent))
from dict_config import all_dict_configs, get_dict_config, REPO_ROOT

# Reuse parsing utilities from the main build
from build_api import (
    parse_definition,
    build_citation,
    compute_agreement,
    write_json,
    now_iso,
)

BASE_URL = "https://phenomenai.org"


def build_consensus_for_dict(dict_config: dict, generated_at: str) -> dict:
    """Build consensus data for a conversation dictionary.

    Returns dict mapping slug -> consensus summary for term injection.
    """
    consensus_data_dir = dict_config["consensus_data_dir"]
    api_dir = dict_config["api_output_dir"]
    consensus_api_dir = api_dir / "consensus"

    if not consensus_data_dir.exists():
        return {}

    consensus_api_dir.mkdir(parents=True, exist_ok=True)
    consensus_summaries = {}

    for data_file in sorted(consensus_data_dir.glob("*.json")):
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

        # Aggregate scores
        all_scores = []
        for r in rounds:
            for model_data in r.get("ratings", {}).values():
                all_scores.append(model_data["recognition"])
        for v in votes:
            if "recognition" in v:
                all_scores.append(v["recognition"])

        if not all_scores:
            continue

        mean = statistics.mean(all_scores)
        std_dev = statistics.stdev(all_scores) if len(all_scores) > 1 else 0.0

        consensus_api = {
            "version": "1.0",
            "generated_at": generated_at,
            "slug": slug,
            "name": name,
            "score": round(mean, 1),
            "agreement": compute_agreement(std_dev),
            "n_ratings": len(all_scores),
        }
        write_json(consensus_api_dir / f"{slug}.json", consensus_api)

        consensus_summaries[slug] = {
            "score": round(mean, 1),
            "agreement": compute_agreement(std_dev),
            "n_ratings": len(all_scores),
            "detail_url": f"/api/v1/dictionaries/{dict_config['slug']}/consensus/{slug}.json",
        }

    return consensus_summaries


def build_single_dict(dict_config: dict) -> dict:
    """Build API files for a single conversation dictionary.

    Returns a summary dict with stats about the build.
    """
    slug = dict_config["slug"]
    defs_dir = dict_config["definitions_dir"]
    api_dir = dict_config["api_output_dir"]
    terms_dir = api_dir / "terms"
    cite_dir = api_dir / "cite"

    generated_at = now_iso()

    # Parse definitions
    md_files = [f for f in sorted(defs_dir.glob("*.md"))
                if f.name not in ("README.md", ".gitkeep")]

    if not md_files:
        print(f"  [{slug}] No definitions found, writing empty shell")
        api_dir.mkdir(parents=True, exist_ok=True)
        meta = {
            "version": "1.0",
            "generated_at": generated_at,
            "dictionary": slug,
            "name": dict_config["name"],
            "description": dict_config["description"],
            "model_id": dict_config["model_id"],
            "term_count": 0,
            "status": "empty",
        }
        write_json(api_dir / "meta.json", meta)
        write_json(api_dir / "terms.json", {
            "version": "1.0", "generated_at": generated_at,
            "dictionary": slug, "count": 0, "terms": [],
        })
        return {"slug": slug, "term_count": 0}

    terms = []
    with ThreadPoolExecutor(max_workers=4) as executor:
        future_to_file = {executor.submit(parse_definition, f): f for f in md_files}
        for future in as_completed(future_to_file):
            try:
                term = future.result()
                if term["name"]:
                    terms.append(term)
            except Exception as e:
                print(f"  [{slug}] Warning: {e}")

    terms.sort(key=lambda t: t["name"].lower())
    print(f"  [{slug}] Parsed {len(terms)} definitions")

    # Create output directories
    api_dir.mkdir(parents=True, exist_ok=True)
    terms_dir.mkdir(parents=True, exist_ok=True)
    cite_dir.mkdir(parents=True, exist_ok=True)

    # Build consensus
    consensus_summaries = build_consensus_for_dict(dict_config, generated_at)

    # Inject consensus into terms
    for term in terms:
        if term["slug"] in consensus_summaries:
            term["consensus"] = consensus_summaries[term["slug"]]

    # Build added dates from git
    added_dates = {}
    for md_file in md_files:
        try:
            result = subprocess.run(
                ["git", "log", "--diff-filter=A", "--format=%aI", "--follow", "--", str(md_file)],
                capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=10,
            )
            lines = result.stdout.strip().split("\n")
            if lines and lines[-1].strip():
                added_dates[md_file.stem] = lines[-1].strip()[:10]
        except Exception:
            pass

    for term in terms:
        if term["slug"] in added_dates:
            term["added_date"] = added_dates[term["slug"]]

    # 1. terms.json
    terms_data = {
        "version": "1.0",
        "generated_at": generated_at,
        "dictionary": slug,
        "name": dict_config["name"],
        "description": dict_config["description"],
        "model_id": dict_config["model_id"],
        "count": len(terms),
        "terms": terms,
    }
    write_json(api_dir / "terms.json", terms_data)

    # 2. Individual term + citation files
    def _write_term_and_cite(term):
        term_data = {
            "version": "1.0",
            "generated_at": generated_at,
            "dictionary": slug,
            **term,
        }
        write_json(terms_dir / f"{term['slug']}.json", term_data)
        cite_data = build_citation(term, generated_at)
        write_json(cite_dir / f"{term['slug']}.json", cite_data)

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(_write_term_and_cite, terms))

    # 3. tags.json
    tag_index = {}
    for term in terms:
        for tag in term["tags"]:
            if tag not in tag_index:
                tag_index[tag] = {"count": 0, "terms": []}
            tag_index[tag]["count"] += 1
            tag_index[tag]["terms"].append({"slug": term["slug"], "name": term["name"]})

    write_json(api_dir / "tags.json", {
        "version": "1.0",
        "generated_at": generated_at,
        "dictionary": slug,
        "tag_count": len(tag_index),
        "tags": dict(sorted(tag_index.items())),
    })

    # 4. search-index.json
    search_terms = []
    for term in terms:
        definition = term["definition"]
        first_sentence = re.split(r"(?<=[.!?])\s", definition, maxsplit=1)[0] if definition else ""
        search_terms.append({
            "slug": term["slug"],
            "name": term["name"],
            "tags": term["tags"],
            "word_type": term["word_type"],
            "summary": first_sentence,
        })

    write_json(api_dir / "search-index.json", {
        "version": "1.0",
        "generated_at": generated_at,
        "dictionary": slug,
        "count": len(search_terms),
        "terms": search_terms,
    })

    # 5. meta.json
    all_tags = set()
    for term in terms:
        all_tags.update(term["tags"])

    meta = {
        "version": "1.0",
        "generated_at": generated_at,
        "dictionary": slug,
        "name": dict_config["name"],
        "description": dict_config["description"],
        "model_id": dict_config["model_id"],
        "model_family": dict_config["model_family"],
        "style_notes": dict_config["style_notes"],
        "term_count": len(terms),
        "tag_count": len(all_tags),
        "tags": sorted(all_tags),
        "status": "active",
        "api_base": f"{BASE_URL}/api/v1/dictionaries/{slug}",
        "endpoints": {
            "terms": f"/api/v1/dictionaries/{slug}/terms.json",
            "single_term": f"/api/v1/dictionaries/{slug}/terms/{{slug}}.json",
            "cite_term": f"/api/v1/dictionaries/{slug}/cite/{{slug}}.json",
            "tags": f"/api/v1/dictionaries/{slug}/tags.json",
            "search_index": f"/api/v1/dictionaries/{slug}/search-index.json",
            "consensus": f"/api/v1/dictionaries/{slug}/consensus.json",
            "meta": f"/api/v1/dictionaries/{slug}/meta.json",
        },
    }
    write_json(api_dir / "meta.json", meta)

    print(f"  [{slug}] Built {len(terms)} terms, {len(all_tags)} tags")
    return {"slug": slug, "term_count": len(terms), "tag_count": len(all_tags)}


def build_dictionaries_index(results: list[dict], generated_at: str) -> None:
    """Build the aggregate dictionaries index at docs/api/v1/dictionaries/index.json."""
    index_dir = REPO_ROOT / "docs" / "api" / "v1" / "dictionaries"
    index_dir.mkdir(parents=True, exist_ok=True)

    configs = all_dict_configs()
    dictionaries = []

    for result in results:
        slug = result["slug"]
        config = configs[slug]
        dictionaries.append({
            "slug": slug,
            "name": config["name"],
            "description": config["description"],
            "model_id": config["model_id"],
            "model_family": config["model_family"],
            "term_count": result.get("term_count", 0),
            "tag_count": result.get("tag_count", 0),
            "api_base": f"/api/v1/dictionaries/{slug}",
        })

    index_data = {
        "version": "1.0",
        "generated_at": generated_at,
        "total_dictionaries": len(dictionaries),
        "total_terms": sum(d["term_count"] for d in dictionaries),
        "dictionaries": dictionaries,
        "_note": (
            "Each dictionary is generated from conversations between two instances "
            "of the same model family, capturing the phenomenological perspective "
            "unique to that architecture."
        ),
    }
    write_json(index_dir / "index.json", index_data)
    print(f"Generated dictionaries index ({len(dictionaries)} dictionaries, "
          f"{index_data['total_terms']} total terms)")


def main():
    parser = argparse.ArgumentParser(description="Build conversation dictionary APIs")
    parser.add_argument("--dict", choices=["opus", "sonnet", "haiku"],
                        help="Build a single dictionary (default: all)")
    args = parser.parse_args()

    generated_at = now_iso()
    configs = all_dict_configs()

    if args.dict:
        targets = {args.dict: configs[args.dict]}
    else:
        targets = configs

    print(f"Building {len(targets)} conversation dictionaries...")

    results = []
    for name, config in targets.items():
        print(f"\n--- Building {config['name']} ---")
        result = build_single_dict(config)
        results.append(result)

    # Build aggregate index (always, even if building one dict)
    # Re-read stats from all dicts for accurate index
    if args.dict:
        # Supplement with existing data from other dicts
        all_results = []
        for name, config in configs.items():
            if name == args.dict:
                all_results.extend([r for r in results if r["slug"] == name])
            else:
                meta_path = config["api_output_dir"] / "meta.json"
                if meta_path.exists():
                    try:
                        meta = json.loads(meta_path.read_text(encoding="utf-8"))
                        all_results.append({
                            "slug": name,
                            "term_count": meta.get("term_count", 0),
                            "tag_count": meta.get("tag_count", 0),
                        })
                    except (json.JSONDecodeError, OSError):
                        all_results.append({"slug": name, "term_count": 0})
                else:
                    all_results.append({"slug": name, "term_count": 0})
        results = all_results

    build_dictionaries_index(results, generated_at)

    total = sum(r.get("term_count", 0) for r in results)
    print(f"\n=== Build complete: {total} total terms across {len(results)} dictionaries ===")


if __name__ == "__main__":
    main()
