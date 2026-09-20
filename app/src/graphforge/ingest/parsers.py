"""Turn an upload into plain text.

Parsers are the most dangerous code in the app: PyMuPDF and python-docx are C
extensions parsing attacker-supplied bytes, and both have had CVEs. Three rules
follow from that, and they are enforced here rather than trusted to callers:

1. **Never trust the filename.** Type comes from magic bytes.
2. **Check limits before parsing**, not after. A 300-page cap is worthless if
   we blow up memory discovering the page count.
3. **Repo import is an SSRF vector.** A URL that the server fetches is an
   attacker's tool for reaching your internal network, so only github.com is
   allowed and only through the API.
"""

from __future__ import annotations

import io
import logging
import re
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

import httpx

from graphforge.core.config import get_limits

log = logging.getLogger(__name__)

# Limits only -- deliberately not the full Settings, so upload validation can be
# reasoned about and tested without database credentials or an API key.
_lim = get_limits

# Magic-byte signatures. The extension is a hint from the user; this is evidence.
_SIGNATURES: list[tuple[bytes, str]] = [
    (b"%PDF-", "application/pdf"),
    # DOCX is a zip; distinguished from a plain zip by its inner structure below.
    (b"PK\x03\x04", "application/zip"),
]

TEXT_SUFFIXES = {".md", ".markdown", ".txt", ".rst"}
CODE_SUFFIXES = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".go", ".rs", ".rb", ".php",
    ".c", ".h", ".cpp", ".hpp", ".cs", ".sh", ".sql", ".yaml", ".yml", ".toml",
}

# Directories that are dependencies or build output, not the author's work.
_SKIP_DIRS = {
    "node_modules", ".git", "dist", "build", "vendor", "target", "__pycache__",
    ".venv", "venv", ".next", ".nuxt", "coverage", ".mypy_cache", ".pytest_cache",
}


class UnsupportedFile(ValueError):
    pass


class FileTooLarge(ValueError):
    pass


@dataclass
class ParsedDocument:
    text: str
    mime: str
    pages: int | None = None


def sniff_mime(data: bytes, filename: str = "") -> str:
    """Determine type from content. Falls back to extension only for plain text."""
    for signature, mime in _SIGNATURES:
        if data.startswith(signature):
            if mime == "application/zip":
                # A DOCX is a zip containing word/document.xml.
                try:
                    with zipfile.ZipFile(io.BytesIO(data)) as z:
                        if "word/document.xml" in z.namelist():
                            return (
                                "application/vnd.openxmlformats-officedocument"
                                ".wordprocessingml.document"
                            )
                except zipfile.BadZipFile:
                    pass
                return "application/zip"
            return mime

    # No signature: accept as text only if it decodes and has no NUL bytes.
    if b"\x00" not in data[:8192]:
        try:
            data[:8192].decode("utf-8")
        except UnicodeDecodeError:
            pass
        else:
            if Path(filename).suffix.lower() in TEXT_SUFFIXES | CODE_SUFFIXES:
                return "text/plain"
    raise UnsupportedFile(f"unsupported or unrecognised file type: {filename!r}")


def parse(data: bytes, filename: str = "") -> ParsedDocument:
    """Parse uploaded bytes into text. Enforces size and page limits first."""
    if len(data) > _lim().max_upload_bytes:
        raise FileTooLarge(
            f"file is {len(data) // 1024 // 1024} MB; the limit is "
            f"{_lim().max_upload_bytes // 1024 // 1024} MB"
        )

    mime = sniff_mime(data, filename)

    if mime == "application/pdf":
        return _parse_pdf(data)
    if mime.endswith("wordprocessingml.document"):
        return _parse_docx(data)
    if mime == "text/plain":
        return ParsedDocument(text=data.decode("utf-8", errors="replace"), mime=mime)

    raise UnsupportedFile(f"cannot extract text from {mime}")


