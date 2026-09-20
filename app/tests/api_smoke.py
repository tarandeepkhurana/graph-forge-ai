"""End-to-end API pass against the real Supabase and Neo4j Aura.

Not a unit test -- it talks to live databases and runs the extraction model, so
it is a script you run deliberately:

    uv run python tests/api_smoke.py

It walks the whole product path in order: sign up, sign in, upload, ingest,
read the graph, open a node's source passage, edit, delete. Anything that
breaks in the real world but passes in isolation shows up here.
"""

import asyncio
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
os.environ["DEBUG"] = "false"  # silence SQL echo; this is a report, not a trace

import httpx  # noqa: E402

from graphforge.db.database import dispose  # noqa: E402
from graphforge.graph import client as graph_client  # noqa: E402
from graphforge.main import create_app  # noqa: E402

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, note: str = "") -> bool:
    results.append((name, PASS if ok else FAIL, note))
    return ok


SAMPLE = b"""# Cyber security notes

Neo4j is a graph database that stores data as nodes and relationships. It uses
Cypher as its query language. GraphForge depends on Neo4j for graph storage.

FastAPI is a Python web framework created by Sebastian Ramirez. It is built on
top of Starlette and Pydantic.

Marie Curie was a physicist who worked at the University of Paris. She
discovered polonium and radium. Her research on radioactivity caused a lasting
shift in atomic physics.
"""


