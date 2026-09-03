"""Data contracts owned by the PDF ingest and parsing stage."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal


SourceKind = Literal["local", "url"]


@dataclass(frozen=True)
class IngestedPDF:
    """An immutable, content-addressed PDF available to the parser."""

    kind: SourceKind
    sha256: str
    local_pdf: Path
    cached_pdf: Path
    original_url: str | None = None
    cache_hit: bool = False

    def to_source_dict(self, *, page_count: int) -> dict[str, object]:
        return {
            "kind": self.kind,
            "sha256": self.sha256,
            "page_count": page_count,
            "local_pdf": str(self.local_pdf),
            "original_url": self.original_url,
        }

    def to_manifest_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["local_pdf"] = str(self.local_pdf)
        value["cached_pdf"] = str(self.cached_pdf)
        return value


@dataclass(frozen=True)
class ParseOptions:
    """Runtime choices that are safe to persist in a stage manifest."""

    render_pages: bool = True
    extract_images: bool = True
    render_dpi: int = 144
    attempt: int = 1
    force: bool = False

