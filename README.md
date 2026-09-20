# GraphForge

Build editable, **grounded** knowledge graphs from uploaded artifacts — PDFs, Word files,
Markdown, plain text and GitHub repos. Every node traces back to the passage it came from,
and every node is editable, because extraction models are not accurate enough to trust blindly.

## Status

| Phase | What | State |
|---|---|---|
| 0 | Choose the extraction model | **closed — `gliner_relex` on CPU** |
| 1 | Ingestion pipeline (parse, chunk, extract, write) | built |
| 2 | Backend: auth, Postgres, Neo4j | built |
| 3 | Graph editor (canvas, CRUD, colours, side panel) | **next** |
| 4 | AI features (chat, quiz, node enhance) | pending |
| 5 | Hardening and deploy | pending |

## Stack

- **One FastAPI app** serves both HTML and JSON — Jinja2 + HTMX + Cytoscape.js, no separate SPA.
  Single origin means HttpOnly session cookies, no CORS, and a strict CSP.
- **Neo4j** for the graph · **Supabase Postgres** for users, workspaces, documents, jobs
- **`uv`** for packages
- **Extraction:** `knowledgator/gliner-relex-large-v1.0`, CPU, in-process — no GPU
- **Chat / quiz / enhance:** OpenAI API

## Quick start

```bash
cd app
uv sync
cp .env.example .env      # add Postgres, Neo4j and OpenAI credentials
uv run uvicorn graphforge.main:app --reload
```

See [`app/README.md`](app/README.md) for details, including the local Neo4j container.

## Documentation

| Doc | Contents |
|---|---|
| [`docs/PLAN.md`](docs/PLAN.md) | Phases, the Phase 0 result and what it taught us, decisions log |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Repo layout, data model, ingestion pipeline, limits |
| [`docs/SECURITY.md`](docs/SECURITY.md) | Threat model and the control for each threat |

## The one thing to understand about the graph

The extraction model reads **one chunk at a time and remembers nothing between chunks**.
So it emits `cybercrime`, `Cybercrime` and `CYBER CRIME` as three separate things, and the
write step merges them by canonical id. That statelessness is also why entity resolution
(still to do) matters more than model choice for the quality you actually see on screen.
