"""Layout-aware PDF parsing with deterministic local fallbacks."""

from __future__ import annotations

import json
import math
import re
import shutil
import struct
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..artifacts import atomic_write_json
from .ingest import ingest_pdf
from .models import IngestedPDF, ParseOptions


STAGE_VERSION = "1.2.7"
_CAPTION_RE = re.compile(r"^\s*(fig(?:ure)?\.?|table)\s*([A-Z]?\d+|[IVX]+)\s*(?:[:.\-–—])\s*(.*)$", re.I)
_NUMBERED_HEADING_RE = re.compile(r"^\s*(\d+(?:\.\d+){0,4})[.)]?\s+([^\n]{2,120})$")
_APPENDIX_HEADING_RE = re.compile(r"^\s*(?:appendix(?:\s+[A-Z0-9])?|[A-Z](?:\.\d+)*[.)]?)\s+[A-Z][^\n]{1,120}$")
_REFERENCE_HEADING_RE = re.compile(r"^\s*(references|bibliography)\s*$", re.I)
_REFERENCE_START_RE = re.compile(r"^\s*(?:\[(\d{1,4})\]|(\d{1,3})[.)])\s+")
_FORMULA_LABEL_RE = re.compile(r"\((\d{1,4})\)\s*$")
_FORMULA_INTRO_RE = re.compile(r"(?:as\s+follows|formula|defined\s+by|given\s+by|compute(?:d)?\s+as)\s*:\s*$", re.I)
_FORMULA_LHS_RE = re.compile(
    r"^\s*(?:where\s+)?[A-Za-z\u0370-\u03ff\u1f00-\u1fff][A-Za-z0-9_\u0370-\u03ff\u1f00-\u1fff]*"
    r"(?:\s*\([^=\n]{0,120}\))?\s*(?::=|=)",
    re.I,
)
_MATH_OPERATOR_RE = re.compile(r"(?:=|:=|∈|≤|≥|≈|→|√|∑|Σ|∫|·|×|≻|−)")
_PROSE_FORMULA_PREFIX_RE = re.compile(
    r"^(?:we\s+(?:used|use|varied)|the\s+(?:main|default)|missing\s+correct|deviation|"
    r"a\s+positive|increased\s+the|used\s+beam|with(?=\s|[\u0370-\u03ff])|fies\b|\(r\d+\s*=)",
    re.I,
)


class ParseError(RuntimeError):
    """Raised when a PDF has no usable parse result."""


def _block_id(page: int, order: int) -> str:
    return f"block:p{page:04d}:{order:04d}"


def _bounded_bbox(values: Iterable[float], width: float, height: float) -> list[float]:
    raw = list(values)
    if len(raw) != 4:
        return [0.0, 0.0, width, height]
    x0, y0, x1, y1 = (float(item) if math.isfinite(float(item)) else 0.0 for item in raw)
    x0, x1 = sorted((max(0.0, min(x0, width)), max(0.0, min(x1, width))))
    y0, y1 = sorted((max(0.0, min(y0, height)), max(0.0, min(y1, height))))
    return [x0, y0, x1, y1]


def _layout_reading_order(blocks: list[dict[str, Any]], page_width: float) -> list[dict[str, Any]]:
    """Order common two-column papers by column, preserving spanning anchors."""

    if len(blocks) < 4:
        return sorted(blocks, key=lambda item: (item["bbox"][1], item["bbox"][0]))
    narrow = [item for item in blocks if item["bbox"][2] - item["bbox"][0] <= page_width * 0.62]
    left = [item for item in narrow if item["bbox"][0] < page_width * 0.30 and item["bbox"][2] < page_width * 0.62]
    right = [item for item in narrow if item["bbox"][0] > page_width * 0.38 and item["bbox"][2] > page_width * 0.70]
    if len(left) < 2 or len(right) < 2:
        return sorted(blocks, key=lambda item: (item["bbox"][1], item["bbox"][0]))
    spanning = [
        item
        for item in blocks
        if item not in narrow
        or item["bbox"][0] < page_width * 0.34 and item["bbox"][2] > page_width * 0.66
    ]
    anchors = sorted(spanning, key=lambda item: (item["bbox"][1], item["bbox"][0]))
    remaining = [item for item in blocks if item not in spanning]
    ordered: list[dict[str, Any]] = []
    lower = float("-inf")
    for anchor in [*anchors, None]:
        upper = anchor["bbox"][1] if anchor is not None else float("inf")
        band = [item for item in remaining if lower <= item["bbox"][1] < upper]
        band_left = sorted(
            (item for item in band if (item["bbox"][0] + item["bbox"][2]) / 2 < page_width / 2),
            key=lambda item: (item["bbox"][1], item["bbox"][0]),
        )
        band_right = sorted(
            (item for item in band if (item["bbox"][0] + item["bbox"][2]) / 2 >= page_width / 2),
            key=lambda item: (item["bbox"][1], item["bbox"][0]),
        )
        ordered.extend(band_left)
        ordered.extend(band_right)
        if anchor is not None:
            ordered.append(anchor)
            lower = anchor["bbox"][3]
    # Overlapping spanning anchors can leave a narrow block outside all bands.
    missing = [item for item in blocks if item not in ordered]
    ordered.extend(sorted(missing, key=lambda item: (item["bbox"][1], item["bbox"][0])))
    return ordered


def _classify_role(text: str, bbox: list[float], page_height: float) -> str:
    stripped = " ".join(text.split())
    lowered = stripped.casefold()
    if bbox[1] <= page_height * 0.055 and len(stripped) < 180:
        return "header"
    if bbox[3] >= page_height * 0.945 and len(stripped) < 180:
        return "footer"
    if _CAPTION_RE.match(stripped):
        return "caption"
    if _REFERENCE_HEADING_RE.match(stripped) or _REFERENCE_START_RE.match(stripped):
        return "reference"
    if _NUMBERED_HEADING_RE.match(stripped) or _APPENDIX_HEADING_RE.match(stripped) or lowered in {
        "abstract", "introduction", "background", "method", "methods", "methodology",
        "experiments", "results", "discussion", "conclusion", "conclusions",
        "references", "bibliography", "appendix", "related work",
    }:
        return "heading"
    if _looks_like_formula(stripped):
        return "formula"
    return "body"


