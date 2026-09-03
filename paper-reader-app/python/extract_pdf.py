#!/usr/bin/env python3
"""Extract sentence-level PDF blocks with stable page coordinates.

The extractor is deliberately independent from the existing Paper Visualizer
pipeline.  It emits a compact JSON document consumed by the Electron reader.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import io
import json
import math
import os
import re
import statistics
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import pymupdf


SENTENCE_END = re.compile(r"(?<=[.!?。！？])(?:[\"'”’）\]]*)\s+")
MATH_FONT = re.compile(r"math|symbol|cmmi|cmsy|cmex|msam|msbm|stix", re.I)
MATH_CHAR = re.compile(r"[=∑∏∫√∞≈≠≤≥±×÷∂∇∈∉⊂⊆∪∩→←↔∀∃λμσθβαγδφψω^_{}|]")
BOLD_FONT = re.compile(r"bold|black|semibold|demi", re.I)
TABLE_CAPTION = re.compile(r"^\s*(?:table|tab\.)\s*(?:[A-Z]?\d+|[IVXLC]+)\b", re.I)
TABLE_DISCOVERY_BATCH = 6


@dataclass(frozen=True)
class Line:
    text: str
    bbox: tuple[float, float, float, float]
    size: float
    font: str
    math_ratio: float = 0.0


def union_bbox(boxes: Iterable[Iterable[float]]) -> tuple[float, float, float, float]:
    boxes = [tuple(box) for box in boxes]
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def normalize_bbox(bbox: Iterable[float], width: float, height: float) -> dict[str, float]:
    x0, y0, x1, y1 = bbox
    padding = 1.5
    x0 = max(0.0, x0 - padding)
    y0 = max(0.0, y0 - padding)
    x1 = min(width, x1 + padding)
    y1 = min(height, y1 + padding)
    return {
        "x": round(x0 / width, 6),
        "y": round(y0 / height, 6),
        "width": round(max(1.0, x1 - x0) / width, 6),
        "height": round(max(1.0, y1 - y0) / height, 6),
    }


def split_sentences(lines: list[Line]) -> list[tuple[str, tuple[float, float, float, float], list[Line]]]:
    """Split a PDF paragraph while retaining the union of contributing line boxes."""
    if not lines:
        return []
    joined = ""
    ranges: list[tuple[int, int, Line]] = []
    for line in lines:
        text = line.text.strip()
        if not text:
            continue
        if joined and not joined.endswith(("-", "—", "/")):
            joined += " "
        elif joined.endswith("-") and text[:1].islower():
            joined = joined[:-1]
        start = len(joined)
        joined += text
        ranges.append((start, len(joined), line))
    if not joined:
        return []
    boundaries = [0]
    boundaries.extend(match.end() for match in SENTENCE_END.finditer(joined))
    boundaries.append(len(joined))
    results = []
    for start, end in zip(boundaries, boundaries[1:]):
        text = joined[start:end].strip()
        if not text:
            continue
        contributors = [line for line_start, line_end, line in ranges if line_end > start and line_start < end]
        if contributors:
            results.append((text, union_bbox(line.bbox for line in contributors), contributors))
    return results


def line_is_equation(line: Line, page_width: float) -> bool:
    text = line.text.strip()
    if not text or len(text) > 260:
        return False
    math_chars = len(MATH_CHAR.findall(text))
    visible = max(1, len(re.sub(r"\s", "", text)))
    line_width = line.bbox[2] - line.bbox[0]
    centered = abs(((line.bbox[0] + line.bbox[2]) / 2) - page_width / 2) < page_width * 0.12
    prose_words = len(re.findall(r"[A-Za-z]{3,}", text))
    numbered = bool(re.search(r"\(\s*\d+[a-z]?\s*\)\s*$", text))
    standalone = centered and line_width < page_width * 0.72
    return (
        (standalone and math_chars >= 1 and line.math_ratio >= 0.12 and prose_words <= 6)
        or (math_chars >= 2 and line.math_ratio >= 0.55 and prose_words <= 3)
        or (numbered and math_chars >= 1 and prose_words <= 6)
    )


def overlap_ratio(first: Iterable[float], second: Iterable[float]) -> float:
    ax0, ay0, ax1, ay1 = first
    bx0, by0, bx1, by1 = second
    width = max(0.0, min(ax1, bx1) - max(ax0, bx0))
    height = max(0.0, min(ay1, by1) - max(ay0, by0))
    intersection = width * height
    area = max(1.0, (ax1 - ax0) * (ay1 - ay0))
    return intersection / area


def merge_text_continuations(blocks: list[dict[str, Any]], width: float, height: float) -> list[dict[str, Any]]:
    """Join sentence fragments split into adjacent PDF text blocks."""
    merged: list[dict[str, Any]] = []
    sentence_closed = re.compile(r"[.!?。！？][\"'”’）\]]*$")
    for current in blocks:
        if not merged or current["type"] != "text" or merged[-1]["type"] != "text":
            merged.append(current)
            continue
        previous = merged[-1]
        previous_box = previous["source_bbox"]
        current_box = current["source_bbox"]
        previous_height = max(1.0, previous_box[3] - previous_box[1])
        current_height = max(1.0, current_box[3] - current_box[1])
        vertical_gap = current_box[1] - previous_box[3]
        previous_center = (previous_box[0] + previous_box[2]) / 2
        current_center = (current_box[0] + current_box[2]) / 2
        follows_line = -1.0 <= vertical_gap <= max(previous_height, current_height) * 1.25
        same_column = abs(previous_center - current_center) <= width * 0.18
        if not sentence_closed.search(previous.get("text", "").strip()) and follows_line and same_column:
            previous_rects = previous.get("rects", [previous["bbox"]])
            current_rects = current.get("rects", [current["bbox"]])
            previous["text"] = f"{previous['text'].rstrip('-')}{' ' if not previous['text'].endswith('-') else ''}{current['text']}"
            combined = union_bbox([previous_box, current_box])
            previous["source_bbox"] = [round(value, 3) for value in combined]
            previous["bbox"] = normalize_bbox(combined, width, height)
            previous["rects"] = [*previous_rects, *current_rects]
        else:
            merged.append(current)
    return merged


def image_data_url(image: bytes, extension: str) -> str:
    extension = extension.lower().replace("jpg", "jpeg")
    return f"data:image/{extension};base64,{base64.b64encode(image).decode('ascii')}"


def raw_block_text(raw_block: dict[str, Any]) -> str:
    return " ".join(
        "".join(span.get("text", "") for span in line.get("spans", [])).strip()
        for line in raw_block.get("lines", [])
    ).strip()


def horizontal_overlap(first: Iterable[float], second: Iterable[float]) -> float:
    ax0, _, ax1, _ = first
    bx0, _, bx1, _ = second
    overlap = max(0.0, min(ax1, bx1) - max(ax0, bx0))
    return overlap / max(1.0, min(ax1 - ax0, bx1 - bx0))


def rect_area(rect: Iterable[float]) -> float:
    x0, y0, x1, y1 = rect
    return max(0.0, x1 - x0) * max(0.0, y1 - y0)


def center_inside(inner: Iterable[float], outer: Iterable[float]) -> bool:
    ix0, iy0, ix1, iy1 = inner
    ox0, oy0, ox1, oy1 = outer
    cx, cy = (ix0 + ix1) / 2, (iy0 + iy1) / 2
    return ox0 <= cx <= ox1 and oy0 <= cy <= oy1


def merge_regions(regions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for region in sorted(regions, key=lambda item: (item["bbox"][1], item["bbox"][0])):
        match = next((item for item in merged if item["type"] == region["type"] and (
            overlap_ratio(region["bbox"], item["bbox"]) > 0.25
            or overlap_ratio(item["bbox"], region["bbox"]) > 0.25
        )), None)
        if match:
            match["bbox"] = union_bbox([match["bbox"], region["bbox"]])
            if region.get("label") and not match.get("label"):
                match["label"] = region["label"]
            if region.get("rows"):
                match["rows"] = region["rows"]
        else:
            merged.append(dict(region))
    return merged


def is_margin_text(bbox: Iterable[float], text: str, width: float, height: float) -> bool:
    x0, y0, x1, y1 = bbox
    vertical_sidebar = (x1 < width * 0.10 or x0 > width * 0.90) and (y1 - y0) > (x1 - x0) * 2
    header_or_footer = y1 < height * 0.055 or y0 > height * 0.93
    arxiv_sidebar = bool(re.search(r"arXiv:\s*\d{4}\.\d+", text, re.I)) and x1 < width * 0.14
    return vertical_sidebar or header_or_footer or arxiv_sidebar


def extract_lines(raw_block: dict[str, Any]) -> list[Line]:
    lines = []
    for raw_line in raw_block.get("lines", []):
        spans = raw_line.get("spans", [])
        text = "".join(span.get("text", "") for span in spans).strip()
        if not text:
            continue
        sizes = [float(span.get("size", 0)) for span in spans if span.get("text", "").strip()]
        font = " ".join(str(span.get("font", "")) for span in spans)
        visible_chars = sum(len(re.sub(r"\s", "", span.get("text", ""))) for span in spans)
        math_chars = sum(
            len(re.sub(r"\s", "", span.get("text", "")))
            for span in spans
            if MATH_FONT.search(str(span.get("font", "")))
        )
        math_ratio = math_chars / max(1, visible_chars)
        lines.append(Line(text, tuple(raw_line["bbox"]), statistics.median(sizes) if sizes else 0.0, font, math_ratio))
    return lines


def extract_tables(page: pymupdf.Page) -> list[dict[str, Any]]:
    results = []
    try:
        # PyMuPDF prints an optional layout-package hint to stdout. Keep the
        # command's stdout reserved for its JSON protocol.
        with contextlib.redirect_stdout(io.StringIO()):
            finder = page.find_tables()
    except Exception:
        return results
    for table in finder.tables:
        rows = table.extract()
        if not rows or not any(any((cell or "").strip() for cell in row) for row in rows):
            continue
        # Extremely wide "tables" are usually paragraph glyphs accidentally
        # segmented into columns by the PDF geometry detector.
        if max((len(row) for row in rows), default=0) > 24:
            continue
        results.append({"bbox": tuple(table.bbox), "rows": [[cell or "" for cell in row] for row in rows]})
    return results


def render_region(page: pymupdf.Page, bbox: Iterable[float]) -> str:
    rect = pymupdf.Rect(bbox)
    rect.intersect(page.rect)
    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(1.6, 1.6), clip=rect, alpha=False)
    return image_data_url(pixmap.tobytes("png"), "png")


def render_page(page: pymupdf.Page, scale: float = 1.0) -> str:
    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
    return image_data_url(pixmap.tobytes("png"), "png")


def detect_visual_regions(
    page: pymupdf.Page,
    raw_blocks: list[dict[str, Any]],
    tables: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Find complete figures/tables before emitting their internal PDF text."""
    width, height = page.rect.width, page.rect.height
    page_area = width * height
    text_blocks = [
        {"bbox": tuple(block["bbox"]), "text": raw_block_text(block)}
        for block in raw_blocks
        if block.get("type") == 0 and raw_block_text(block)
    ]
    image_boxes = [tuple(block["bbox"]) for block in raw_blocks if block.get("type") == 1]
    figure_captions = [item for item in text_blocks if re.match(r"^(Figure|Fig\.)\s*\d+\s*[:.]", item["text"], re.I)]
    table_captions = [item for item in text_blocks if re.match(r"^Table\s*\d+\s*[:.]", item["text"], re.I)]

    def extend_caption(caption: dict[str, Any]) -> None:
        while not re.search(r"[.!?。！？][\"'”’）\]]*$", caption["text"]):
            candidates = [
                item for item in text_blocks
                if item is not caption
                and 0 <= item["bbox"][1] - caption["bbox"][3] <= 32
                and horizontal_overlap(item["bbox"], caption["bbox"]) > 0.55
                and not re.match(r"^(Figure|Fig\.|Table)\s*\d+\s*[:.]", item["text"], re.I)
            ]
            if not candidates:
                break
            continuation = min(candidates, key=lambda item: item["bbox"][1] - caption["bbox"][3])
            spacer = "" if caption["text"].endswith("-") else " "
            caption["text"] = f"{caption['text'].rstrip('-')}{spacer}{continuation['text']}"
            caption["bbox"] = union_bbox([caption["bbox"], continuation["bbox"]])

    for caption in [*figure_captions, *table_captions]:
        extend_caption(caption)
    regions: list[dict[str, Any]] = [
        {"type": "table", "bbox": tuple(table["bbox"]), "label": "表格", "rows": table.get("rows", [])}
        for table in tables
    ]

    try:
        drawing_regions = page.cluster_drawings()
    except Exception:
        drawing_regions = []
    for drawing in drawing_regions:
        bbox = tuple(drawing)
        area = rect_area(bbox)
        if drawing.width < 60 or drawing.height < 42 or area > page_area * 0.72:
            continue
        if any(overlap_ratio(bbox, item["bbox"]) > 0.35 for item in regions):
            continue
        contained_images = sum(center_inside(image_box, bbox) for image_box in image_boxes)
        contained_text = " ".join(item["text"] for item in text_blocks if center_inside(item["bbox"], bbox))
        nearby_caption = any(
            horizontal_overlap(bbox, caption["bbox"]) > 0.35
            and -12 <= caption["bbox"][1] - bbox[3] <= 90
            for caption in figure_captions
        )
        if len(contained_text) > 160 and contained_images == 0 and not nearby_caption:
            continue
        regions.append({"type": "figure", "bbox": bbox, "label": "论文图片"})

    for image_box in image_boxes:
        if any(center_inside(image_box, region["bbox"]) for region in regions):
            continue
        if rect_area(image_box) < page_area * 0.004 or (image_box[2] - image_box[0]) < 35 or (image_box[3] - image_box[1]) < 28:
            continue
        regions.append({"type": "figure", "bbox": image_box, "label": "论文图片"})

    regions = merge_regions(regions)

    # Expand figure regions through their captions. This captures selectable
    # labels and annotations as pixels in one coherent visual block.
    for caption in figure_captions:
        candidates = [
            region for region in regions
            if region["type"] == "figure"
            and horizontal_overlap(region["bbox"], caption["bbox"]) > 0.35
            and -12 <= caption["bbox"][1] - region["bbox"][3] <= 95
        ]
        if candidates:
            region = min(candidates, key=lambda item: abs(caption["bbox"][1] - item["bbox"][3]))
            region["bbox"] = union_bbox([region["bbox"], caption["bbox"]])
            region["label"] = caption["text"]

    # Borderless tables frequently escape find_tables(). Their caption is a
    # reliable anchor: absorb the dense numeric/text rows immediately beside it.
    for caption in table_captions:
        if any(center_inside(caption["bbox"], region["bbox"]) for region in regions if region["type"] == "table"):
            continue
        cap = caption["bbox"]
        above = [
            item for item in text_blocks
            if item is not caption
            and cap[1] - 80 <= item["bbox"][1] < cap[1]
            and item["bbox"][3] <= cap[1] + 2
            and horizontal_overlap(item["bbox"], cap) > 0.35
        ]
        below = [
            item for item in text_blocks
            if item is not caption
            and cap[3] - 2 <= item["bbox"][1] <= cap[3] + 120
            and horizontal_overlap(item["bbox"], cap) > 0.35
        ]

        def table_score(items: list[dict[str, Any]]) -> tuple[int, int]:
            text = " ".join(item["text"] for item in items)
            return (sum(character.isdigit() for character in text), len(items))

        nearby = max((above, below), key=table_score)
        if nearby and table_score(nearby)[0] >= 3:
            regions.append({
                "type": "table",
                "bbox": union_bbox([cap, *(item["bbox"] for item in nearby)]),
                "label": caption["text"],
                "rows": [],
            })

    regions = merge_regions(regions)
    table_regions = [region for region in regions if region["type"] == "table"]
    regions = [
        region for region in regions
        if region["type"] == "table"
        or not any(overlap_ratio(region["bbox"], table["bbox"]) > 0.45 for table in table_regions)
    ]
    return regions


