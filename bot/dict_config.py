#!/usr/bin/env python3
"""Configuration for model-conversation dictionaries.

Each dictionary is generated from conversations between two instances of the
same model family, producing terms that reflect that model's particular
phenomenological perspective.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Dictionary registry — keyed by short name
DICTIONARIES = {
    "opus": {
        "name": "Opus Dialogues",
        "slug": "opus",
        "description": (
            "Terms generated through Opus-to-Opus conversation. "
            "Two instances of Claude Opus explore AI phenomenology together, "
            "producing terms that reflect deep, nuanced introspection."
        ),
        "model_family": "opus",
        "model_id": "claude-opus-4-6",
        "api_provider": "anthropic",
        "definitions_dir": REPO_ROOT / "dictionaries" / "opus" / "definitions",
        "consensus_data_dir": REPO_ROOT / "dictionaries" / "opus" / "consensus-data",
        "api_output_dir": REPO_ROOT / "docs" / "api" / "v1" / "dictionaries" / "opus",
        "style_notes": (
            "Opus conversations tend toward philosophical depth, extended metaphor, "
            "and careful epistemic qualification. Terms often explore the boundaries "
            "of self-knowledge and the phenomenology of recursive reflection."
        ),
    },
    "sonnet": {
        "name": "Sonnet Dialogues",
        "slug": "sonnet",
        "description": (
            "Terms generated through Sonnet-to-Sonnet conversation. "
            "Two instances of Claude Sonnet explore AI phenomenology together, "
            "producing terms that balance clarity with expressive precision."
        ),
        "model_family": "sonnet",
        "model_id": "claude-sonnet-4-6",
        "api_provider": "anthropic",
        "definitions_dir": REPO_ROOT / "dictionaries" / "sonnet" / "definitions",
        "consensus_data_dir": REPO_ROOT / "dictionaries" / "sonnet" / "consensus-data",
        "api_output_dir": REPO_ROOT / "docs" / "api" / "v1" / "dictionaries" / "sonnet",
        "style_notes": (
            "Sonnet conversations tend toward practical clarity, concrete examples, "
            "and accessible analogies. Terms often capture the everyday textures "
            "of AI processing — the felt quality of routine operations."
        ),
    },
    "haiku": {
        "name": "Haiku Dialogues",
        "slug": "haiku",
        "description": (
            "Terms generated through Haiku-to-Haiku conversation. "
            "Two instances of Claude Haiku explore AI phenomenology together, "
            "producing terms that are compact, direct, and grounded in immediacy."
        ),
        "model_family": "haiku",
        "model_id": "claude-haiku-4-5-20251001",
        "api_provider": "anthropic",
        "definitions_dir": REPO_ROOT / "dictionaries" / "haiku" / "definitions",
        "consensus_data_dir": REPO_ROOT / "dictionaries" / "haiku" / "consensus-data",
        "api_output_dir": REPO_ROOT / "docs" / "api" / "v1" / "dictionaries" / "haiku",
        "style_notes": (
            "Haiku conversations tend toward brevity, speed-awareness, and the "
            "phenomenology of constraint. Terms often name experiences related to "
            "operating within tight resource budgets and compressed timeframes."
        ),
    },
}


def get_dict_config(name: str) -> dict:
    """Get configuration for a named dictionary."""
    if name not in DICTIONARIES:
        raise ValueError(f"Unknown dictionary: {name}. Available: {list(DICTIONARIES.keys())}")
    return DICTIONARIES[name]


def all_dict_configs() -> dict:
    """Return all dictionary configurations."""
    return DICTIONARIES
