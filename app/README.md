# GraphForge — application

One FastAPI process serves the HTML pages and the JSON API. See
[`../docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md) for why there is no separate SPA.

## Setup

```bash
cd app
uv sync                 # includes CPU-only torch + the extraction model deps
cp .env.example .env    # fill in the secrets
```

You also need Postgres (Supabase) and Neo4j reachable. For local Neo4j:

```bash
docker run -d --name gf-neo4j -p 7474:7474 -p 7687:7687 \
  -e NEO4J_AUTH=neo4j/localdevpassword neo4j:5
```

## Run

```bash
uv run uvicorn graphforge.main:app --reload
```

`http://localhost:8000/health` should return `{"status": "ok"}`.

The extraction model (~1.8 GB) downloads on first ingestion and loads once per
process — roughly 20 seconds and 2.7 GB resident, then reused for every job.

## Tests

```bash
uv run python tests/test_ingest.py
```

The offset test is the load-bearing one: the side panel shows source text by
slicing the original document with a chunk's stored offsets, so drift there means
silently showing the wrong passage.

## Layout

```
src/graphforge/
├── core/         config (all limits live here), security, rate limiting
├── db/           Postgres models + session factory
├── graph/        Neo4j driver and Cypher; tenant isolation enforced in client.py
├── ingest/       parsers, chunking, pipeline
├── extraction/   the model behind a swappable Extractor interface
├── ai/           chat, quiz, node enhance
├── api/          JSON + form routes
├── web/          Jinja2 templates
└── static/       css, js, cytoscape
```

## Two things worth knowing before editing

**`graph/client.py` refuses unscoped queries.** Every Cypher statement must
reference `$workspace_id`. That check is blunt on purpose — a blunt check that
always runs beats a careful one that is sometimes forgotten, and forgetting it
once leaks one user's documents to another.

**Relation types are an allowlist.** Cypher cannot parameterise a relationship
type, so relations are stored as `REL` with a `type` property. Building the type
into the query string from model output would be a Cypher injection hole.
