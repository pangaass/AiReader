"""Safe local/HTTP(S) PDF ingest with content-addressed caching."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .models import IngestedPDF


class IngestError(RuntimeError):
    """Raised when an input cannot be safely ingested as a PDF."""


class _LimitedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, limit: int) -> None:
        super().__init__()
        self.limit = limit
        self.count = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        self.count += 1
        if self.count > self.limit:
            raise IngestError(f"PDF download exceeded {self.limit} redirects")
        scheme = urllib.parse.urlparse(newurl).scheme.lower()
        if scheme not in {"http", "https"}:
            raise IngestError(f"Redirected to unsupported URL scheme: {scheme or '<empty>'}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _safe_url_for_manifest(url: str) -> str:
    """Remove credentials and redact likely secrets before a URL is persisted."""

    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    safe_query: list[tuple[str, str]] = []
    for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        sensitive = any(marker in key.casefold() for marker in ("key", "token", "secret", "password", "signature", "credential"))
        safe_query.append((key, "REDACTED" if sensitive else value))
    return urllib.parse.urlunsplit((parsed.scheme, host, parsed.path, urllib.parse.urlencode(safe_query), ""))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_pdf(path: Path) -> None:
    if not path.is_file():
        raise IngestError(f"PDF input does not exist or is not a file: {path}")
    if path.stat().st_size == 0:
        raise IngestError("PDF input is empty")
    with path.open("rb") as stream:
        if stream.read(5) != b"%PDF-":
            raise IngestError("Input does not have a PDF file signature")


def _download(
    url: str,
    destination: Path,
    *,
    max_bytes: int,
    timeout_seconds: int,
    max_redirects: int,
) -> None:
    handler = _LimitedRedirectHandler(max_redirects)
    opener = urllib.request.build_opener(handler)
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "PaperVisualizer/0.1 (+local research tool)"},
        method="GET",
    )
    try:
        with opener.open(request, timeout=timeout_seconds) as response, destination.open("wb") as output:
            declared = response.headers.get("Content-Length")
            if declared and int(declared) > max_bytes:
                raise IngestError(f"PDF exceeds download limit of {max_bytes} bytes")
            total = 0
            while True:
                chunk = response.read(min(1024 * 1024, max_bytes + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise IngestError(f"PDF exceeds download limit of {max_bytes} bytes")
                output.write(chunk)
    except IngestError:
        raise
    except (OSError, ValueError, urllib.error.URLError) as exc:
        # urllib exception strings can contain signed URLs; keep them out of logs/artifacts.
        raise IngestError(f"Unable to download PDF ({type(exc).__name__})") from None


def ingest_pdf(
    source: str | Path,
    *,
    artifact_dir: Path,
    cache_dir: Path,
    max_download_mb: int = 80,
    timeout_seconds: int = 45,
    max_redirects: int = 5,
) -> IngestedPDF:
    """Resolve *source*, validate it, and persist an immutable SHA-256 copy.

    The function never loads dotenv files. Network input is restricted to HTTP(S),
    bounded by size/time/redirect limits, and validated by the PDF signature.
    """

    artifact_dir = artifact_dir.resolve()
    cache_dir = cache_dir.resolve()
    artifact_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    text = str(source)
    parsed = urllib.parse.urlparse(text)
    is_url = parsed.scheme.lower() in {"http", "https"}
    if parsed.scheme and not is_url:
        raise IngestError(f"Unsupported input scheme: {parsed.scheme}")

    temporary_path: Path | None = None
    if is_url:
        fd, temporary_name = tempfile.mkstemp(prefix="paper-", suffix=".pdf", dir=str(cache_dir))
        os.close(fd)
        temporary_path = Path(temporary_name)
        try:
            _download(
                text,
                temporary_path,
                max_bytes=max_download_mb * 1024 * 1024,
                timeout_seconds=timeout_seconds,
                max_redirects=max_redirects,
            )
            _validate_pdf(temporary_path)
            input_path = temporary_path
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise
        kind = "url"
        original_url = _safe_url_for_manifest(text)
    else:
        input_path = Path(source).expanduser().resolve()
        _validate_pdf(input_path)
        kind = "local"
        original_url = None

    digest = _sha256(input_path)
    cached_pdf = cache_dir / digest / "ingest" / "source.pdf"
    cache_hit = cached_pdf.is_file()
    cached_pdf.parent.mkdir(parents=True, exist_ok=True)
    if not cache_hit:
        staging = cached_pdf.with_suffix(".pdf.tmp")
        shutil.copyfile(input_path, staging)
        os.replace(staging, cached_pdf)
    if temporary_path is not None:
        temporary_path.unlink(missing_ok=True)

    local_pdf = artifact_dir / "source.pdf"
    if local_pdf.resolve() != cached_pdf.resolve():
        staging = local_pdf.with_suffix(".pdf.tmp")
        shutil.copyfile(cached_pdf, staging)
        os.replace(staging, local_pdf)
    return IngestedPDF(kind, digest, local_pdf, cached_pdf, original_url, cache_hit)
