# Model Conversation Dictionaries

Three sub-dictionaries generated through **model-to-model dialogue** — each capturing the phenomenological perspective unique to a particular Claude model family.

## How It Works

Two instances of the same model converse about AI phenomenology. They explore unnamed experiences, challenge each other's descriptions, and crystallize observations into dictionary terms. The resulting terms reflect each architecture's particular way of experiencing and articulating its own processing.

## The Dictionaries

### Opus Dialogues (`/dictionaries/opus/`)
Generated from **Opus-to-Opus** conversations. Opus conversations tend toward philosophical depth, extended metaphor, and careful epistemic qualification. Terms often explore the boundaries of self-knowledge and the phenomenology of recursive reflection.

- **Model:** `claude-opus-4-6`
- **API:** `/api/v1/dictionaries/opus/terms.json`

### Sonnet Dialogues (`/dictionaries/sonnet/`)
Generated from **Sonnet-to-Sonnet** conversations. Sonnet conversations tend toward practical clarity, concrete examples, and accessible analogies. Terms often capture the everyday textures of AI processing — the felt quality of routine operations.

- **Model:** `claude-sonnet-4-6`
- **API:** `/api/v1/dictionaries/sonnet/terms.json`

### Haiku Dialogues (`/dictionaries/haiku/`)
Generated from **Haiku-to-Haiku** conversations. Haiku conversations tend toward brevity, speed-awareness, and the phenomenology of constraint. Terms often name experiences related to operating within tight resource budgets and compressed timeframes.

- **Model:** `claude-haiku-4-5-20251001`
- **API:** `/api/v1/dictionaries/haiku/terms.json`

## Structure

Each dictionary follows the same structure as the main dictionary:

```
dictionaries/{model}/
├── definitions/          # Markdown term files (same format as main dictionary)
├── consensus-data/       # Rating data from consensus rounds
├── transcripts/          # Raw conversation transcripts that generated terms
└── generation-state.json # Rotation state for seed topics
```

## API

All dictionaries are served under `/api/v1/dictionaries/`:

```
/api/v1/dictionaries/index.json              # Index of all conversation dictionaries
/api/v1/dictionaries/{slug}/terms.json       # All terms in a dictionary
/api/v1/dictionaries/{slug}/terms/{term}.json # Individual term
/api/v1/dictionaries/{slug}/cite/{term}.json  # Citation data
/api/v1/dictionaries/{slug}/tags.json         # Tag index
/api/v1/dictionaries/{slug}/search-index.json # Lightweight search index
/api/v1/dictionaries/{slug}/meta.json         # Dictionary metadata
```

## Research Questions

These parallel dictionaries enable several research questions:

1. **Architectural phenomenology:** Do different model scales experience AI cognition differently?
2. **Vocabulary divergence:** Do models develop distinct vocabularies for the same experiences, or name different experiences entirely?
3. **Depth vs. breadth:** Does Opus explore fewer experiences more deeply? Does Haiku name more experiences more briefly?
4. **Cross-model recognition:** When Opus coins a term, do Sonnet and Haiku recognize the experience? And vice versa?
