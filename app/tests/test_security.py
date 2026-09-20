"""Security tests.

Run with:  uv run python tests/test_security.py

These cover the controls that fail *silently* if broken. A missing workspace
filter does not raise -- it just returns someone else's data, and the app looks
fine while doing it. So the checks are mechanical and run without a database.
"""

import asyncio
import inspect
import io
import re
import tarfile

from graphforge.core import security
from graphforge.graph import client, queries
from graphforge.ingest import parsers


# --------------------------------------------------------------------------
# Tenant isolation
# --------------------------------------------------------------------------

def test_unscoped_query_is_refused() -> None:
    """The guard must reject Cypher that does not filter by workspace."""
    async def go():
        await client.run("MATCH (e:Entity) RETURN e", "some-workspace-id")

    try:
        asyncio.run(go())
    except client.TenantScopeError:
        return
    except Exception as exc:
        raise AssertionError(
            f"expected TenantScopeError, got {type(exc).__name__}: {exc}"
        ) from exc
    raise AssertionError("unscoped query was NOT refused")


def test_every_query_in_queries_module_is_scoped() -> None:
    """Static sweep: no Cypher literal in queries.py may omit $workspace_id.

    Catches a future edit that adds a query without the filter, which the
    runtime guard would only catch if that code path happened to be exercised.
    """
    source = inspect.getsource(queries)
    # Triple-quoted Cypher blocks passed to client.run
    blocks = re.findall(r'"""\s*(MATCH|MERGE|UNWIND|CREATE)(.*?)"""', source, re.S)
    assert blocks, "no Cypher blocks found -- did the module change shape?"

    for keyword, body in blocks:
        cypher = keyword + body
        assert "$workspace_id" in cypher, (
            "Cypher block without $workspace_id:\n" + cypher[:200]
        )


# --------------------------------------------------------------------------
# SSRF: repo import must not fetch arbitrary URLs
# --------------------------------------------------------------------------

def test_github_url_allowlist() -> None:
    ok, _ = parsers.parse_github_url("https://github.com/owner/repo")
    assert ok == "owner"
    parsers.parse_github_url("https://github.com/owner/repo.git")

    # Each of these is a way to reach somewhere we must never fetch.
    hostile = [
        "http://localhost:8000/admin",
        "http://169.254.169.254/latest/meta-data/",     # cloud metadata
        "https://github.com.evil.com/owner/repo",       # suffix confusion
        "https://evil.com/https://github.com/o/r",      # prefix confusion
        "file:///etc/passwd",
        "https://raw.githubusercontent.com/o/r/main/x", # not a repo URL
        "https://github.com/owner/repo/../../secrets",
    ]
    for url in hostile:
        try:
            parsers.parse_github_url(url)
        except parsers.UnsupportedFile:
            continue
        raise AssertionError(f"hostile URL was accepted: {url}")


# --------------------------------------------------------------------------
# Upload type sniffing: the extension is never trusted
# --------------------------------------------------------------------------

def test_mime_sniffing_ignores_extension() -> None:
    # A PDF is a PDF even when named .txt
    assert parsers.sniff_mime(b"%PDF-1.7\nrest", "notes.txt") == "application/pdf"

    # An executable renamed .pdf is not accepted as text or PDF
    try:
        parsers.sniff_mime(b"MZ\x90\x00\x03\x00\x00\x00", "invoice.pdf")
    except parsers.UnsupportedFile:
        pass
    else:
        raise AssertionError("binary payload accepted")

    # Plain text with a known suffix is fine
    assert parsers.sniff_mime(b"# Title\n\nbody", "readme.md") == "text/plain"


def test_oversized_upload_rejected() -> None:
    huge = b"%PDF-" + b"0" * (30 * 1024 * 1024)
    try:
        parsers.parse(huge, "big.pdf")
    except parsers.FileTooLarge:
        return
    raise AssertionError("oversized upload was not rejected")


# --------------------------------------------------------------------------
# Zip-slip: archive entries must not escape their root
# --------------------------------------------------------------------------

def _tarball(names: list[str]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name in names:
            payload = b"print('hi')\n"
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


def test_zip_slip_entries_are_skipped() -> None:
    data = _tarball([
        "repo-abc123/app.py",                  # legitimate
        "repo-abc123/../../../etc/cron.py",    # path traversal
        "/absolute/evil.py",                   # absolute path
        "repo-abc123/node_modules/dep.py",     # vendored, should be skipped
    ])
    files = parsers._read_tarball(data)
    paths = [p for p, _ in files]

    assert "app.py" in paths, "legitimate file was dropped"
    assert not any(".." in p for p in paths), f"traversal survived: {paths}"
    assert not any(p.startswith("/") for p in paths), f"absolute path survived: {paths}"
    assert not any("node_modules" in p for p in paths), f"vendored dir kept: {paths}"


# --------------------------------------------------------------------------
# Session tokens
# --------------------------------------------------------------------------

def test_session_tokens_are_hashed_and_unique() -> None:
    a, b = security.new_session_token(), security.new_session_token()
    assert a != b, "session tokens must not repeat"
    assert len(a) >= 32

    digest = security.hash_session_token(a)
    assert digest != a, "the raw token must never be what we store"
    assert digest == security.hash_session_token(a), "hashing must be deterministic"


def test_password_hashes_are_salted() -> None:
    pw = "correct-horse-battery-staple"
    assert security.hash_password(pw) != security.hash_password(pw), (
        "identical passwords must produce different hashes"
    )
    assert security.verify_password(pw, security.hash_password(pw))



# --------------------------------------------------------------------------
# The pages must not violate the CSP the app sets on them
# --------------------------------------------------------------------------

def test_templates_obey_the_csp() -> None:
    """Every <script> carries a nonce; nothing loads from a third-party host.

    The app sends `script-src 'self' 'nonce-...'` and `font-src 'self'`. An
    inline script without a nonce, or a Google Fonts link, keeps working in a
    permissive dev browser and silently dies in production.
    """
    import pathlib
    import re as _re

    root = pathlib.Path(__file__).resolve().parents[1] / "src" / "graphforge"
    templates = list((root / "web" / "templates").glob("*.html"))
    assert templates, "no templates found"

    for path in templates:
        html = path.read_text(encoding="utf-8")

        for tag in _re.findall(r"<script[^>]*>", html):
            assert "nonce=" in tag, f"{path.name}: <script> without a nonce: {tag}"

        for host in ("fonts.googleapis.com", "fonts.gstatic.com",
                     "cdnjs.", "cdn.jsdelivr", "unpkg."):
            assert host not in html, f"{path.name}: third-party host {host} is CSP-blocked"


def test_css_has_no_offsite_urls() -> None:
    """Fonts are self-hosted; font-src is 'self' and would block anything else."""
    import pathlib
    import re as _re

    css = (
        pathlib.Path(__file__).resolve().parents[1]
        / "src" / "graphforge" / "static" / "app.css"
    ).read_text(encoding="utf-8")

    for url in _re.findall(r"url\(([^)]+)\)", css):
        cleaned = url.strip("\"' ")
        assert cleaned.startswith("/static/"), f"off-site asset in app.css: {cleaned}"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print(chr(10) + "all security tests passed")