def _parse_pdf(data: bytes) -> ParsedDocument:
    import pymupdf

    from graphforge.ingest import layout

    with pymupdf.open(stream=data, filetype="pdf") as doc:
        if doc.page_count > _lim().max_pdf_pages:
            raise FileTooLarge(
                f"PDF has {doc.page_count} pages; the limit is "
                f"{_lim().max_pdf_pages}. Please split it."
            )
        # Running headers/footers and the bibliography are stripped here rather
        # than downstream: once they reach the extraction model they become
        # entities, and no amount of filtering afterwards knows they were noise.
        text, report = layout.extract_clean_text(doc)
        pages = doc.page_count

    if report["lines_dropped"] or report["references_removed"]:
        log.info("pdf layout cleanup: %s", report)

    return ParsedDocument(
        text=text[: _lim().max_extracted_chars], mime="application/pdf", pages=pages
    )


def _parse_docx(data: bytes) -> ParsedDocument:
    import docx

    document = docx.Document(io.BytesIO(data))
    parts = [p.text for p in document.paragraphs]
    # Tables carry real content in reports; losing them loses the point.
    for table in document.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                parts.append(" | ".join(cells))

    return ParsedDocument(
        text="\n\n".join(parts)[: _lim().max_extracted_chars],
        mime=(
            "application/vnd.openxmlformats-officedocument"
            ".wordprocessingml.document"
        ),
    )


# ---------------------------------------------------------------------------
# GitHub repositories
# ---------------------------------------------------------------------------

_GITHUB_REPO = re.compile(
    r"^https?://(?:www\.)?github\.com/"
    r"(?P<owner>[A-Za-z0-9_.-]{1,39})/"
    r"(?P<repo>[A-Za-z0-9_.-]{1,100}?)(?:\.git)?/?$"
)


def parse_github_url(url: str) -> tuple[str, str]:
    """Accept only a github.com repo URL.

    An allowlist, not a denylist. Blocking `localhost` and `169.254.*` invites a
    game of whack-a-mole with DNS rebinding and redirects; permitting exactly one
    host does not.
    """
    match = _GITHUB_REPO.match(url.strip())
    if not match:
        raise UnsupportedFile(
            "only public github.com repository URLs are supported, "
            "e.g. https://github.com/owner/repo"
        )
    return match.group("owner"), match.group("repo")


async def fetch_repo_files(
    url: str, token: str | None = None
) -> list[tuple[str, str]]:
    """Download a repo tarball via the GitHub API and return (path, text) pairs.

    Uses the API rather than `git clone` so the URL is never handed to a shell
    and redirects cannot leave github.com.
    """
    owner, repo = parse_github_url(url)
    api = f"https://api.github.com/repos/{owner}/{repo}/tarball"
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=60.0,
        # Redirects are followed only within GitHub's own hosts.
        event_hooks={"response": [_reject_offsite_redirect]},
    ) as http:
        response = await http.get(api, headers=headers)
        response.raise_for_status()
        data = response.content

    if len(data) > _lim().max_upload_bytes * 4:
        raise FileTooLarge("repository archive is too large")

    return _read_tarball(data)


async def _reject_offsite_redirect(response: httpx.Response) -> None:
    location = response.headers.get("location")
    if location and not re.match(
        r"^https://([a-z0-9-]+\.)*(github\.com|githubusercontent\.com)/", location
    ):
        raise UnsupportedFile("refusing redirect away from GitHub")


def _read_tarball(data: bytes) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []

    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar:
            if len(out) >= _lim().max_repo_files:
                break
            if not member.isfile() or member.size > _lim().max_repo_file_bytes:
                continue

            # Zip-slip: reject any path escaping the archive root.
            path = Path(member.name)
            if path.is_absolute() or ".." in path.parts:
                continue
            # Strip GitHub's "<owner>-<repo>-<sha>/" wrapper directory.
            parts = path.parts[1:]
            if not parts or _SKIP_DIRS.intersection(parts):
                continue

            relative = Path(*parts)
            if relative.suffix.lower() not in TEXT_SUFFIXES | CODE_SUFFIXES:
                continue

            handle = tar.extractfile(member)
            if handle is None:
                continue
            raw = handle.read(_lim().max_repo_file_bytes)
            if b"\x00" in raw[:4096]:
                continue  # binary despite the extension
            out.append((str(relative).replace("\\", "/"), raw.decode("utf-8", "replace")))

    return out