def _looks_like_formula(text: str) -> bool:
    if not text or len(text) > 500:
        return False
    if _FORMULA_LABEL_RE.search(text) and any(token in text for token in ("=", "∑", "Σ", "∫", "→", "≈")):
        return True
    math_tokens = sum(text.count(token) for token in ("=", "∑", "Σ", "∫", "√", "≤", "≥", "→", "∂", "λ", "μ", "σ"))
    word_count = len(re.findall(r"[A-Za-z]{3,}", text))
    return math_tokens >= 2 and word_count <= 12


def _trim_formula_prose(text: str) -> str:
    """Remove a prose sentence accidentally appended after a complete equation."""

    normalized = " ".join(text.split())
    if "≻" in normalized:
        expression = re.search(r"[A-Za-z\u0370-\u03ff\u1f00-\u1fff]\s*\d+\s*[−-]", normalized)
        if expression and re.search(r"[A-Za-z]{3,}", normalized[: expression.start()]):
            normalized = normalized[expression.start() :]
    for match in re.finditer(r"\.\s+(?=[A-Z][a-z]{2,}\b)", normalized):
        left = normalized[: match.start()].rstrip()
        if "=" in left and len(_MATH_OPERATOR_RE.findall(left)) >= 2:
            return left
    return normalized


def _formula_signal(text: str, *, introduced: bool = False) -> bool:
    """Return true for display equations, not prose that merely contains values."""

    stripped = _trim_formula_prose(text)
    if not stripped or "=" not in stripped or _PROSE_FORMULA_PREFIX_RE.match(stripped):
        return False
    label = bool(_FORMULA_LABEL_RE.search(stripped))
    direct_lhs = bool(_FORMULA_LHS_RE.match(stripped))
    callable_lhs = bool(
        re.match(
            r"^\s*[A-Za-z\u0370-\u03ff\u1f00-\u1fff][A-Za-z0-9_\u0370-\u03ff\u1f00-\u1fff]*\s*\([^=]{1,120}\)\s*=",
            stripped,
        )
    )
    operators = len(_MATH_OPERATOR_RE.findall(stripped))
    prefix = stripped.split("=", 1)[0]
    prefix_words = re.findall(r"[A-Za-z]{2,}", prefix)
    rhs_words = re.findall(r"[A-Za-z]{3,}", stripped.split("=", 1)[1])
    labelled_comparison = "≻" in stripped and len(prefix_words) <= 3
    symbolic_difference = bool(
        re.fullmatch(
            r"\s*[A-Za-z\u0370-\u03ff\u1f00-\u1fff][A-Za-z0-9_\u0370-\u03ff\u1f00-\u1fff]*"
            r"(?:\s*[+\-−]\s*[A-Za-z\u0370-\u03ff\u1f00-\u1fff][A-Za-z0-9_\u0370-\u03ff\u1f00-\u1fff]*)+\s*",
            prefix,
        )
    )
    if introduced:
        return operators >= 1 and len(re.findall(r"[A-Za-z]{3,}", stripped)) <= 8
    if stripped.casefold().startswith("where ") and direct_lhs:
        return True
    if callable_lhs or label:
        return direct_lhs or callable_lhs or labelled_comparison
    if len(rhs_words) >= 3 and not labelled_comparison:
        return False
    if labelled_comparison or (direct_lhs or symbolic_difference) and operators >= 2:
        return True
    return False


def _formula_needs_continuation(text: str) -> bool:
    compact = " ".join(text.split())
    if _FORMULA_LABEL_RE.search(compact):
        return False
    opening = compact.count("(") + compact.count("[") + compact.count("{")
    closing = compact.count(")") + compact.count("]") + compact.count("}")
    return opening > closing or bool(re.search(r"(?:=|[+\-−*/·×,(])\s*$", compact))


def _formula_continuation(text: str) -> bool:
    stripped = " ".join(text.split())
    if not stripped or _PROSE_FORMULA_PREFIX_RE.match(stripped):
        return False
    return bool(_MATH_OPERATOR_RE.search(stripped) or _FORMULA_LABEL_RE.search(stripped) or re.match(r"^[)\]}A-Za-z0-9_\u0370-\u03ff\u1f00-\u1fff√−]", stripped))


def _formula_fragment(text: str, *, introduced: bool) -> tuple[str, int, int] | None:
    """Find a display equation occupying one line inside a larger text block."""

    source = text.strip()
    if _formula_signal(source, introduced=introduced):
        trimmed = _trim_formula_prose(source)
        start = source.find(trimmed)
        return trimmed, max(0, start), max(0, start) + len(trimmed)
    offset = 0
    previous_intro = introduced
    for raw_line in source.splitlines(keepends=True):
        line = raw_line.strip()
        leading = len(raw_line) - len(raw_line.lstrip())
        if line and _formula_signal(line, introduced=previous_intro):
            trimmed = _trim_formula_prose(line)
            local = raw_line.find(trimmed)
            start = offset + (local if local >= 0 else leading)
            return trimmed, start, start + len(trimmed)
        previous_intro = bool(_FORMULA_INTRO_RE.search(" ".join(line.split())))
        offset += len(raw_line)
    return None


def _formula_fragments(text: str, *, introduced: bool) -> list[tuple[str, int, int]]:
    """Return separate, continuous equation spans from one extractor block."""

    source = text.strip()
    if "\n" not in source:
        fragment = _formula_fragment(source, introduced=introduced)
        return [fragment] if fragment else []
    fragments: list[tuple[str, int, int]] = []
    current: tuple[str, int, int] | None = None
    offset = 0
    previous_intro = introduced
    for raw_line in source.splitlines(keepends=True):
        line = raw_line.strip()
        local = raw_line.find(line) if line else 0
        equation = _trim_formula_prose(line) if line and _formula_signal(line, introduced=previous_intro) else None
        if equation:
            if current is not None:
                fragments.append(current)
            equation_start = raw_line.find(equation)
            start = offset + (equation_start if equation_start >= 0 else local)
            current = (equation, start, start + len(equation))
        elif current is not None and line and _formula_continuation(line) and not re.search(r"[A-Za-z]{3,}\s+[A-Za-z]{3,}", line):
            current = (f"{current[0]} {line}", current[1], offset + local + len(line))
        elif current is not None:
            fragments.append(current)
            current = None
        previous_intro = bool(_FORMULA_INTRO_RE.search(" ".join(line.split())))
        offset += len(raw_line)
    if current is not None:
        fragments.append(current)
    if fragments:
        return fragments
    fragment = _formula_fragment(source, introduced=introduced)
    return [fragment] if fragment else []


