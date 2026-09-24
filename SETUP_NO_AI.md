# Running GraphForge Without OpenAI

This document explains how to set up and run GraphForge for development **without** needing OpenAI API credentials.

## What You Can Do Without OpenAI

✅ **Works:**
- Authentication & user sessions
- Frontend UI and graph visualization  
- Database operations (Supabase Postgres, Neo4j)
- File upload mechanics
- Graph operations and queries
- All non-AI features you might build

❌ **Requires OpenAI API key:**
- Document graph extraction (the core AI feature)
- Chat functionality (answering questions about documents)
- Entity resolution (merging duplicate concepts)
- Quiz generation

## Quick Setup (No API Key Needed)

### 1. Clone and Install

```bash
git clone <repo>
cd graph-forge-ai/app
python -m venv .venv
.venv\Scripts\activate          # Windows
source .venv/bin/activate       # macOS/Linux
pip install -e .
```

### 2. Configure `.env`

Copy `.env.example` to `.env`:

```bash
# Session signing (generate with: python -c "import secrets;print(secrets.token_urlsafe(48))")
SECRET_KEY=<your-secret-key>

# Database credentials (from your provider)
DATABASE_URL=postgresql+asyncpg://...
NEO4J_URI=neo4j+s://...
NEO4J_USER=...
NEO4J_PASSWORD=...

# Leave empty to disable AI features
OPENAI_API_KEY=

# Development settings
DEBUG=true
COOKIE_SECURE=false
BASE_URL=http://localhost:8000
```

### 3. Start the Server

```bash
python -m uvicorn graphforge.main:app --reload
```

**The server will start successfully.** You can now:
- Browse the UI at http://localhost:8000
- Build features that don't require AI
- Test database connectivity
- Work on the frontend

### 4. Try Document Upload (What Fails Gracefully)

When you try to upload a document without the OpenAI API key:

```
Error: OPENAI_API_KEY is missing from app/.env — extraction cannot run
```

This is expected. The feature fails with a **clear, friendly message**, not a cryptic error.

---

## If You Need AI Features

Set `OPENAI_API_KEY` in your `.env`:

```bash
OPENAI_API_KEY=sk-proj-your-key-here
OPENAI_MODEL=gpt-5-nano
EXTRACTION_MODEL=openai
EXTRACTION_OPENAI_MODEL=gpt-5-mini
```

Then extraction, chat, and entity resolution will all work.

---

## What Changed

- `OPENAI_API_KEY` is now **optional** in the config (defaults to empty string)
- Server **starts fine** with or without the key
- AI features **fail gracefully** with a clear error message if the key is missing
- No app crashes, no silent failures

## Testing This Setup

Run the included tests:

```bash
# Test 1: Config loads with/without key
python test_config.py

# Test 2: FastAPI app initializes with/without key  
python test_startup.py

# Test 3: Extraction error messages
python test_extraction_error.py
```

All three pass, confirming the setup works.
