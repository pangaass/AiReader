from __future__ import annotations

import hashlib
from pathlib import Path

from PIL import Image


def prepare_paper_context(source: Path, destination: Path, max_chars: int = 60_000) -> Path:
    """Create a compact, page-addressable text view of a PDF for the paper agent."""
    try:
        import pymupdf as fitz

        document = fitz.open(source)
        pages = []
        per_page = max(1_500, max_chars // max(len(document), 1))
        truncated = False
        for index, page in enumerate(document):
            text = page.get_text("text", sort=True).strip()
            truncated = truncated or len(text) > per_page
            text = text[:per_page]
            pages.append(
                {
                    "page": index + 1,
                    "width": round(page.rect.width, 2),
                    "height": round(page.rect.height, 2),
                    "text": text,
                }
            )
        page_count = len(document)
    except ImportError:
        from pypdf import PdfReader

        reader = PdfReader(source)
        pages = []
        per_page = max(1_500, max_chars // max(len(reader.pages), 1))
        truncated = False
        for index, page in enumerate(reader.pages):
            text = (page.extract_text() or "").strip()
            truncated = truncated or len(text) > per_page
            text = text[:per_page]
            pages.append({"page": index + 1, "text": text})
        page_count = len(reader.pages)
    destination.parent.mkdir(parents=True, exist_ok=True)
    header = [
        "# Deterministic PDF text extraction",
        "",
        f"- source_pdf: {source}",
        f"- source_sha256: {hashlib.sha256(source.read_bytes()).hexdigest()}",
        f"- page_count: {page_count}",
        f"- pages_in_context: {len(pages)}",
        f"- truncated: {str(truncated).lower()}",
        "",
    ]
    body: list[str] = []
    for page in pages:
        body.extend([f"## Page {page['page']}", "", page["text"], ""])
    destination.write_text("\n".join(header + body), encoding="utf-8")
    return destination


def prepare_presentation_units(
    source: Path, destination_dir: Path
) -> list[tuple[str, Path | None, str | None]]:
    """Split a presentation into bounded visual or textual agent units."""
    suffix = source.suffix.lower()
    destination_dir.mkdir(parents=True, exist_ok=True)
    if suffix == ".json":
        import json

        payload = json.loads(source.read_text(encoding="utf-8"))
        slides = list((payload.get("slides") or {}).items())
        units = []
        for start in range(0, len(slides), 4):
            chunk = dict(slides[start : start + 4])
            text = json.dumps(
                {"paper_title": payload.get("paper_title"), "slides": chunk},
                ensure_ascii=False,
                indent=2,
            )
            units.append((f"slides-{start + 1}-{start + len(chunk)}", None, text))
        return units or [("slides-empty", None, "{}")]
    if suffix in {".png", ".jpg", ".jpeg", ".webp"}:
        units = []
        with Image.open(source) as opened:
            image = opened.convert("RGB")
            width, height = image.size
            overlap_x, overlap_y = width // 20, height // 20
            boxes = [
                (0, 0, width // 2 + overlap_x, height // 2 + overlap_y),
                (width // 2 - overlap_x, 0, width, height // 2 + overlap_y),
                (0, height // 2 - overlap_y, width // 2 + overlap_x, height),
                (width // 2 - overlap_x, height // 2 - overlap_y, width, height),
            ]
            labels = ["top-left", "top-right", "bottom-left", "bottom-right"]
            for label, box in zip(labels, boxes):
                target = destination_dir / f"presentation-{label}.jpg"
                crop = image.crop(box)
                crop.thumbnail((1000, 1000))
                crop.save(target, "JPEG", quality=88, optimize=True)
                units.append((label, target, None))
        return units
    return [("document", source, None)]


def split_paper_context(context: str, max_chars: int = 16_000) -> list[tuple[str, str]]:
    """Group complete PDF pages into bounded model inputs."""
    parts = context.split("\n## Page ")
    header = parts[0]
    pages = ["## Page " + part for part in parts[1:]]
    units: list[tuple[str, str]] = []
    current: list[str] = []
    current_size = 0
    start_page = 1
    for index, page in enumerate(pages, 1):
        if current and current_size + len(page) > max_chars:
            units.append((f"pages-{start_page}-{index - 1}", header + "\n" + "\n".join(current)))
            current, current_size, start_page = [], 0, index
        current.append(page)
        current_size += len(page)
    if current:
        units.append((f"pages-{start_page}-{start_page + len(current) - 1}", header + "\n" + "\n".join(current)))
    return units
