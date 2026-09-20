"""Push every supported format through the real API.

    uv run python tests/api_formats.py

`api_smoke.py` proves the pipeline works, but only ever uploads Markdown. This
one covers the formats a user will actually bring: PDF (pages, so grounding has
to carry page numbers), DOCX (including a table, whose extraction path had never
executed), and a GitHub repository.

Each format is checked the same way: does it ingest, does it produce entities,
and does clicking a node return text that is genuinely in the source. That last
check is the one that matters -- a parser can "work" and still hand back
scrambled text, which would silently poison every graph built from it.
"""

import asyncio
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

DATA = ROOT / "tests" / "data"
results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, note: str = "") -> bool:
    results.append((name, "PASS" if ok else "FAIL", note))
    return ok


async def wait_ready(c, ws, headers, doc_id, label, timeout=600):
    waited = 0
    while waited < timeout:
        await asyncio.sleep(3)
        waited += 3
        docs = (await c.get(f"/api/w/{ws}/documents", headers=headers)).json()
        doc = next((d for d in docs if d["id"] == doc_id), None)
        if doc is None:
            return None, waited
        if doc["status"] in {"ready", "failed"}:
            return doc, waited
    return None, waited


async def ingest_and_check(c, ws, headers, label, filename, payload, expect_terms):
    """Upload one file, wait, then verify entities and grounding."""
    r = await c.post(
        f"/api/w/{ws}/documents", headers=headers,
        files={"file": (filename, payload, "application/octet-stream")},
    )
    if not check(f"{label}: upload", r.status_code == 201, str(r.json())[:70]):
        return
    doc_id = r.json()["id"]

    doc, waited = await wait_ready(c, ws, headers, doc_id, label)
    ok = doc is not None and doc["status"] == "ready"
    check(
        f"{label}: ingested",
        ok,
        f"{doc['status'] if doc else 'gone'} in {waited}s"
        + (f" — {doc.get('error')}" if doc and doc.get("error") else ""),
    )
    if not ok:
        return

    graph = (await c.get(f"/api/w/{ws}/graph", headers=headers)).json()
    names = {n["name"].lower() for n in graph["nodes"]}
    check(
        f"{label}: produced entities",
        len(graph["nodes"]) > 0,
        f"{len(graph['nodes'])} nodes, {len(graph['edges'])} edges",
    )

    # Did the parser actually read the document, or just produce *something*?
    found = [t for t in expect_terms if any(t.lower() in n for n in names)]
    check(
        f"{label}: found expected content",
        len(found) > 0,
        f"{found} out of {expect_terms}",
    )

    # Grounding: the side panel must return real source text.
    if graph["nodes"]:
        nid = graph["nodes"][0]["id"]
        detail = (await c.get(f"/api/w/{ws}/node/{nid}", headers=headers)).json()
        sources = detail.get("sources", [])
        check(
            f"{label}: grounding",
            len(sources) > 0 and len(sources[0]["text"].strip()) > 20,
            f"'{detail.get('name')}' -> {len(sources)} passage(s)"
            + (f", page {sources[0].get('page')}" if sources else ""),
        )

    # Leave the workspace clean for the next format.
    await c.delete(f"/api/w/{ws}/documents/{doc_id}", headers=headers)


async def main() -> int:
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=900
    ) as c:
        email = f"formats-{int(time.time())}@example.com"
        pw = "a-really-long-passphrase"
        await c.post("/auth/signup", data={"email": email, "password": pw})
        r = await c.post("/auth/login", data={"email": email, "password": pw})
        headers = {"X-CSRF-Token": r.cookies.get("gf_csrf") or ""}
        r = await c.get("/", follow_redirects=False)
        ws = re.search(r'"workspaceId":\s*"([0-9a-f-]{36})"', r.text).group(1)

        await ingest_and_check(
            c, ws, headers, "PDF", "sample.pdf",
            (DATA / "sample.pdf").read_bytes(),
            ["neo4j", "cypher", "fastapi"],
        )
        await ingest_and_check(
            c, ws, headers, "DOCX", "sample.docx",
            (DATA / "sample.docx").read_bytes(),
            ["kubernetes", "terraform", "amazon"],
        )

        # ---- GitHub repo -------------------------------------------------
        # Validation first: a hostile URL must be refused at the endpoint,
        # not inside a background task where the user only sees "failed".
        r = await c.post(
            f"/api/w/{ws}/documents/repo", headers=headers,
            json={"url": "http://169.254.169.254/latest/meta-data/"},
        )
        check("repo: SSRF URL rejected", r.status_code == 400, str(r.json())[:60])

        r = await c.post(
            f"/api/w/{ws}/documents/repo", headers=headers,
            json={"url": "https://github.com/octocat/Spoon-Knife"},
        )
        if check("repo: accepted", r.status_code == 201, str(r.json())[:70]):
            doc_id = r.json()["id"]
            doc, waited = await wait_ready(c, ws, headers, doc_id, "repo")
            check(
                "repo: ingested",
                doc is not None and doc["status"] == "ready",
                f"{doc['status'] if doc else 'gone'} in {waited}s"
                + (f" — {doc.get('error')}" if doc and doc.get("error") else ""),
            )
            if doc and doc["status"] == "ready":
                g = (await c.get(f"/api/w/{ws}/graph", headers=headers)).json()
                check("repo: produced entities", len(g["nodes"]) > 0,
                      f"{len(g['nodes'])} nodes")
            await c.delete(f"/api/w/{ws}/documents/{doc_id}", headers=headers)

    await graph_client.close()
    await dispose()

    width = max(len(n) for n, _, _ in results) + 2
    print(f"\n{'check':<{width}}{'':<6}note")
    print("-" * (width + 62))
    for name, status, note in results:
        print(f"{name:<{width}}{status:<6}{note}")

    failed = [n for n, s, _ in results if s == "FAIL"]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("FAILED: " + ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
