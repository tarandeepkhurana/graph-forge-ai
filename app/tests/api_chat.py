"""Chat over one document, through the real streaming API.

    uv run python tests/api_chat.py

Checks the whole path: ingest, embed, ask, stream. The questions are chosen to
exercise different retrieval routes --

  * one that names an entity          -> graph linking + traversal
  * one about a relationship          -> the thing vector RAG cannot do
  * one with no entity in it          -> vector search has to carry it
  * one the document cannot answer    -> must refuse, not invent

That last one matters most. A grounded assistant that confabulates is worse
than no assistant, because the citations make the invention look sourced.
"""

import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
os.environ["DEBUG"] = "false"

import httpx  # noqa: E402

from graphforge.db.database import dispose  # noqa: E402
from graphforge.graph import client as graph_client  # noqa: E402
from graphforge.main import create_app  # noqa: E402

results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, note: str = "") -> bool:
    results.append((name, "PASS" if ok else "FAIL", note))
    return ok


DOC = b"""# Infrastructure Overview

Neo4j is a graph database created by Neo Technology. It uses Cypher as its
query language. GraphForge depends on Neo4j for graph storage.

FastAPI is a Python web framework created by Sebastian Ramirez. It is built on
top of Starlette and Pydantic. GraphForge uses FastAPI to serve its pages.

The deployment runs on a single virtual machine with 8 GB of memory. Response
latency is measured in milliseconds and logged for every request.
"""


async def ask(c, ws, headers, document_id, question):
    """POST the question and collect the SSE stream."""
    events, answer = [], ""
    async with c.stream(
        "POST",
        f"/api/w/{ws}/chat",
        headers=headers,
        json={"document_id": document_id, "question": question},
    ) as response:
        if response.status_code != 200:
            await response.aread()
            return [], "", response.status_code
        async for line in response.aiter_lines():
            if not line.startswith("data: "):
                continue
            event = json.loads(line[6:])
            events.append(event)
            if event["type"] == "token":
                answer += event["text"]
    return events, answer, 200


async def main() -> int:
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=900
    ) as c:
        email = f"chat-{int(time.time())}@example.com"
        pw = "a-really-long-passphrase"
        await c.post("/auth/signup", data={"email": email, "password": pw})
        r = await c.post("/auth/login", data={"email": email, "password": pw})
        headers = {"X-CSRF-Token": r.cookies.get("gf_csrf") or ""}
        ws = re.search(
            r'"workspaceId":\s*"([0-9a-f-]{36})"',
            (await c.get("/", follow_redirects=False)).text,
        ).group(1)

        r = await c.post(
            f"/api/w/{ws}/documents",
            headers=headers,
            files={"file": ("infra.md", DOC, "text/markdown")},
        )
        doc_id = r.json()["id"]
        for _ in range(200):
            await asyncio.sleep(3)
            docs = (await c.get(f"/api/w/{ws}/documents", headers=headers)).json()
            doc = next(d for d in docs if d["id"] == doc_id)
            if doc["status"] in {"ready", "failed"}:
                break
        if not check("document ingested", doc["status"] == "ready", doc.get("error") or ""):
            return 1

        # --- a question naming an entity ---------------------------------
        events, answer, code = await ask(c, ws, headers, doc_id, "What is Neo4j?")
        kinds = [e["type"] for e in events]
        stages = [e.get("stage") for e in events if e["type"] == "status"]

        check("chat responds", code == 200, f"HTTP {code}")
        check("streams status events", "status" in kinds, f"stages: {stages}")
        check("streams answer tokens", kinds.count("token") > 1,
              f"{kinds.count('token')} token events")
        check("sends a done event", "done" in kinds)
        check(
            "answer mentions the subject",
            "graph database" in answer.lower() or "neo4j" in answer.lower(),
            repr(answer[:90]),
        )

        done = next((e for e in events if e["type"] == "done"), {})
        check("returns citable sources", len(done.get("sources", [])) > 0,
              f"{len(done.get('sources', []))} source(s)")
        check(
            "reports retrieval stats",
            bool(done.get("stats", {}).get("chunks")),
            str(done.get("stats")),
        )

        # --- a relationship question (what vector RAG cannot do) ---------
        events, answer, _ = await ask(
            c, ws, headers, doc_id, "What does GraphForge depend on?"
        )
        check(
            "answers a relationship question",
            "neo4j" in answer.lower(),
            repr(answer[:90]),
        )
        linked = next((e for e in events if e.get("stage") == "linked"), {})
        check("linked entities from the question", bool(linked.get("entities")),
              str(linked.get("entities")))

        # --- a question naming no entity (vector search must carry it) ---
        events, answer, _ = await ask(
            c, ws, headers, doc_id, "How much memory does the deployment have?"
        )
        check(
            "answers without a named entity",
            "8" in answer,
            repr(answer[:90]),
        )

        # --- a question the document cannot answer -----------------------
        _, answer, _ = await ask(
            c, ws, headers, doc_id, "What is the capital of Australia?"
        )
        refused = any(
            phrase in answer.lower()
            for phrase in ("not", "does not", "no ", "cannot", "doesn't", "unable")
        )
        check("refuses what the document cannot answer",
              refused and "canberra" not in answer.lower(), repr(answer[:110]))

        # --- cross-document isolation ------------------------------------
        _, _, code = await ask(
            c, ws, headers, "00000000-0000-0000-0000-000000000000", "Anything?"
        )
        events, _, _ = await ask(
            c, ws, headers, "00000000-0000-0000-0000-000000000000", "Anything?"
        )
        check(
            "rejects a document from another workspace",
            any(e["type"] == "error" for e in events),
            "error event returned",
        )

        await c.delete(f"/api/w/{ws}/documents/{doc_id}", headers=headers)

    await graph_client.close()
    await dispose()

    width = max(len(n) for n, _, _ in results) + 2
    print(f"\n{'check':<{width}}{'':<6}note")
    print("-" * (width + 60))
    for name, status, note in results:
        print(f"{name:<{width}}{status:<6}{note}")
    failed = [n for n, s, _ in results if s == "FAIL"]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("FAILED: " + ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