def reading_order(blocks: list[dict[str, Any]], width: float) -> list[dict[str, Any]]:
    """Order blocks by page regions, then complete each column top-to-bottom."""
    if not blocks:
        return []
    midpoint = width / 2
    gutter = width * 0.035
    spanning = [
        block for block in blocks
        if (block["source_bbox"][2] - block["source_bbox"][0]) >= width * 0.68
        or (
            block["source_bbox"][0] < midpoint - width * 0.18
            and block["source_bbox"][2] > midpoint + width * 0.18
        )
    ]
    normal = [block for block in blocks if block not in spanning]

    def order_band(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        left = [item for item in items if item["source_bbox"][2] <= midpoint + gutter]
        right = [item for item in items if item["source_bbox"][0] >= midpoint - gutter]
        middle = [item for item in items if item not in left and item not in right]
        key = lambda item: (item["source_bbox"][1], item["source_bbox"][0])
        if len(left) >= 2 and len(right) >= 2:
            return sorted(left, key=key) + sorted(right, key=key) + sorted(middle, key=key)
        return sorted(items, key=key)

    ordered: list[dict[str, Any]] = []
    remaining = list(normal)
    for wide in sorted(spanning, key=lambda item: (item["source_bbox"][1], item["source_bbox"][0])):
        before = [item for item in remaining if item["source_bbox"][1] < wide["source_bbox"][1]]
        ordered.extend(order_band(before))
        before_ids = {id(item) for item in before}
        remaining = [item for item in remaining if id(item) not in before_ids]
        ordered.append(wide)
    ordered.extend(order_band(remaining))
    return ordered


def consolidate_equations(
    page: pymupdf.Page,
    blocks: list[dict[str, Any]],
    width: float,
    height: float,
) -> list[dict[str, Any]]:
    """Fold same-baseline PDF fragments into one display-equation image."""
    consumed: set[str] = set()
    output: list[dict[str, Any]] = []
    for equation in [block for block in blocks if block["type"] == "equation"]:
        if equation["id"] in consumed:
            continue
        members = [equation]
        changed = True
        while changed:
            changed = False
            group_box = union_bbox(member["source_bbox"] for member in members)
            for candidate in blocks:
                if candidate["id"] in consumed or candidate in members or candidate["type"] not in {"text", "equation"}:
                    continue
                if candidate["type"] == "text" and len(candidate.get("text", "")) > 55:
                    continue
                box = candidate["source_bbox"]
                vertical_overlap = max(0.0, min(group_box[3], box[3]) - max(group_box[1], box[1]))
                min_height = max(1.0, min(group_box[3] - group_box[1], box[3] - box[1]))
                horizontal_gap = max(0.0, max(group_box[0], box[0]) - min(group_box[2], box[2]))
                if vertical_overlap / min_height >= 0.35 and horizontal_gap <= width * 0.04:
                    members.append(candidate)
                    changed = True
        for member in members:
            consumed.add(member["id"])
        group_box = union_bbox(member["source_bbox"] for member in members)
        ordered_text = " ".join(member.get("text", "") for member in sorted(members, key=lambda item: item["source_bbox"][0]))
        equation["text"] = ordered_text
        equation["source_bbox"] = [round(value, 3) for value in group_box]
        equation["bbox"] = normalize_bbox(group_box, width, height)
        if len(re.findall(r"[A-Za-z]{3,}", ordered_text)) >= 5:
            equation["type"] = "text"
            equation["rects"] = [equation["bbox"]]
            equation.pop("image", None)
        else:
            equation["image"] = render_region(page, group_box)
        output.append(equation)
    output.extend(block for block in blocks if block["id"] not in consumed)
    return output


def page_blocks(page: pymupdf.Page, page_number: int, counter: list[int]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    width, height = page.rect.width, page.rect.height
    raw = page.get_text("dict", sort=True)
    raw_blocks = raw.get("blocks", [])
    raw_text_blocks = [
        block for block in raw_blocks
        if block.get("type") == 0
        and not is_margin_text(tuple(block["bbox"]), raw_block_text(block), width, height)
    ]
    all_sizes = [line.size for block in raw_text_blocks for line in extract_lines(block) if line.size > 0]
    body_size = statistics.median(all_sizes) if all_sizes else 10.0
    tables = extract_tables(page)
    visual_regions = detect_visual_regions(page, raw_blocks, tables)
    blocks: list[dict[str, Any]] = []
    heading_candidates: list[dict[str, Any]] = []

    def add_block(kind: str, bbox: Iterable[float], **payload: Any) -> dict[str, Any]:
        counter[0] += 1
        block = {
            "id": f"block-{counter[0]:05d}",
            "type": kind,
            "page": page_number,
            "bbox": normalize_bbox(bbox, width, height),
            "source_bbox": [round(float(value), 3) for value in bbox],
            **payload,
        }
        blocks.append(block)
        return block

    for region in visual_regions:
        rows = region.get("rows", [])
        flat = " | ".join(cell for row in rows for cell in row if cell).strip()
        add_block(
            region["type"],
            region["bbox"],
            text=flat[:500],
            label=region.get("label") or ("表格" if region["type"] == "table" else "论文图片"),
            rows=rows,
            image=render_region(page, region["bbox"]),
        )

    for raw_block in raw_text_blocks:
        lines = extract_lines(raw_block)
        if not lines:
            continue
        bbox = tuple(raw_block["bbox"])
        if any(center_inside(bbox, region["bbox"]) or overlap_ratio(bbox, region["bbox"]) > 0.35 for region in visual_regions):
            continue
        average_size = statistics.mean(line.size for line in lines if line.size > 0) if any(line.size > 0 for line in lines) else body_size
        joined = " ".join(line.text for line in lines)
        bold = sum(bool(BOLD_FONT.search(line.font)) for line in lines) >= max(1, len(lines) / 2)
        horizontal = (bbox[2] - bbox[0]) > (bbox[3] - bbox[1]) * 1.5
        numbered_heading = bool(re.match(r"^\d+(?:\.\d+)*\s+[A-Z]", joined))
        short_heading = len(joined) < 70 and not re.search(r"[.!?。！？]$", joined)
        is_heading = horizontal and len(lines) <= 2 and (
            numbered_heading
            or (average_size >= body_size * 1.5 and len(joined) < 130)
            or (bold and short_heading and average_size >= body_size * 1.03)
        )
        if is_heading:
            block = add_block("heading", bbox, text=joined)
            heading_candidates.append(block)
            continue
        paragraph_like = (bbox[2] - bbox[0]) > width * 0.52 and len(re.findall(r"[A-Za-z]{3,}", joined)) > 8
        groups: list[tuple[str, list[Line]]] = []
        for line in lines:
            kind = "equation" if not paragraph_like and line_is_equation(line, width) else "text"
            if groups and groups[-1][0] == kind:
                groups[-1][1].append(line)
            else:
                groups.append((kind, [line]))
        for kind, group_lines in groups:
            if kind == "equation":
                equation_bbox = union_bbox(line.bbox for line in group_lines)
                add_block(
                    "equation",
                    equation_bbox,
                    text=" ".join(line.text for line in group_lines),
                    image=render_region(page, equation_bbox),
                )
                continue
            for text, sentence_bbox, contributors in split_sentences(group_lines):
                add_block(
                    "text",
                    sentence_bbox,
                    text=text,
                    rects=[normalize_bbox(line.bbox, width, height) for line in contributors],
                )

    blocks = consolidate_equations(page, blocks, width, height)
    blocks = reading_order(blocks, width)
    blocks = merge_text_continuations(blocks, width, height)
    for order, block in enumerate(blocks):
        block["reading_order"] = order
    return blocks, heading_candidates


def document_title(doc: pymupdf.Document, pdf_path: Path, blocks: list[dict[str, Any]]) -> str:
    metadata_title = (doc.metadata or {}).get("title", "").strip()
    if metadata_title and metadata_title.lower() not in {"untitled", "unknown"}:
        return metadata_title
    first_heading = next((block["text"] for block in blocks if block["type"] == "heading" and block["page"] == 1), "")
    return first_heading or pdf_path.stem.replace("-", " ").title()


def build_outline(doc: pymupdf.Document, headings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    toc = doc.get_toc(simple=True)
    if toc:
        result = []
        for level, title, page in toc:
            candidates = [block for block in headings if block["page"] == page]
            result.append({
                "level": level,
                "title": title,
                "page": page,
                "block_id": candidates[0]["id"] if candidates else None,
            })
        return result
    return [
        {"level": 1, "title": block["text"], "page": block["page"], "block_id": block["id"]}
        for block in headings
        if len(block.get("text", "")) <= 150
    ][:80]


def response_text(payload: dict[str, Any]) -> str:
    for output in payload.get("output", []):
        for content in output.get("content", []):
            if content.get("type") == "output_text":
                return content.get("text", "")
    return payload.get("output_text", "")


def responses_endpoint(value: str | None = None) -> str:
    base = (value or os.environ.get("PAPER_READER_LLM_ENDPOINT") or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").strip().rstrip("/")
    parsed = urllib.parse.urlsplit(base)
    local_http = parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if (parsed.scheme != "https" and not local_http) or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("LLM Endpoint 必须是有效的 HTTPS URL")
    return base if parsed.path.rstrip("/").endswith("/responses") else f"{base}/responses"


def call_responses_api(
    api_key: str,
    model: str,
    system_prompt: str,
    user_content: list[dict[str, Any]],
    schema_name: str,
    schema: dict[str, Any],
) -> dict[str, Any]:
    body = {
        "model": model,
        "input": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "text": {"format": {"type": "json_schema", "name": schema_name, "strict": True, "schema": schema}},
    }
    request = urllib.request.Request(
        responses_endpoint(),
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=150) as response:
        parsed = json.loads(response.read().decode("utf-8"))
    output = response_text(parsed)
    if not output:
        raise ValueError("LLM 未返回结构化内容")
    return json.loads(output)


def compact_block(block: dict[str, Any], limit: int = 220) -> dict[str, Any]:
    return {
        "id": block["id"],
        "type": block["type"],
        "bbox": block["bbox"],
        "text": (block.get("text") or block.get("label") or "")[:limit],
    }


def page_table_cues(page_blocks: list[dict[str, Any]]) -> list[str]:
    cues: list[str] = []
    if any(block["type"] == "table" for block in page_blocks):
        cues.append("本地解析器已发现表格区域")
    texts = [(block.get("text") or block.get("label") or "").strip() for block in page_blocks]
    if any(TABLE_CAPTION.match(text) for text in texts):
        cues.append("存在 Table/Tab. 表题")
    combined = " ".join(texts)
    digits = sum(character.isdigit() for character in combined)
    short_numeric = sum(
        len(text) <= 100 and len(re.findall(r"\d", text)) >= 2
        for text in texts
        if text
    )
    if digits >= 24 and short_numeric >= 4:
        cues.append("数字密集且存在多行短文本")
    x_buckets: dict[int, int] = {}
    for block in page_blocks:
        source = block.get("source_bbox")
        if source and len((block.get("text") or "")) <= 100:
            bucket = round(float(source[0]) / 18)
            x_buckets[bucket] = x_buckets.get(bucket, 0) + 1
    if sum(count >= 4 for count in x_buckets.values()) >= 3:
        cues.append("多个短文本列具有重复横坐标")
    return cues


def local_table_candidate_pages(blocks: list[dict[str, Any]]) -> set[int]:
    pages = {block["page"] for block in blocks}
    candidates = set()
    for page in pages:
        cues = page_table_cues([block for block in blocks if block["page"] == page])
        if "本地解析器已发现表格区域" in cues or "存在 Table/Tab. 表题" in cues:
            candidates.add(page)
    return candidates


def table_discovery_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "pages": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "page": {"type": "integer"},
                        "has_table": {"type": "boolean"},
                        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                        "reason": {"type": "string"},
                    },
                    "required": ["page", "has_table", "confidence", "reason"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["pages"],
        "additionalProperties": False,
    }


def table_extraction_schema() -> dict[str, Any]:
    bbox = {
        "type": "object",
        "properties": {
            "x": {"type": "number"},
            "y": {"type": "number"},
            "width": {"type": "number"},
            "height": {"type": "number"},
        },
        "required": ["x", "y", "width", "height"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "tables": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "caption": {"type": "string"},
                        "bbox": bbox,
                        "rows": {
                            "type": "array",
                            "items": {"type": "array", "items": {"type": "string"}},
                        },
                        "source_block_ids": {"type": "array", "items": {"type": "string"}},
                        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                        "continues_from_previous": {"type": "boolean"},
                        "continues_to_next": {"type": "boolean"},
                    },
                    "required": [
                        "caption", "bbox", "rows", "source_block_ids", "confidence",
                        "continues_from_previous", "continues_to_next",
                    ],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["tables"],
        "additionalProperties": False,
    }


def discover_table_pages(
    doc: pymupdf.Document,
    blocks: list[dict[str, Any]],
    api_key: str,
    model: str,
) -> tuple[set[int], list[str], int]:
    discovered: set[int] = set()
    errors: list[str] = []
    calls = 0
    page_numbers = list(range(1, len(doc) + 1))
    for start in range(0, len(page_numbers), TABLE_DISCOVERY_BATCH):
        batch = page_numbers[start : start + TABLE_DISCOVERY_BATCH]
        page_records = []
        content: list[dict[str, Any]] = []
        for page_number in batch:
            page_blocks = [block for block in blocks if block["page"] == page_number]
            page_records.append({
                "page": page_number,
                "local_cues": page_table_cues(page_blocks),
                "blocks": [compact_block(block, 180) for block in page_blocks],
            })
        content.append({
            "type": "input_text",
            "text": (
                "判断下面每一页是否包含真正的数据表格。优先保证召回率；排除普通段落、公式和流程图。"
                "每张图片前都有对应页码，图片顺序与 JSON 相同。\n"
                + json.dumps(page_records, ensure_ascii=False)
            ),
        })
        for page_number in batch:
            content.append({"type": "input_text", "text": f"第 {page_number} 页缩略图"})
            content.append({"type": "input_image", "image_url": render_page(doc[page_number - 1], 0.72), "detail": "low"})
        try:
            result = call_responses_api(
                api_key,
                model,
                "你是学术论文表格召回器。结合页面缩略图、文本、坐标和本地提示判断表格页，不解析单元格。",
                content,
                "table_page_discovery",
                table_discovery_schema(),
            )
            calls += 1
            for item in result.get("pages", []):
                page = item.get("page")
                if item.get("has_table") and isinstance(page, int) and page in batch:
                    discovered.add(page)
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError, json.JSONDecodeError) as error:
            errors.append(f"候选页 {batch[0]}-{batch[-1]}：{str(error)[:160]}")
    return discovered, errors, calls


def parse_table_page(
    page: pymupdf.Page,
    page_number: int,
    page_blocks: list[dict[str, Any]],
    api_key: str,
    model: str,
) -> list[dict[str, Any]]:
    block_inventory = [compact_block(block, 360) for block in page_blocks]
    content = [
        {
            "type": "input_text",
            "text": (
                f"解析第 {page_number} 页的所有数据表格。bbox 使用页面归一化坐标 0-1，只框表格主体，不包含表题或正文。"
                "逐字抄录单元格，无法辨认时留空，不要猜测。rows 必须保持视觉行列结构；source_block_ids 只填写表格主体覆盖的块。"
                "不要把图、公式或算法伪代码当成表格。\n页面文本块："
                + json.dumps(block_inventory, ensure_ascii=False)
            ),
        },
        {"type": "input_image", "image_url": render_page(page, 2.0), "detail": "high"},
    ]
    result = call_responses_api(
        api_key,
        model,
        "你是高精度学术表格解析器。视觉结构决定行列，PDF 文本块用于核对字符和建立可追溯关系。",
        content,
        "page_tables",
        table_extraction_schema(),
    )
    tables = result.get("tables", [])
    for table in tables:
        table["page"] = page_number
    return tables


def normalized_table_rows(rows: Any) -> list[list[str]]:
    if not isinstance(rows, list):
        return []
    clean: list[list[str]] = []
    for row in rows[:120]:
        if not isinstance(row, list):
            continue
        cells = [str(cell).strip()[:1000] for cell in row[:50]]
        while cells and not cells[-1]:
            cells.pop()
        if cells and any(cells):
            clean.append(cells)
    if not clean:
        return []
    width = max(len(row) for row in clean)
    if width < 2:
        return []
    return [row + [""] * (width - len(row)) for row in clean]


def source_bbox_from_normalized(bbox: dict[str, Any], width: float, height: float) -> tuple[float, float, float, float] | None:
    try:
        x = min(1.0, max(0.0, float(bbox["x"])))
        y = min(1.0, max(0.0, float(bbox["y"])))
        right = min(1.0, max(x, x + float(bbox["width"])))
        bottom = min(1.0, max(y, y + float(bbox["height"])))
    except (KeyError, TypeError, ValueError):
        return None
    if right - x < 0.04 or bottom - y < 0.015:
        return None
    return (x * width, y * height, right * width, bottom * height)


def apply_visual_tables(
    doc: pymupdf.Document,
    blocks: list[dict[str, Any]],
    detected_tables: list[dict[str, Any]],
) -> dict[str, int]:
    next_id = max(
        (int(match.group(1)) for block in blocks if (match := re.match(r"block-(\d+)$", block["id"]))),
        default=0,
    ) + 1
    added = enhanced = replaced = rejected = 0
    for detected in detected_tables:
        page_number = detected.get("page")
        if not isinstance(page_number, int) or not 1 <= page_number <= len(doc):
            rejected += 1
            continue
        page = doc[page_number - 1]
        rows = normalized_table_rows(detected.get("rows"))
        source_bbox = source_bbox_from_normalized(detected.get("bbox", {}), page.rect.width, page.rect.height)
        if len(rows) < 2 or source_bbox is None:
            rejected += 1
            continue
        existing_tables = [block for block in blocks if block["page"] == page_number and block["type"] == "table"]
        matching = next((
            block for block in existing_tables
            if overlap_ratio(source_bbox, block["source_bbox"]) > 0.45
            or overlap_ratio(block["source_bbox"], source_bbox) > 0.45
        ), None)
        source_ids = set(detected.get("source_block_ids") or [])
        affected = []
        for block in blocks:
            if block["page"] != page_number or block["type"] == "table":
                continue
            if block["type"] == "figure":
                same_visual_region = (
                    overlap_ratio(source_bbox, block["source_bbox"]) > 0.72
                    and overlap_ratio(block["source_bbox"], source_bbox) > 0.52
                )
                if same_visual_region:
                    affected.append(block)
                continue
            text = (block.get("text") or "").strip()
            if TABLE_CAPTION.match(text):
                continue
            geometrically_inside = center_inside(block["source_bbox"], source_bbox) or overlap_ratio(block["source_bbox"], source_bbox) > 0.42
            model_linked = block["id"] in source_ids and overlap_ratio(block["source_bbox"], source_bbox) > 0.18
            if geometrically_inside or model_linked:
                affected.append(block)
        replacement_ids = [block["id"] for block in sorted(affected, key=lambda item: item.get("reading_order", 0))]
        caption = str(detected.get("caption") or "").strip()[:500] or f"第 {page_number} 页表格"
        flat = " | ".join(cell for row in rows for cell in row if cell)
        if matching:
            old_cells = sum(bool(cell) for row in matching.get("rows", []) for cell in row)
            new_cells = sum(bool(cell) for row in rows for cell in row)
            if new_cells >= old_cells:
                matching["rows"] = rows
                matching["text"] = flat[:4000]
            if caption:
                matching["label"] = caption
            matching["extraction_source"] = "local+vision_llm"
            matching["vision_confidence"] = detected.get("confidence", "medium")
            matching["replaces_block_ids"] = sorted(set(matching.get("replaces_block_ids", [])) | set(replacement_ids))
            target = matching
            enhanced += 1
        else:
            block_id = f"block-{next_id:05d}"
            next_id += 1
            target = {
                "id": block_id,
                "type": "table",
                "page": page_number,
                "bbox": normalize_bbox(source_bbox, page.rect.width, page.rect.height),
                "source_bbox": [round(value, 3) for value in source_bbox],
                "text": flat[:4000],
                "label": caption,
                "rows": rows,
                "image": render_region(page, source_bbox),
                "extraction_source": "vision_llm",
                "vision_confidence": detected.get("confidence", "medium"),
                "replaces_block_ids": replacement_ids,
                "continues_from_previous": bool(detected.get("continues_from_previous")),
                "continues_to_next": bool(detected.get("continues_to_next")),
            }
            blocks.append(target)
            added += 1
        for block in affected:
            if block.get("replaced_by") != target["id"]:
                block["replaced_by"] = target["id"]
                replaced += 1

    reordered: list[dict[str, Any]] = []
    for page_number in range(1, len(doc) + 1):
        page_blocks = [block for block in blocks if block["page"] == page_number]
        reordered.extend(reading_order(page_blocks, doc[page_number - 1].rect.width))
    blocks[:] = reordered
    for order, block in enumerate(blocks):
        block["reading_order"] = order
    return {"tables_added": added, "tables_enhanced": enhanced, "blocks_replaced": replaced, "tables_rejected": rejected}


def enhance_with_llm(
    doc: pymupdf.Document,
    blocks: list[dict[str, Any]],
    api_key: str,
    model: str,
) -> dict[str, Any]:
    local_candidates = local_table_candidate_pages(blocks)
    discovered, errors, discovery_calls = discover_table_pages(doc, blocks, api_key, model)
    candidate_pages = sorted(local_candidates | discovered)
    detected_tables: list[dict[str, Any]] = []
    vision_calls = 0
    for page_number in candidate_pages:
        try:
            page_tables = parse_table_page(
                doc[page_number - 1],
                page_number,
                [block for block in blocks if block["page"] == page_number],
                api_key,
                model,
            )
            vision_calls += 1
            detected_tables.extend(page_tables)
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError, json.JSONDecodeError) as error:
            errors.append(f"第 {page_number} 页：{str(error)[:160]}")
    stats = apply_visual_tables(doc, blocks, detected_tables)
    status = "applied" if not errors else ("partial" if discovery_calls or vision_calls else "error")
    return {
        "status": status,
        "strategy": "table_harness_v1",
        "model": model,
        "discovery_calls": discovery_calls,
        "vision_calls": vision_calls,
        "local_candidate_pages": sorted(local_candidates),
        "candidate_pages": candidate_pages,
        "tables_detected": len(detected_tables),
        **stats,
        "errors": errors,
        "message": errors[0] if errors else "",
    }


def extract(pdf_path: Path, use_llm: bool = False) -> dict[str, Any]:
    doc = pymupdf.open(pdf_path)
    blocks: list[dict[str, Any]] = []
    headings: list[dict[str, Any]] = []
    counter = [0]
    page_sizes = []
    for index, page in enumerate(doc):
        page_sizes.append({"width": round(page.rect.width, 3), "height": round(page.rect.height, 3), "rotation": page.rotation})
        page_items, page_headings = page_blocks(page, index + 1, counter)
        blocks.extend(page_items)
        headings.extend(page_headings)
    result = {
        "schema_version": "1.0",
        "source": str(pdf_path.resolve()),
        "title": document_title(doc, pdf_path, blocks),
        "page_count": len(doc),
        "page_sizes": page_sizes,
        "blocks": blocks,
        "outline": build_outline(doc, headings),
        "llm": {"status": "disabled"},
    }
    if use_llm:
        api_key = os.environ.get("PAPER_READER_LLM_KEY") or os.environ.get("OPENAI_API_KEY")
        model = os.environ.get("PAPER_READER_LLM_MODEL", "gpt-5.4-mini")
        if not api_key:
            result["llm"] = {"status": "error", "message": "未提供 API Key"}
        else:
            try:
                result["llm"] = enhance_with_llm(doc, blocks, api_key, model)
            except (urllib.error.URLError, TimeoutError, ValueError, KeyError, json.JSONDecodeError) as error:
                result["llm"] = {"status": "error", "message": str(error)[:240]}
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract aligned content blocks from a scholarly PDF")
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--llm", action="store_true", help="Enhance block classification with an LLM")
    args = parser.parse_args()
    if not args.pdf.is_file():
        parser.error(f"PDF 不存在：{args.pdf}")
    try:
        result = extract(args.pdf, use_llm=args.llm)
    except Exception as error:
        print(f"PDF 解析失败：{error}", file=sys.stderr)
        return 1
    json.dump(result, sys.stdout, ensure_ascii=False, separators=(",", ":"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