async def main() -> int:
    app = create_app()
    # Lifespan is driven manually so schema setup runs exactly once here.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=600
    ) as c:
        email = f"smoke-{int(time.time())}@example.com"
        password = "a-really-long-passphrase"

        # ---------------------------------------------------------- health --
        r = await c.get("/health")
        check("GET  /health", r.status_code == 200, str(r.json()))

        r = await c.get("/", follow_redirects=False)
        check("GET  / (anonymous)", r.status_code == 200 and "GraphForge" in r.text, "home page")

        r = await c.get("/signin")
        check(
            "GET  /signin",
            r.status_code == 200 and "content-security-policy" in r.headers,
            f"{len(r.text)}B, CSP header present",
        )

        # ------------------------------------------------------------ auth --
        r = await c.post("/auth/signup", data={"email": email, "password": password})
        check("POST /auth/signup", r.status_code == 201, str(r.json()))

        r = await c.post("/auth/signup", data={"email": email, "password": password})
        check(
            "POST /auth/signup (duplicate)",
            r.status_code == 201,
            "no account-existence leak",
        )

        r = await c.post("/auth/signup", data={"email": "a@b.com", "password": "short"})
        check("POST /auth/signup (weak password)", r.status_code == 422, "rejected")

        r = await c.post("/auth/login", data={"email": email, "password": "wrong"})
        wrong_pw_body = r.json()
        check("POST /auth/login (wrong password)", r.status_code == 401, str(wrong_pw_body))

        r = await c.post(
            "/auth/login", data={"email": "nobody@nowhere.test", "password": password}
        )
        check(
            "POST /auth/login (unknown email)",
            r.status_code == 401 and r.json() == wrong_pw_body,
            "identical response to wrong password",
        )

        r = await c.post("/auth/login", data={"email": email, "password": password})
        csrf = r.cookies.get("gf_csrf")
        check(
            "POST /auth/login",
            r.status_code == 200 and bool(r.cookies.get("gf_session")) and bool(csrf),
            "session + csrf cookies set",
        )
        headers = {"X-CSRF-Token": csrf or ""}

        r = await c.get("/", follow_redirects=False)
        check("GET  / (signed in)", r.status_code == 200, "workspace page rendered")

        # Recover the workspace id the page was built with.
        import re

        match = re.search(r'"workspaceId":\s*"([0-9a-f-]{36})"', r.text)
        if not check("workspace id in page", bool(match)):
            return 1
        ws = match.group(1)

        # ------------------------------------------------------------ CSRF --
        r = await c.post(
            f"/api/w/{ws}/node", json={"name": "NoCsrf", "type": "concept"}
        )
        check("POST /node without CSRF token", r.status_code == 403, "rejected")

        # -------------------------------------------------- cross-tenant --
        other = "00000000-0000-0000-0000-000000000000"
        r = await c.get(f"/api/w/{other}/graph", headers=headers)
        check(
            "GET  /api/w/<not mine>/graph",
            r.status_code == 404,
            "404, not 403 -- does not confirm existence",
        )

        # -------------------------------------------------------- documents --
        r = await c.get(f"/api/w/{ws}/documents", headers=headers)
        check("GET  /documents (empty)", r.status_code == 200 and r.json() == [], "[]")

        r = await c.post(
            f"/api/w/{ws}/documents",
            headers=headers,
            files={"file": ("notes.md", SAMPLE, "text/markdown")},
        )
        ok = r.status_code == 201
        check("POST /documents (upload)", ok, str(r.json())[:80])
        if not ok:
            return 1
        doc_id = r.json()["id"]

        r = await c.post(
            f"/api/w/{ws}/documents",
            headers=headers,
            files={"file": ("notes.md", SAMPLE, "text/markdown")},
        )
        check("POST /documents (same file twice)", r.status_code == 409, "409 duplicate")

        r = await c.post(
            f"/api/w/{ws}/documents",
            headers=headers,
            files={"file": ("evil.pdf", b"MZ\x90\x00\x03bad", "application/pdf")},
        )
        check(
            "POST /documents (binary as .pdf)",
            r.status_code == 400,
            "magic-byte sniff rejected it",
        )

        # ------------------------------------------------------- ingestion --
        print("\n  waiting for ingestion (first run loads the model, ~20s)...", flush=True)
        status, waited = "", 0.0
        while waited < 420:
            await asyncio.sleep(3)
            waited += 3
            docs = (await c.get(f"/api/w/{ws}/documents", headers=headers)).json()
            doc = next((d for d in docs if d["id"] == doc_id), None)
            if doc is None:
                break
            status = doc["status"]
            print(f"    {waited:5.0f}s  {status:<12} {doc['progress']}%", flush=True)
            if status in {"ready", "failed"}:
                break

        check(
            "ingestion completed",
            status == "ready",
            f"status={status} after {waited:.0f}s"
            + (f" err={doc.get('error')}" if doc and doc.get("error") else ""),
        )

        # ----------------------------------------------------------- graph --
        r = await c.get(f"/api/w/{ws}/graph", headers=headers)
        graph = r.json() if r.status_code == 200 else {"nodes": [], "edges": []}
        check(
            "GET  /graph",
            r.status_code == 200 and len(graph["nodes"]) > 0,
            f"{len(graph['nodes'])} nodes, {len(graph['edges'])} edges",
        )
        if not graph["nodes"]:
            return 1

        node_id = graph["nodes"][0]["id"]

        r = await c.get(f"/api/w/{ws}/node/{node_id}", headers=headers)
        detail = r.json() if r.status_code == 200 else {}
        sources = detail.get("sources", [])
        check(
            "GET  /node/<id> (grounding)",
            r.status_code == 200 and len(sources) > 0,
            f"'{detail.get('name')}' with {len(sources)} source passage(s)",
        )
        if sources:
            snippet = sources[0]["text"][:60].replace("\n", " ")
            check(
                "source text is verbatim",
                sources[0]["text"] in SAMPLE.decode(),
                f'"{snippet}..."',
            )

        # ------------------------------------------------------------ CRUD --
        r = await c.post(
            f"/api/w/{ws}/node",
            headers=headers,
            json={"name": "Manual Concept", "type": "concept"},
        )
        check("POST /node (create)", r.status_code == 201, str(r.json())[:70])
        new_id = r.json().get("id") if r.status_code == 201 else None

        r = await c.patch(
            f"/api/w/{ws}/node/{node_id}", headers=headers, json={"user_edited": True}
        )
        check("PATCH /node (mark correct)", r.status_code == 200, "inked")

        r = await c.get(f"/api/w/{ws}/graph", headers=headers)
        inked = [n for n in r.json()["nodes"] if n.get("user_edited")]
        check("inking persisted", len(inked) >= 1, f"{len(inked)} node(s) user_edited")

        if new_id:
            r = await c.post(
                f"/api/w/{ws}/edge",
                headers=headers,
                json={"source": new_id, "target": node_id, "type": "related_to"},
            )
            check("POST /edge (create)", r.status_code == 201, "edge created")

            r = await c.request(
                "DELETE",
                f"/api/w/{ws}/edge",
                headers=headers,
                json={"source": new_id, "target": node_id, "type": "related_to"},
            )
            check("DELETE /edge", r.status_code == 204, "removed")

            r = await c.post(
                f"/api/w/{ws}/edge",
                headers=headers,
                json={"source": new_id, "target": node_id, "type": "not_a_real_type"},
            )
            check(
                "POST /edge (bad relation type)",
                r.status_code == 400,
                "allowlist rejected it",
            )

            r = await c.delete(f"/api/w/{ws}/node/{new_id}", headers=headers)
            check("DELETE /node", r.status_code == 204, "removed")

        # ------------------------------------------------ delete document --
        r = await c.delete(f"/api/w/{ws}/documents/{doc_id}", headers=headers)
        check("DELETE /documents/<id>", r.status_code == 204, "subgraph removed")

        r = await c.get(f"/api/w/{ws}/graph", headers=headers)
        left = r.json()
        check(
            "subgraph deletion cleaned up",
            len(left["nodes"]) == 0,
            f"{len(left['nodes'])} nodes left (0 expected)",
        )

        # ---------------------------------------------------------- logout --
        r = await c.post("/auth/logout", headers=headers)
        check("POST /auth/logout", r.status_code == 200, "session deleted")

        r = await c.get("/", follow_redirects=False)
        check("GET  / (after logout)", r.status_code == 200 and "Create an account" in r.text, "back to the home page")

    await graph_client.close()
    await dispose()

    width = max(len(n) for n, _, _ in results) + 2
    print(f"\n{'endpoint / check':<{width}}{'':<6}note")
    print("-" * (width + 60))
    for name, status, note in results:
        print(f"{name:<{width}}{status:<6}{note}")

    failed = [n for n, s, _ in results if s == FAIL]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("FAILED: " + ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