def _extract_with_pymupdf(pdf_path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    try:
        import pymupdf  # type: ignore
    except ImportError:
        try:
            import fitz as pymupdf  # type: ignore
        except ImportError as exc:
            raise ParseError("PyMuPDF is unavailable") from exc

    pages: list[dict[str, Any]] = []
    embedded_images: list[dict[str, Any]] = []
    warnings: list[str] = []
    try:
        document = pymupdf.open(str(pdf_path))
    except Exception as exc:
        raise ParseError(f"PyMuPDF could not open the PDF: {exc}") from exc
    if getattr(document, "needs_pass", False):
        document.close()
        raise ParseError("Encrypted PDF requires a password")
    try:
        for page_index, page in enumerate(document):
            page_number = page_index + 1
            width, height = float(page.rect.width), float(page.rect.height)
            data = page.get_text("dict", sort=True)
            extracted_blocks: list[dict[str, Any]] = []
            for raw in data.get("blocks", []):
                if raw.get("type") != 0:
                    continue
                lines: list[str] = []
                sizes: list[float] = []
                for line in raw.get("lines", []):
                    spans = line.get("spans", [])
                    line_text = "".join(str(span.get("text", "")) for span in spans).strip()
                    if line_text:
                        lines.append(line_text)
                    sizes.extend(float(span.get("size", 0.0)) for span in spans if span.get("size"))
                text = "\n".join(lines).strip()
                if not text:
                    continue
                bbox = _bounded_bbox(raw.get("bbox", (0, 0, width, height)), width, height)
                extracted_blocks.append({
                    "text": text,
                    "bbox": bbox,
                    "font_size_median": sorted(sizes)[len(sizes) // 2] if sizes else None,
                    "coordinate_precision": "exact",
                })
            blocks: list[dict[str, Any]] = []
            for order, block in enumerate(_layout_reading_order(extracted_blocks, width)):
                block["id"] = _block_id(page_number, order)
                block["order"] = order
                block["role"] = _classify_role(block["text"], block["bbox"], height)
                blocks.append(block)
            pages.append({"number": page_number, "width": width, "height": height, "blocks": blocks, "render_path": None})
            for image_order, image in enumerate(page.get_images(full=True)):
                xref = int(image[0])
                try:
                    info = document.extract_image(xref)
                except Exception as exc:
                    warnings.append(f"page {page_number}: embedded image {xref} could not be extracted: {exc}")
                    continue
                rects = page.get_image_rects(xref)
                source_bbox = _bounded_bbox(rects[0], width, height) if rects else None
                embedded_images.append({
                    "id": f"image:p{page_number:04d}:{image_order:03d}",
                    "page": page_number,
                    "xref": xref,
                    "extension": str(info.get("ext") or "bin").lower(),
                    "bytes": info.get("image", b""),
                    "width": info.get("width"),
                    "height": info.get("height"),
                    "source_bbox": source_bbox,
                    "extraction_method": "pymupdf_embedded",
                })
    finally:
        document.close()
    return pages, embedded_images, warnings


def _extract_with_pypdf(pdf_path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ParseError("Neither PyMuPDF nor pypdf is available") from exc
    try:
        reader = PdfReader(str(pdf_path))
    except Exception as exc:
        raise ParseError(f"pypdf could not open the PDF: {exc}") from exc
    if reader.is_encrypted:
        raise ParseError("Encrypted PDF requires a password")
    pages: list[dict[str, Any]] = []
    images: list[dict[str, Any]] = []
    warnings = ["Used pypdf fallback; text block coordinates are estimated"]
    for page_index, page in enumerate(reader.pages):
        page_number = page_index + 1
        width = float(page.mediabox.width)
        height = float(page.mediabox.height)
        caption_positions: list[tuple[str, list[float]]] = []

        def capture_caption(text_fragment, cm, tm, font, font_size):  # type: ignore[no-untyped-def]
            normalized = " ".join(str(text_fragment).split())
            if not _CAPTION_RE.match(normalized):
                return
            size = max(1.0, float(font_size or 10.0))
            x0 = float(tm[4])
            y0 = height - float(tm[5]) - size
            estimated_width = min(width - x0, max(size * 3, len(normalized) * size * 0.46))
            caption_positions.append((normalized, _bounded_bbox([x0, y0, x0 + estimated_width, y0 + size * 1.35], width, height)))

        try:
            text = page.extract_text(visitor_text=capture_caption) or ""
        except Exception as exc:
            warnings.append(f"page {page_number}: text extraction failed: {exc}")
            text = ""
        # Keep PDF line boundaries in the fallback. This produces less semantic
        # grouping than PyMuPDF, but preserves caption/reference/formula starts
        # and never merges non-contiguous passages into a single source block.
        paragraphs = [line.strip() for line in text.splitlines() if line.strip()]
        blocks: list[dict[str, Any]] = []
        slot = height / max(1, len(paragraphs))
        for order, paragraph in enumerate(paragraphs):
            bbox = [0.0, order * slot, width, min(height, (order + 1) * slot)]
            normalized = " ".join(paragraph.split())
            for candidate_text, candidate_bbox in caption_positions:
                if normalized.startswith(candidate_text) or candidate_text.startswith(normalized):
                    bbox = candidate_bbox
                    break
            blocks.append({
                "id": _block_id(page_number, order),
                "text": paragraph,
                "bbox": bbox,
                "order": order,
                "role": _classify_role(paragraph, bbox, height),
                "font_size_median": None,
                "coordinate_precision": "text_matrix" if bbox in [item[1] for item in caption_positions] else "estimated",
            })
        try:
            for image_order, image in enumerate(page.images):
                images.append({
                    "id": f"image:p{page_number:04d}:{image_order:03d}",
                    "page": page_number,
                    "extension": Path(image.name).suffix.lstrip(".") or "bin",
                    "bytes": image.data,
                    "width": None,
                    "height": None,
                    "extraction_method": "pypdf_embedded",
                })
        except Exception as exc:
            warnings.append(f"page {page_number}: pypdf image extraction failed: {exc}")
        pages.append({"number": page_number, "width": width, "height": height, "blocks": blocks, "render_path": None})
    return pages, images, warnings


def _mark_repeated_margins(pages: list[dict[str, Any]]) -> None:
    candidates: list[tuple[dict[str, Any], str]] = []
    for page in pages:
        height = page["height"]
        for block in page["blocks"]:
            role = block["role"]
            if role not in {"header", "footer"}:
                continue
            normalized = re.sub(r"\d+", "#", " ".join(block["text"].casefold().split()))
            if normalized:
                candidates.append((block, normalized))
    counts = Counter(normalized for _, normalized in candidates)
    threshold = max(2, math.ceil(len(pages) * 0.4))
    for block, normalized in candidates:
        if counts[normalized] < threshold:
            block["role"] = "body"


def _sections(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    headings: list[tuple[int, dict[str, Any]]] = []
    for page in pages:
        body_sizes = [block["font_size_median"] for block in page["blocks"] if block["role"] == "body" and block["font_size_median"]]
        median_body = sorted(body_sizes)[len(body_sizes) // 2] if body_sizes else None
        for block in page["blocks"]:
            text = " ".join(block["text"].split())
            numbered = _NUMBERED_HEADING_RE.match(text)
            font_heading = median_body and block["font_size_median"] and block["font_size_median"] >= median_body * 1.18 and len(text) <= 140
            if block["role"] == "heading" or numbered or font_heading:
                block["role"] = "heading"
                headings.append((page["number"], block))
    sections: list[dict[str, Any]] = []
    for index, (page_number, heading) in enumerate(headings):
        title = " ".join(heading["text"].split())
        match = _NUMBERED_HEADING_RE.match(title)
        level = match.group(1).count(".") + 1 if match else 1
        end_page = headings[index + 1][0] if index + 1 < len(headings) else pages[-1]["number"]
        block_ids: list[str] = []
        next_heading_id = headings[index + 1][1]["id"] if index + 1 < len(headings) else None
        collecting = False
        for page in pages:
            if not page_number <= page["number"] <= end_page:
                continue
            for block in page["blocks"]:
                if block["id"] == heading["id"]:
                    collecting = True
                if block["id"] == next_heading_id:
                    collecting = False
                if collecting:
                    block_ids.append(block["id"])
        sections.append({
            "id": f"section:{index + 1:03d}",
            "title": title,
            "level": level,
            "page_start": page_number,
            "page_end": end_page,
            "block_ids": block_ids,
            "heading_block_id": heading["id"],
        })
    return sections


def _captions(pages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    figures: list[dict[str, Any]] = []
    tables: list[dict[str, Any]] = []
    seen_labels: set[tuple[str, str]] = set()
    for page in pages:
        blocks = page["blocks"]
        for block_index, block in enumerate(blocks):
            match = _CAPTION_RE.match(" ".join(block["text"].split()))
            if not match:
                continue
            family = match.group(1).casefold()
            kind = "table" if family == "table" else "figure"
            label_key = (kind, match.group(2).casefold())
            # Labels are document-level identifiers. A later paragraph that
            # begins with an existing label is a prose reference, not another
            # caption, and must remain available as body Evidence.
            if label_key in seen_labels:
                if block["role"] == "caption":
                    block["role"] = "body"
                continue
            seen_labels.add(label_key)
            target = tables if kind == "table" else figures
            caption_blocks = [block]
            caption_text = block["text"]
            cursor = block_index + 1
            while re.search(r"-\s*$", caption_text) and cursor < len(blocks):
                continuation = blocks[cursor]
                vertical_gap = continuation["bbox"][1] - caption_blocks[-1]["bbox"][3]
                if (
                    continuation["order"] != caption_blocks[-1]["order"] + 1
                    or continuation["role"] != "body"
                    or vertical_gap > max(12.0, page["height"] * 0.025)
                    or _horizontal_overlap(continuation["bbox"], block["bbox"]) < 0.25
                ):
                    break
                caption_blocks.append(continuation)
                caption_text = f"{caption_text}\n{continuation['text']}"
                continuation["role"] = "caption"
                cursor += 1
            target.append({
                "id": f"{kind}:{len(target) + 1:03d}",
                "label": match.group(2),
                "caption": caption_text,
                "page": page["number"],
                "caption_block_ids": [item["id"] for item in caption_blocks],
                "bbox": [
                    min(item["bbox"][0] for item in caption_blocks),
                    min(item["bbox"][1] for item in caption_blocks),
                    max(item["bbox"][2] for item in caption_blocks),
                    max(item["bbox"][3] for item in caption_blocks),
                ],
                "asset_path": None,
                "asset_id": None,
                "match_status": "caption_only",
            })
            block["role"] = "caption"
    return figures, tables


def _formula_candidates(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    formulas: list[dict[str, Any]] = []
    for page in pages:
        blocks = sorted(page["blocks"], key=lambda item: item["order"])
        # Initial role classification is intentionally broad. Reset it here so
        # parameter sentences and broken prose are not promoted as formulas.
        for block in blocks:
            if block["role"] == "formula":
                block["role"] = "body"
        index = 0
        while index < len(blocks):
            block = blocks[index]
            previous = blocks[index - 1] if index else None
            introduced = bool(previous and _FORMULA_INTRO_RE.search(" ".join(previous["text"].split())))
            fragments = _formula_fragments(block["text"], introduced=introduced)
            if block["role"] in {"heading", "caption", "reference", "header", "footer"} or not fragments:
                index += 1
                continue
            if len(fragments) > 1:
                for text, fragment_start, fragment_end in fragments:
                    label_match = _FORMULA_LABEL_RE.search(" ".join(text.split()))
                    formulas.append({
                        "id": f"formula-candidate:{len(formulas) + 1:03d}",
                        "text": text,
                        "label": label_match.group(1) if label_match else None,
                        "page": page["number"],
                        "block_ids": [block["id"]],
                        "bbox": list(block["bbox"]),
                        "confidence": "high" if label_match else "candidate",
                        "char_start": fragment_start,
                        "char_end": fragment_end,
                    })
                index += 1
                continue
            fragment = fragments[0]
            selected = [block]
            combined, fragment_start, fragment_end = fragment
            cursor = index + 1
            while cursor < len(blocks) and len(selected) < 5:
                next_block = blocks[cursor]
                if next_block["order"] != selected[-1]["order"] + 1 or next_block["role"] in {"heading", "caption", "reference", "header", "footer"}:
                    break
                next_text = " ".join(next_block["text"].split())
                next_has_label = bool(_FORMULA_LABEL_RE.search(next_text))
                if not (_formula_needs_continuation(combined) or next_has_label and _formula_continuation(next_text)):
                    break
                if not _formula_continuation(next_text):
                    break
                selected.append(next_block)
                combined = "\n".join((combined, next_text))
                cursor += 1
                if _FORMULA_LABEL_RE.search(" ".join(combined.split())):
                    break
            text = _trim_formula_prose(combined)
            label_match = _FORMULA_LABEL_RE.search(" ".join(text.split()))
            bbox = [
                min(item["bbox"][0] for item in selected),
                min(item["bbox"][1] for item in selected),
                max(item["bbox"][2] for item in selected),
                max(item["bbox"][3] for item in selected),
            ]
            formulas.append({
                "id": f"formula-candidate:{len(formulas) + 1:03d}",
                "text": text,
                "label": label_match.group(1) if label_match else None,
                "page": page["number"],
                "block_ids": [item["id"] for item in selected],
                "bbox": bbox,
                "confidence": "high" if label_match else "candidate",
                "char_start": fragment_start if len(selected) == 1 else 0,
                "char_end": fragment_end if len(selected) == 1 else len(" ".join(item["text"].strip() for item in selected).strip()),
            })
            for item in selected:
                if len(selected) > 1 or fragment_start == 0 and fragment_end == len(item["text"].strip()):
                    item["role"] = "formula"
            index = cursor
    return formulas


def _references(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    reference_heading_page = next(
        (page["number"] for page in pages for block in page["blocks"] if _REFERENCE_HEADING_RE.match(" ".join(block["text"].split()))),
        None,
    )
    numbered_pages = [
        page["number"] for page in pages
        if reference_heading_page and page["number"] >= reference_heading_page
        and any(_REFERENCE_START_RE.match(" ".join(block["text"].split())) for block in page["blocks"])
    ]
    last_numbered_page = max(numbered_pages) if numbered_pages else None
    in_references = False
    raw_entries: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for page in pages:
        if in_references and last_numbered_page and page["number"] > last_numbered_page:
            break
        column_bases: dict[str, float] = {}
        for side in ("left", "right"):
            candidates = [
                block["bbox"][0]
                for block in page["blocks"]
                if block.get("role") not in {"header", "footer"}
                and ("left" if (block["bbox"][0] + block["bbox"][2]) / 2 < page["width"] / 2 else "right") == side
            ]
            if candidates:
                column_bases[side] = min(candidates)
        for block in page["blocks"]:
            text = " ".join(block["text"].split())
            if _REFERENCE_HEADING_RE.match(text):
                in_references = True
                block["role"] = "heading"
                continue
            if not in_references:
                continue
            embedded_reference_start = re.search(r"(?:^|\s)\[\d{1,4}\]\s+(?=[A-ZÀ-ÖØ-Þ])", text)
            if block["role"] == "heading" and not embedded_reference_start:
                # Appendices and later major sections end the bibliography.
                in_references = False
                current = None
                continue
            if embedded_reference_start and embedded_reference_start.start() > 0:
                prefix = text[: embedded_reference_start.start()].strip()
                if prefix and current is not None:
                    current["block_ids"].append(block["id"])
                    current["parts"].append(prefix)
                text = text[embedded_reference_start.start() :].strip()
            start = _REFERENCE_START_RE.match(text)
            current_text = " ".join(current["parts"]) if current else ""
            looks_like_author_start = "," in text and re.match(r"^[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'`-]+(?:\s+[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'`-]+)?[, ]", text)
            prior_entry_complete = bool(re.search(r"\b(?:19|20)\d{2}[a-z]?\.", current_text)) and current_text.rstrip().endswith(".")
            prior_ends_at_year = bool(re.search(r"\b(?:19|20)\d{2}[a-z]?\.\s*$", current_text))
            side = "left" if (block["bbox"][0] + block["bbox"][2]) / 2 < page["width"] / 2 else "right"
            hanging_start = block["bbox"][0] <= column_bases.get(side, block["bbox"][0]) + 4.0
            new_unnumbered_entry = prior_entry_complete and not prior_ends_at_year and (hanging_start or looks_like_author_start)
            if start or current is None or new_unnumbered_entry:
                current = {"page": page["number"], "block_ids": [block["id"]], "parts": [text]}
                raw_entries.append(current)
            else:
                current["block_ids"].append(block["id"])
                current["parts"].append(text)
            block["role"] = "reference"
    return [
        {
            "id": f"reference:{index:04d}",
            "raw_reference": " ".join(entry.pop("parts")),
            **entry,
        }
        for index, entry in enumerate(raw_entries, 1)
        if any(entry["parts"])
    ]


def _render_pages(pdf_path: Path, pages: list[dict[str, Any]], output_dir: Path, dpi: int) -> tuple[str | None, list[str]]:
    warnings: list[str] = []
    output_dir.mkdir(parents=True, exist_ok=True)
    executable = shutil.which("pdftoppm")
    if executable:
        prefix = output_dir / "page"
        try:
            subprocess.run([executable, "-png", "-r", str(dpi), str(pdf_path), str(prefix)], check=True, capture_output=True, timeout=300)
            rendered = sorted(output_dir.glob("page-*.png"), key=lambda item: int(re.search(r"(\d+)$", item.stem).group(1)))
            for page, image in zip(pages, rendered):
                page["render_path"] = str(image)
            if len(rendered) != len(pages):
                warnings.append(f"Rendered {len(rendered)} of {len(pages)} pages")
            return "pdftoppm", warnings
        except (OSError, subprocess.SubprocessError, AttributeError) as exc:
            warnings.append(f"pdftoppm rendering failed: {exc}")
    try:
        import pymupdf  # type: ignore
    except ImportError:
        try:
            import fitz as pymupdf  # type: ignore
        except ImportError:
            warnings.append("Page rendering unavailable: install Poppler or PyMuPDF")
            return None, warnings
    try:
        document = pymupdf.open(str(pdf_path))
        zoom = dpi / 72.0
        for page_index, page in enumerate(document):
            target = output_dir / f"page-{page_index + 1}.png"
            page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False).save(str(target))
            pages[page_index]["render_path"] = str(target)
        document.close()
        return "pymupdf_pixmap", warnings
    except Exception as exc:
        warnings.append(f"PyMuPDF rendering fallback failed: {exc}")
        return None, warnings


def _save_images(images: list[dict[str, Any]], output_dir: Path) -> list[dict[str, Any]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    saved: list[dict[str, Any]] = []
    for image in images:
        payload = image.pop("bytes", b"")
        if not payload:
            continue
        extension = re.sub(r"[^a-z0-9]", "", image.get("extension", "bin").lower()) or "bin"
        target = output_dir / f"{image['id'].replace(':', '-')}.{extension}"
        target.write_bytes(payload)
        image["asset_path"] = str(target)
        saved.append(image)
    return saved


def _horizontal_overlap(left: list[float], right: list[float]) -> float:
    overlap = max(0.0, min(left[2], right[2]) - max(left[0], right[0]))
    return overlap / max(1.0, min(left[2] - left[0], right[2] - right[0]))


def _match_assets(figures: list[dict[str, Any]], assets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Match only position-supported embedded images; page-only matches stay reviewable."""

    review: list[dict[str, Any]] = []
    by_page: dict[int, list[dict[str, Any]]] = {}
    for asset in assets:
        by_page.setdefault(asset["page"], []).append(asset)
    for figure in figures:
        candidates = by_page.get(figure["page"], [])
        positioned = [
            item for item in candidates
            if item.get("source_bbox")
            and item["source_bbox"][3] <= figure["bbox"][1] + 12
            and _horizontal_overlap(item["source_bbox"], figure["bbox"]) >= 0.25
        ]
        if len(positioned) == 1:
            figure["asset_id"] = positioned[0]["id"]
            figure["asset_path"] = positioned[0]["asset_path"]
            figure["asset_confidence"] = "high"
            figure["match_status"] = "position_matched_embedded"
        elif len(candidates) == 1:
            review.append({"kind": "figure_asset_ambiguous", "page": figure["page"], "object_id": figure["id"], "candidate_ids": [candidates[0]["id"]], "reason": "The only embedded image on the page has no reliable positional match to the caption."})
        elif not candidates:
            pass
        else:
            review.append({"kind": "figure_asset_ambiguous", "page": figure["page"], "object_id": figure["id"], "candidate_ids": [item["id"] for item in candidates], "reason": "Multiple embedded images occur on the caption page."})
    return review


def _caption_crop_bbox(item: dict[str, Any], page: dict[str, Any], kind: str) -> tuple[list[float] | None, str]:
    """Return a conservative crop in top-left PDF coordinates and its confidence."""

    width, height = float(page["width"]), float(page["height"])
    caption = _bounded_bbox(item["bbox"], width, height)
    caption_block = next((block for block in page["blocks"] if block["id"] in item["caption_block_ids"]), None)
    precision = caption_block.get("coordinate_precision") if caption_block else "estimated"
    confidence = "medium" if precision in {"exact", "text_matrix"} else "low"

    caption_width = caption[2] - caption[0]
    # Do not infer a cross-column figure merely from caption length. Long
    # single-column captions are common; only measured geometry may widen it.
    full_width = caption_width >= width * 0.62
    if full_width:
        x0, x1 = width * 0.045, width * 0.955
    elif (caption[0] + caption[2]) / 2 <= width / 2:
        x0, x1 = width * 0.045, width * 0.495
    else:
        x0, x1 = width * 0.505, width * 0.955

    if kind == "figure":
        y1 = caption[1] - max(2.0, height * 0.005)
        y0 = max(height * 0.035, y1 - height * 0.43)
        obstacles = [
            block["bbox"][3] for block in page["blocks"]
            if block["role"] == "caption"
            and block.get("coordinate_precision") in {"exact", "text_matrix"}
            and block["id"] not in item["caption_block_ids"]
            and block["bbox"][3] < y1
            and _horizontal_overlap(block["bbox"], [x0, y0, x1, y1]) >= 0.25
        ]
        if obstacles:
            y0 = max(y0, max(obstacles) + height * 0.008)
    else:
        # Table captions occur both above and below the ruled table. Build the
        # closest contiguous text cluster on each side and select the side
        # with the smaller caption-to-content gap. This handles venues such as
        # NeurIPS (caption above) as well as papers that place captions below.
        max_gap = max(18.0, height * 0.032)
        usable = [
            block for block in page["blocks"]
            if block["id"] not in item["caption_block_ids"]
            and block["role"] not in {"caption", "header", "footer"}
            and _horizontal_overlap(block["bbox"], [x0, 0.0, x1, height]) >= 0.25
        ]

        def adjacent(direction: str) -> tuple[list[dict[str, Any]], float]:
            if direction == "above":
                candidates = sorted(
                    (block for block in usable if block["bbox"][3] <= caption[1] + 2.0),
                    key=lambda block: (block["bbox"][3], block["bbox"][1]), reverse=True,
                )
                cursor = caption[1]
                gap_of = lambda block, edge: edge - block["bbox"][3]
                next_edge = lambda block: min(cursor, block["bbox"][1])
            else:
                candidates = sorted(
                    (block for block in usable if block["bbox"][1] >= caption[3] - 2.0),
                    key=lambda block: (block["bbox"][1], block["bbox"][3]),
                )
                cursor = caption[3]
                gap_of = lambda block, edge: block["bbox"][1] - edge
                next_edge = lambda block: max(cursor, block["bbox"][3])
            selected: list[dict[str, Any]] = []
            first_gap = float("inf")
            for block in candidates:
                gap = gap_of(block, cursor)
                if gap > max_gap:
                    if selected:
                        break
                    continue
                if not selected:
                    first_gap = max(0.0, gap)
                selected.append(block)
                cursor = next_edge(block)
            return selected, first_gap

        above, above_gap = adjacent("above")
        below, below_gap = adjacent("below")
        if below and below_gap < above_gap:
            y0 = max(caption[3] + 2.0, min(block["bbox"][1] for block in below) - 4.0)
            y1 = min(height * 0.965, max(block["bbox"][3] for block in below) + 4.0)
        else:
            y1 = caption[1] - max(2.0, height * 0.005)
            y0 = max(height * 0.035, (min(block["bbox"][1] for block in above) - 4.0) if above else y1 - height * 0.31)
    minimum_height = height * (0.035 if kind == "table" else 0.06)
    if x1 - x0 < width * 0.15 or y1 - y0 < minimum_height:
        return None, "low"
    return [round(x0, 3), round(y0, 3), round(x1, 3), round(y1, 3)], confidence


def _render_crop(
    pdf_path: Path,
    *,
    page_number: int,
    bbox: list[float],
    dpi: int,
    target: Path,
    rendered_page_path: Path | None = None,
) -> tuple[list[int], str] | None:
    """Render one source-page rectangle without requiring an image library."""

    scale = dpi / 72.0
    pixel_bbox = [max(0, round(value * scale)) for value in bbox]
    pixel_width = pixel_bbox[2] - pixel_bbox[0]
    pixel_height = pixel_bbox[3] - pixel_bbox[1]
    if pixel_width < 8 or pixel_height < 8:
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    if rendered_page_path and rendered_page_path.is_file():
        try:
            from PIL import Image  # type: ignore

            with Image.open(rendered_page_path) as rendered_page:
                right = min(rendered_page.width, pixel_bbox[2])
                bottom = min(rendered_page.height, pixel_bbox[3])
                rendered_page.crop((pixel_bbox[0], pixel_bbox[1], right, bottom)).save(target, format="PNG")
            return pixel_bbox, "rendered_page_caption_crop"
        except ImportError:
            pass
        except Exception:
            target.unlink(missing_ok=True)
        sips = shutil.which("sips")
        if sips:
            try:
                subprocess.run(
                    [sips, "--cropToHeightWidth", str(pixel_height), str(pixel_width), "--cropOffset", str(pixel_bbox[1]),
                     str(pixel_bbox[0]), str(rendered_page_path), "--out", str(target)],
                    check=True,
                    capture_output=True,
                    timeout=30,
                )
                if target.is_file():
                    return pixel_bbox, "rendered_page_caption_crop"
            except (OSError, subprocess.SubprocessError):
                target.unlink(missing_ok=True)
    executable = shutil.which("pdftoppm")
    if executable:
        prefix = target.with_suffix("")
        try:
            subprocess.run(
                [executable, "-f", str(page_number), "-l", str(page_number), "-singlefile", "-png", "-r", str(dpi),
                 "-x", str(pixel_bbox[0]), "-y", str(pixel_bbox[1]), "-W", str(pixel_width), "-H", str(pixel_height),
                 str(pdf_path), str(prefix)],
                check=True,
                capture_output=True,
                timeout=120,
            )
            if target.is_file():
                return pixel_bbox, "pdftoppm_caption_crop"
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        import pymupdf  # type: ignore
    except ImportError:
        try:
            import fitz as pymupdf  # type: ignore
        except ImportError:
            return None
    document = None
    try:
        document = pymupdf.open(str(pdf_path))
        page = document[page_number - 1]
        pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), clip=pymupdf.Rect(*bbox), alpha=False)
        pixmap.save(str(target))
        return pixel_bbox, "pymupdf_caption_crop"
    except Exception:
        return None
    finally:
        if document is not None:
            document.close()


def _crop_validation_error(
    target: Path,
    pixel_bbox: list[int],
    pdf_bbox: list[float],
    item: Mapping[str, Any],
    page: Mapping[str, Any],
    kind: str,
) -> str | None:
    """Reject corrupt, implausible, blank, or caption-overlapping crops."""

    expected_width = pixel_bbox[2] - pixel_bbox[0]
    expected_height = pixel_bbox[3] - pixel_bbox[1]
    if expected_width < 8 or expected_height < 8:
        return "crop dimensions are too small"
    caption_bbox = item.get("bbox") or [0, 0, 0, 0]
    caption_top, caption_bottom = float(caption_bbox[1]), float(caption_bbox[3])
    if len(pdf_bbox) != 4 or not (0 <= pdf_bbox[0] < pdf_bbox[2] <= float(page["width"])):
        return "crop horizontal geometry is outside the source page"
    if not (0 <= pdf_bbox[1] < pdf_bbox[3] <= float(page["height"])):
        return "crop vertical geometry is outside the source page"
    if pdf_bbox[1] < caption_bottom - 1.0 and pdf_bbox[3] > caption_top + 1.0:
        return "crop overlaps the formal caption"
    if not target.is_file() or target.stat().st_size < 32:
        return "crop renderer did not produce a usable image file"
    try:
        header = target.read_bytes()[:24]
    except OSError:
        return "crop image cannot be read"
    if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        return "crop renderer output is not a PNG image"
    actual_width, actual_height = struct.unpack(">II", header[16:24])
    tolerance_w = max(2, round(expected_width * 0.03))
    tolerance_h = max(2, round(expected_height * 0.03))
    if abs(actual_width - expected_width) > tolerance_w or abs(actual_height - expected_height) > tolerance_h:
        return "crop image dimensions do not match the requested source region"
    try:
        from PIL import Image  # type: ignore

        with Image.open(target) as image:
            image.load()
            sample = image.convert("L")
            sample.thumbnail((96, 96))
            extrema = sample.getextrema()
            if not extrema or extrema[1] - extrema[0] < 3:
                return f"{kind} crop is visually blank or uniform"
    except ImportError:
        return "crop visual validation requires Pillow"
    except Exception:
        return "crop image decoder rejected the rendered file"
    return None


def _create_caption_crops(
    pdf_path: Path,
    pages: list[dict[str, Any]],
    figures: list[dict[str, Any]],
    tables: list[dict[str, Any]],
    output_dir: Path,
    dpi: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    page_by_number = {page["number"]: page for page in pages}
    assets: list[dict[str, Any]] = []
    review: list[dict[str, Any]] = []
    for kind, items in (("figure", figures), ("table", tables)):
        for item in items:
            if item.get("asset_path"):
                continue
            page = page_by_number[item["page"]]
            bbox, confidence = _caption_crop_bbox(item, page, kind)
            if bbox is None or not page.get("render_path"):
                review.append({"kind": f"{kind}_crop_unavailable", "page": item["page"], "object_id": item["id"], "reason": "Caption geometry or a rendered source page was unavailable."})
                continue
            asset_id = f"crop:{item['id']}"
            target = output_dir / f"{asset_id.replace(':', '-')}.png"
            rendered = _render_crop(
                pdf_path,
                page_number=item["page"],
                bbox=bbox,
                dpi=dpi,
                target=target,
                rendered_page_path=Path(page["render_path"]),
            )
            if rendered is None:
                review.append({"kind": f"{kind}_crop_unavailable", "page": item["page"], "object_id": item["id"], "reason": "No crop renderer was available."})
                continue
            pixel_bbox, method = rendered
            validation_error = _crop_validation_error(target, pixel_bbox, bbox, item, page, kind)
            if validation_error:
                target.unlink(missing_ok=True)
                item["asset_id"] = None
                item["asset_path"] = None
                item["asset_confidence"] = "rejected"
                item["asset_validation"] = "failed"
                item["match_status"] = "caption_crop_rejected"
                review.append({
                    "kind": f"{kind}_crop_rejected",
                    "page": item["page"],
                    "object_id": item["id"],
                    "crop_bbox_pdf": bbox,
                    "confidence": "rejected",
                    "fallback": "caption_and_pdf_page_only",
                    "reason": f"Rejected inferred crop: {validation_error}. The page will fall back to caption/PDF traceability without displaying this asset.",
                })
                continue
            asset = {
                "id": asset_id,
                "page": item["page"],
                "source_page": item["page"],
                "extension": "png",
                "asset_path": str(target),
                "source_bbox": bbox,
                "crop_bbox_pdf": bbox,
                "crop_bbox_pixels": pixel_bbox,
                "extraction_method": method,
                "confidence": confidence,
                "validation": "passed",
                "provenance_kind": "paper_original_page_crop",
            }
            assets.append(asset)
            item["asset_id"] = asset_id
            item["asset_path"] = str(target)
            item["asset_confidence"] = confidence
            item["asset_validation"] = "passed"
            item["match_status"] = "caption_crop_candidate"
            review.append({
                "kind": f"{kind}_crop_review",
                "page": item["page"],
                "object_id": item["id"],
                "candidate_ids": [asset_id],
                "crop_bbox_pdf": bbox,
                "confidence": confidence,
                "reason": "The asset is a source-page crop inferred from caption geometry; verify boundaries before publishing.",
            })
    return assets, review


def _parse_ingested(ingested: IngestedPDF, parsed_dir: Path, options: ParseOptions) -> dict[str, Any]:
    warnings: list[str] = []
    try:
        pages, raw_images, backend_warnings = _extract_with_pymupdf(ingested.local_pdf)
        backend = "pymupdf"
    except ParseError as primary_error:
        pages, raw_images, backend_warnings = _extract_with_pypdf(ingested.local_pdf)
        backend = "pypdf_fallback"
        warnings.append(str(primary_error))
    warnings.extend(backend_warnings)
    if not pages:
        raise ParseError("PDF has no pages")
    if not any(block["text"].strip() for page in pages for block in page["blocks"]):
        raise ParseError("PDF contains no extractable text; OCR or human review is required")

    _mark_repeated_margins(pages)
    sections = _sections(pages)
    figures, tables = _captions(pages)
    formulas = _formula_candidates(pages)
    references = _references(pages)
    render_backend = None
    if options.render_pages:
        render_backend, render_warnings = _render_pages(ingested.local_pdf, pages, parsed_dir / "pages", options.render_dpi)
        warnings.extend(render_warnings)
    assets = _save_images(raw_images, parsed_dir / "figures") if options.extract_images else []
    review_items = _match_assets(figures, assets) if options.extract_images else []
    if options.extract_images and options.render_pages:
        crop_assets, crop_reviews = _create_caption_crops(
            ingested.local_pdf,
            pages,
            figures,
            tables,
            parsed_dir / "figures",
            options.render_dpi,
        )
        assets.extend(crop_assets)
        review_items.extend(crop_reviews)
    elif not options.extract_images:
        review_items.append({"kind": "image_extraction_disabled", "reason": "Image extraction was disabled; visual assets are incomplete."})
    elif not options.render_pages:
        review_items.append({"kind": "caption_crop_disabled", "reason": "Page rendering was disabled, so vector figure/table crops could not be produced."})
    if not sections:
        review_items.append({"kind": "section_detection", "reason": "No reliable section headings were detected."})
    if backend == "pypdf_fallback":
        review_items.append({"kind": "coordinate_precision", "reason": "Block coordinates are estimated; verify locators before publishing Evidence."})
    if options.render_pages and render_backend is None:
        review_items.append({"kind": "page_rendering", "reason": "No page renderer was available."})

    return {
        "schema_version": "1.0.0",
        "stage_version": STAGE_VERSION,
        "input_hashes": [ingested.sha256],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "attempt": options.attempt,
        "status": "needs_review" if review_items else "passed",
        "source": ingested.to_source_dict(page_count=len(pages)),
        "parser": {
            "text_backend": backend,
            "render_backend": render_backend,
            "coordinate_system": "PDF points, origin at top-left",
            "render_dpi": options.render_dpi if options.render_pages else None,
        },
        "pages": pages,
        "sections": sections,
        "figures": figures,
        "tables": tables,
        "formula_candidates": formulas,
        "references": references,
        "image_assets": assets,
        "warnings": warnings,
        "review_items": review_items,
    }


def _rebase_cached_result(result: dict[str, Any], ingested: IngestedPDF, artifact_dir: Path) -> dict[str, Any]:
    """Point a cached, content-only parse at the current artifact directory."""

    result["source"] = ingested.to_source_dict(page_count=len(result.get("pages", [])))
    parsed_dir = artifact_dir / "parsed"
    for page in result.get("pages", []):
        if page.get("render_path"):
            page["render_path"] = str(parsed_dir / "pages" / Path(page["render_path"]).name)
    assets_by_id: dict[str, str] = {}
    for asset in result.get("image_assets", []):
        if asset.get("asset_path"):
            asset["asset_path"] = str(parsed_dir / "figures" / Path(asset["asset_path"]).name)
            assets_by_id[asset["id"]] = asset["asset_path"]
    for figure in result.get("figures", []):
        figure["asset_path"] = assets_by_id.get(figure.get("asset_id"))
    for table in result.get("tables", []):
        table["asset_path"] = assets_by_id.get(table.get("asset_id"))
    return result


def parse_source(
    source: str | Path,
    *,
    artifact_dir: Path,
    cache_dir: Path,
    options: ParseOptions | None = None,
    max_download_mb: int = 80,
    timeout_seconds: int = 45,
) -> dict[str, Any]:
    """Ingest and parse a paper, restoring a content-addressed result when possible."""

    options = options or ParseOptions()
    artifact_dir = artifact_dir.resolve()
    parsed_dir = artifact_dir / "parsed"
    ingested = ingest_pdf(
        source,
        artifact_dir=artifact_dir,
        cache_dir=cache_dir,
        max_download_mb=max_download_mb,
        timeout_seconds=timeout_seconds,
    )
    option_key = f"render-{int(options.render_pages)}_images-{int(options.extract_images)}_dpi-{options.render_dpi}"
    cache_stage = cache_dir.resolve() / ingested.sha256 / "parse" / STAGE_VERSION / option_key
    cached_json = cache_stage / "parsed_document.json"
    output_json = parsed_dir / "parsed_document.json"
    if cached_json.is_file() and not options.force:
        if cache_stage.resolve() != parsed_dir.resolve():
            shutil.copytree(cache_stage, parsed_dir, dirs_exist_ok=True)
        result = json.loads(output_json.read_text(encoding="utf-8"))
        result = _rebase_cached_result(result, ingested, artifact_dir)
        result["cache_hit"] = True
        atomic_write_json(output_json, result)
        return result

    parsed_dir.mkdir(parents=True, exist_ok=True)
    result = _parse_ingested(ingested, parsed_dir, options)
    result["cache_hit"] = False
    atomic_write_json(output_json, result)
    cache_stage.mkdir(parents=True, exist_ok=True)
    if cache_stage.resolve() != parsed_dir.resolve():
        shutil.copytree(parsed_dir, cache_stage, dirs_exist_ok=True)
    return result
