"""Deterministic, evidence-grounded conversion from Parse output to Paper IR.

The default builder is deliberately conservative.  It registers source text as
single-block Evidence, creates extractive claim summaries, and leaves table
``discussed_cells`` empty unless the parser or a human overlay explicitly binds
a cell to Evidence.  A human overlay can upsert IR entities, but Evidence text
and hashes are always reconstructed from immutable page blocks.
"""

from __future__ import annotations

import copy
import difflib
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..artifacts import atomic_write_json
from ..validation import validate_schema


IR_SCHEMA_VERSION = "1.0.0"
STAGE_VERSION = "1.3.3"

_ALLOWED_OVERLAY_KEYS = {
    "approval",
    "paper",
    "evidence",
    "claims",
    "formulas",
    "variables",
    "visuals",
    "tables",
    "citations",
    "relations",
    "review",
}
_CONTENT_ROLES = {"body", "caption", "formula"}
_CLAIM_CUES = re.compile(
    r"\b(?:we\s+(?:propose|present|introduce|show|find|demonstrate|achieve)|"
    r"our\s+(?:method|model|approach|results?)|outperform|improv(?:e|es|ed|ement)|"
    r"significant(?:ly)?|state[- ]of[- ]the[- ]art)\b",
    re.IGNORECASE,
)
_INLINE_CITATION_RE = re.compile(r"\[(\d{1,4}(?:\s*[-,]\s*\d{1,4})*)\]")
_YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
_ARXIV_RE = re.compile(r"(?<!\d)(\d{4}\.\d{4,5})(?:v\d+)?(?!\d)", re.IGNORECASE)
_URL_RE = re.compile(r"https?://[^\s)>\]}]+", re.IGNORECASE)
_DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
_MATH_TOKEN_RE = re.compile(r"[A-Za-z\u0391-\u03a9\u03b1-\u03c9][A-Za-z0-9_\u0391-\u03a9\u03b1-\u03c9]*")
_MATH_FUNCTIONS = {
    "cos", "exp", "ffn", "gelu", "log", "max", "mean", "min", "relu", "sigmoid",
    "sin", "softmax", "sqrt", "sum", "tan", "tanh",
}
_VARIABLE_STOP = {"a", "an", "and", "as", "at", "by", "e", "for", "from", "i", "in", "is", "of", "or", "the", "to", "where", "with"}
_GREEK_LATEX = {
    "α": r"\alpha", "β": r"\beta", "γ": r"\gamma", "δ": r"\delta", "ε": r"\epsilon", "ϵ": r"\epsilon",
    "θ": r"\theta", "λ": r"\lambda", "μ": r"\mu", "π": r"\pi", "ρ": r"\rho", "σ": r"\sigma", "τ": r"\tau", "φ": r"\phi", "ω": r"\omega",
    "Α": r"\Alpha", "Β": r"\Beta", "Γ": r"\Gamma", "Δ": r"\Delta", "Θ": r"\Theta", "Λ": r"\Lambda", "Σ": r"\Sigma", "Φ": r"\Phi", "Ω": r"\Omega",
}


class ModelingError(ValueError):
    """Raised when parsed input or a human overlay violates source fidelity."""


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalize_text(text: object) -> str:
    return " ".join(str(text or "").split())


def _pdf_url(source: Mapping[str, Any], page: int) -> str:
    base = source.get("original_url") or source.get("local_pdf") or ""
    separator = "&" if "#" in str(base) else "#"
    return f"{base}{separator}page={page}"


def _normalize_source(source: Mapping[str, Any], page_count: int) -> dict[str, Any]:
    digest = str(source.get("sha256") or "")
    if not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ModelingError("parsed_document.source.sha256 must be a lowercase SHA-256 digest")
    kind = source.get("kind")
    if kind not in {"local", "url"}:
        raise ModelingError("parsed_document.source.kind must be 'local' or 'url'")
    return {
        "kind": kind,
        "sha256": digest,
        "page_count": page_count,
        "local_pdf": str(source.get("local_pdf") or ""),
        "original_url": source.get("original_url"),
    }


def _normalize_pages(raw_pages: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    seen_pages: set[int] = set()
    seen_blocks: set[str] = set()
    for raw_page in raw_pages:
        number = int(raw_page.get("number", 0))
        if number < 1 or number in seen_pages:
            raise ModelingError(f"invalid or duplicate page number: {number}")
        seen_pages.add(number)
        width = float(raw_page.get("width", 0))
        height = float(raw_page.get("height", 0))
        if width <= 0 or height <= 0:
            raise ModelingError(f"page {number}: dimensions must be positive")
        blocks: list[dict[str, Any]] = []
        seen_orders: set[int] = set()
        for raw_block in sorted(raw_page.get("blocks", []), key=lambda item: int(item.get("order", 0))):
            block_id = str(raw_block.get("id") or "")
            if not block_id or block_id in seen_blocks:
                raise ModelingError(f"missing or duplicate block id: {block_id!r}")
            seen_blocks.add(block_id)
            bbox = [float(value) for value in raw_block.get("bbox", [])]
            if len(bbox) != 4:
                raise ModelingError(f"{block_id}: bbox must contain four values")
            if not (0 <= bbox[0] <= bbox[2] <= width and 0 <= bbox[1] <= bbox[3] <= height):
                raise ModelingError(f"{block_id}: bbox lies outside its page")
            order = int(raw_block.get("order", 0))
            if order in seen_orders:
                raise ModelingError(f"page {number}: duplicate block order {order}")
            seen_orders.add(order)
            role = raw_block.get("role", "body")
            if role not in {"body", "heading", "caption", "formula", "reference", "header", "footer"}:
                role = "body"
            blocks.append(
                {
                    "id": block_id,
                    "text": str(raw_block.get("text") or ""),
                    "bbox": bbox,
                    "order": order,
                    "role": role,
                }
            )
        pages.append(
            {
                "number": number,
                "width": width,
                "height": height,
                "blocks": blocks,
                "render_path": str(raw_page["render_path"]) if raw_page.get("render_path") else None,
            }
        )
    pages.sort(key=lambda page: page["number"])
    if not pages:
        raise ModelingError("parsed_document has no pages")
    if [page["number"] for page in pages] != list(range(1, len(pages) + 1)):
        raise ModelingError("page numbers must form the contiguous range 1..page_count")
    return pages


def _normalize_sections(raw_sections: Iterable[Mapping[str, Any]], pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    valid_blocks = {block["id"] for page in pages for block in page["blocks"]}
    block_pages = {block["id"]: page["number"] for page in pages for block in page["blocks"]}
    sections: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_sections, 1):
        section_id = str(raw.get("id") or f"section:{index:03d}")
        if section_id in seen:
            raise ModelingError(f"duplicate section id: {section_id}")
        seen.add(section_id)
        block_ids = [str(block_id) for block_id in raw.get("block_ids", [])]
        unknown = [block_id for block_id in block_ids if block_id not in valid_blocks]
        if unknown:
            raise ModelingError(f"{section_id}: unknown blocks: {unknown}")
        page_start = max(1, int(raw.get("page_start", 1)))
        page_end = max(1, int(raw.get("page_end", page_start)))
        if page_end < page_start:
            raise ModelingError(f"{section_id}: page_end precedes page_start")
        if any(not page_start <= block_pages[block_id] <= page_end for block_id in block_ids):
            raise ModelingError(f"{section_id}: block lies outside section page range")
        sections.append(
            {
                "id": section_id,
                "title": str(raw.get("title") or ""),
                "level": max(1, int(raw.get("level", 1))),
                "page_start": page_start,
                "page_end": page_end,
                "block_ids": block_ids,
            }
        )
    return sections


def _block_indexes(
    pages: list[dict[str, Any]], sections: list[dict[str, Any]]
) -> tuple[dict[str, tuple[int, dict[str, Any]]], dict[str, str]]:
    blocks = {block["id"]: (page["number"], block) for page in pages for block in page["blocks"]}
    block_sections: dict[str, str] = {}
    for section in sections:
        for block_id in section["block_ids"]:
            block_sections.setdefault(block_id, section["id"])
    return blocks, block_sections


def reconstruct_evidence(pages: list[dict[str, Any]], locator: Mapping[str, Any]) -> tuple[str, list[float]]:
    """Reconstruct and validate one continuous, same-page Evidence span."""

    page_number = int(locator.get("page", 0))
    page = next((item for item in pages if item["number"] == page_number), None)
    if page is None:
        raise ModelingError(f"unknown evidence page: {page_number}")
    by_id = {block["id"]: block for block in page["blocks"]}
    block_ids = [str(item) for item in locator.get("block_ids", [])]
    if not block_ids or any(block_id not in by_id for block_id in block_ids):
        raise ModelingError(f"page {page_number}: evidence contains an unknown block")
    selected = [by_id[block_id] for block_id in block_ids]
    orders = [block["order"] for block in selected]
    if orders != sorted(orders) or any(right != left + 1 for left, right in zip(orders, orders[1:])):
        raise ModelingError("Evidence block_ids must be consecutive and in reading order")
    joined = " ".join(block["text"].strip() for block in selected).strip()
    start = int(locator.get("char_start", 0))
    end = int(locator.get("char_end", len(joined)))
    if start < 0 or end <= start or end > len(joined):
        raise ModelingError(f"invalid Evidence char range [{start}, {end}) for {len(joined)} characters")
    bbox = [
        min(block["bbox"][0] for block in selected),
        min(block["bbox"][1] for block in selected),
        max(block["bbox"][2] for block in selected),
        max(block["bbox"][3] for block in selected),
    ]
    return joined[start:end], bbox


def _build_evidence(
    pages: list[dict[str, Any]], sections: list[dict[str, Any]], source: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    _, block_sections = _block_indexes(pages, sections)
    evidence: list[dict[str, Any]] = []
    by_block: dict[str, str] = {}
    for page in pages:
        for block in page["blocks"]:
            text = block["text"].strip()
            if block["role"] not in _CONTENT_ROLES or not text:
                continue
            evidence_id = f"evidence:{len(evidence) + 1:05d}"
            locator = {
                "page": page["number"],
                "block_ids": [block["id"]],
                "char_start": 0,
                "char_end": len(text),
                "bbox": list(block["bbox"]),
                "pdf_url": _pdf_url(source, page["number"]),
            }
            item: dict[str, Any] = {
                "id": evidence_id,
                "verbatim_text": text,
                "text_sha256": _sha256(text),
                "locator": locator,
                "provenance": {
                    "kind": "paper_verbatim",
                    "agent": "knowledge-modeling:deterministic",
                    "source_ids": [block["id"]],
                    "verification": "verified",
                },
            }
            if block["id"] in block_sections:
                item["section_id"] = block_sections[block["id"]]
            evidence.append(item)
            by_block[block["id"]] = evidence_id
    return evidence, by_block


def _claim_summary(text: str) -> str:
    """Create a readable, complete extractive summary without changing meaning."""

    compound_prefixes = {
        "agent", "answer", "cross", "data", "end", "evidence", "group",
        "human", "long", "machine", "memory", "model", "multi", "page",
        "reward", "self", "source", "state", "task", "user",
    }

    def repair_linebreak_hyphen(match: re.Match[str]) -> str:
        left, right = match.group(1), match.group(2)
        suffixes = {"al", "able", "ed", "es", "ible", "ing", "ion", "ity", "ive", "ly", "ment", "ness", "ous", "s"}
        preserve = ("-" in left or left.casefold() in compound_prefixes) and right.casefold() not in suffixes and not right.isupper()
        return f"{left}-{right}" if preserve else f"{left}{right}"

    value = re.sub(
        r"\b([A-Za-z]+(?:-[A-Za-z]+)*)-\s*\n\s*([A-Za-z]+)\b",
        repair_linebreak_hyphen,
        str(text),
    )
    value = _normalize_text(value)
    value = re.sub(r"^([A-Z])\s+([a-z]{1,2})\b", lambda match: match.group(1) + match.group(2), value)
    broken_word = re.compile(r"\b([a-z]+)-\s+([a-z]+)\b")

    def dehyphenate(match: re.Match[str]) -> str:
        # Keep an existing compound such as ``memory-missing`` when a line
        # happens to break after its second component.
        if match.start() > 0 and value[match.start() - 1] == "-":
            return match.group(0)
        return match.group(1) + match.group(2)

    value = broken_word.sub(dehyphenate, value)
    # PDF text extraction commonly collapses an adjacent prose token and a
    # short math symbol (for example ``dimensiondk`` or ``exceedsr10``).
    # Repair only after a small set of grammatical boundary words so ordinary
    # identifiers and scientific terms remain untouched.
    for prefix in ("and", "dimension", "exceeds", "to", "values", "with"):
        value = re.sub(
            rf"\b{prefix}(?=(?:d(?:model|[kv])|[hqr]\d*)\b)",
            prefix + " ",
            value,
            flags=re.IGNORECASE,
        )
    value = re.sub(r"(?<=[.!?])(?=[A-Z])", " ", value)
    value = re.sub(r"(?<=[A-Z])(?=(?:and|or|with)\b)", " ", value)
    heading = re.match(r"^([A-Z][^.]{0,40})\.\s+(?=[A-Z])", value)
    if heading and len(heading.group(1).split()) <= 4:
        value = value[heading.end() :]
    lowercase_tail = re.search(r"\.\s+(?=[a-z])", value)
    if lowercase_tail and lowercase_tail.start() >= 30:
        value = value[: lowercase_tail.start() + 1]
    value = re.sub(r"\s+([,.;:!?])", r"\1", value)
    return value


def _claim_fingerprint(text: str) -> str:
    """Normalize superficial PDF/citation differences for claim de-duplication."""

    value = re.sub(r"\[[0-9,\s-]+\]", " ", _claim_summary(text).casefold())
    value = re.sub(r"[^\w\u0370-\u03ff]+", " ", value, flags=re.UNICODE)
    return " ".join(value.split())


def _claim_is_duplicate(summary: str, existing: Iterable[str]) -> bool:
    """Conservatively collapse exact and near-identical repeated claims."""

    fingerprint = _claim_fingerprint(summary)
    if not fingerprint:
        return True
    for previous in existing:
        if fingerprint == previous:
            return True
        shorter, longer = sorted((fingerprint, previous), key=len)
        if len(shorter) >= 60 and shorter in longer and len(shorter) / max(1, len(longer)) >= 0.9:
            return True
        if min(len(fingerprint), len(previous)) >= 80 and difflib.SequenceMatcher(None, fingerprint, previous).ratio() >= 0.96:
            return True
    return False


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    """Find complete sentence spans; an unterminated trailing fragment is omitted."""

    boundaries = [0]
    boundaries.extend(
        match.end()
        for match in re.finditer(r"[.!?](?:[\"'”’\]])?\s+(?=(?:[\"'“‘\[(]?)[A-Z0-9])", text)
    )
    boundaries.extend(match.end() for match in re.finditer(r"[。！？](?:[”’》】])?\s*", text))
    boundaries = sorted(set(boundaries))
    spans: list[tuple[int, int]] = []
    for start, end in zip(boundaries, boundaries[1:]):
        while start < end and text[start].isspace():
            start += 1
        while end > start and text[end - 1].isspace():
            end -= 1
        if end > start:
            spans.append((start, end))
    tail_start = boundaries[-1]
    tail = text[tail_start:].rstrip()
    if tail.endswith((".", "!", "?", "。", "！", "？", '."', '.”', ".'")):
        start = tail_start
        while start < len(text) and text[start].isspace():
            start += 1
        spans.append((start, start + len(text[start:].rstrip())))
    return spans


def _claim_evidence_candidates(
    pages: list[dict[str, Any]],
    sections: list[dict[str, Any]],
    source: Mapping[str, Any],
) -> list[dict[str, Any]]:
    _, block_sections = _block_indexes(pages, sections)
    candidates: list[dict[str, Any]] = []
    for page in pages:
        runs: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        current_section: str | None = None
        for block in page["blocks"]:
            section_id = block_sections.get(block["id"])
            contiguous = current and block["order"] == current[-1]["order"] + 1 and section_id == current_section
            if block["role"] == "body" and block["text"].strip():
                if current and not contiguous:
                    runs.append(current)
                    current = []
                if not current:
                    current_section = section_id
                current.append(block)
            elif current:
                runs.append(current)
                current = []
                current_section = None
        if current:
            runs.append(current)

        for run in runs:
            texts = [block["text"].strip() for block in run]
            starts: list[int] = []
            cursor = 0
            for index, text in enumerate(texts):
                starts.append(cursor)
                cursor += len(text) + (1 if index + 1 < len(texts) else 0)
            joined = " ".join(texts)
            for span_start, span_end in _sentence_spans(joined):
                sentence = joined[span_start:span_end]
                readable = _claim_summary(sentence)
                first_alpha = next((character for character in readable if character.isalpha()), "")
                math_marks = len(re.findall(r"[=<>≤≥≻]", readable))
                truncated_reference = bool(re.search(r"\b(?:Sec|Fig|Eq)\.$", readable, re.IGNORECASE))
                merged_footnote = bool(re.search(r"\b\d+[A-Z][a-z]", readable))
                short_colon_tail = bool(re.search(r":\s+[A-Z][A-Za-z-]*(?:\s+[A-Za-z-]+){0,2}\.$", readable))
                metadata_like = "@" in sentence or bool(re.search(r"\b[A-Z]\.$", readable))
                if not 35 <= len(readable) <= 1200 or first_alpha and first_alpha.islower() or math_marks or truncated_reference or merged_footnote or short_colon_tail or metadata_like:
                    continue
                first_index = max(index for index, start in enumerate(starts) if start <= span_start)
                last_index = max(index for index, start in enumerate(starts) if start < span_end)
                selected = run[first_index : last_index + 1]
                local_start = span_start - starts[first_index]
                local_end = span_end - starts[first_index]
                locator = {
                    "page": page["number"],
                    "block_ids": [block["id"] for block in selected],
                    "char_start": local_start,
                    "char_end": local_end,
                    "bbox": [
                        min(block["bbox"][0] for block in selected),
                        min(block["bbox"][1] for block in selected),
                        max(block["bbox"][2] for block in selected),
                        max(block["bbox"][3] for block in selected),
                    ],
                    "pdf_url": _pdf_url(source, page["number"]),
                }
                reconstructed, _ = reconstruct_evidence(pages, locator)
                if reconstructed != sentence:
                    raise ModelingError("internal sentence locator could not be reconstructed")
                candidate: dict[str, Any] = {
                    "verbatim_text": sentence,
                    "text_sha256": _sha256(sentence),
                    "locator": locator,
                    "provenance": {
                        "kind": "paper_verbatim",
                        "agent": "knowledge-modeling:sentence-builder",
                        "source_ids": locator["block_ids"],
                        "verification": "verified",
                    },
                }
                section_id = block_sections.get(selected[0]["id"])
                if section_id:
                    candidate["section_id"] = section_id
                candidates.append(candidate)
    return candidates


def _build_claims(
    pages: list[dict[str, Any]],
    sections: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    source: Mapping[str, Any],
) -> list[dict[str, Any]]:
    candidates = _claim_evidence_candidates(pages, sections, source)
    section_order = [section["id"] for section in sections] or [None]
    selected: list[dict[str, Any]] = []
    selected_summaries: list[str] = []
    for section_id in section_order:
        pool = [item for item in candidates if section_id is None or item.get("section_id") == section_id]
        pool.sort(
            key=lambda item: (
                not bool(_CLAIM_CUES.search(item["verbatim_text"])),
                item["locator"]["page"],
                item["locator"]["block_ids"][0],
                item["locator"]["char_start"],
            )
        )
        added = 0
        for item in pool:
            summary = _claim_summary(item["verbatim_text"])
            if _claim_is_duplicate(summary, selected_summaries):
                continue
            selected.append(item)
            selected_summaries.append(_claim_fingerprint(summary))
            added += 1
            if added == 2:
                break
    if not selected:
        for item in candidates:
            summary = _claim_summary(item["verbatim_text"])
            if _claim_is_duplicate(summary, selected_summaries):
                continue
            selected.append(item)
            selected_summaries.append(_claim_fingerprint(summary))
            if len(selected) == 6:
                break
    unique: list[dict[str, Any]] = []
    seen: set[tuple[object, ...]] = set()
    for item in selected:
        key = (
            item["locator"]["page"],
            tuple(item["locator"]["block_ids"]),
            item["locator"]["char_start"],
            item["locator"]["char_end"],
        )
        if key not in seen and len(unique) < 16:
            unique.append(item)
            seen.add(key)
    claims: list[dict[str, Any]] = []
    for item in unique:
        equivalent = next(
            (
                existing
                for existing in evidence
                if existing["verbatim_text"] == item["verbatim_text"]
                and existing["locator"]["page"] == item["locator"]["page"]
                and existing["locator"]["block_ids"] == item["locator"]["block_ids"]
                and existing["locator"].get("char_start", 0) == item["locator"]["char_start"]
                and existing["locator"].get("char_end") == item["locator"]["char_end"]
            ),
            None,
        )
        if equivalent is None:
            item["id"] = f"evidence:claim:{len(claims) + 1:05d}"
            evidence.append(item)
            evidence_id = item["id"]
        else:
            evidence_id = equivalent["id"]
        claims.append(
            {
                "id": f"claim:{len(claims) + 1:04d}",
                "summary": _claim_summary(item["verbatim_text"]),
                "display_label": "原文摘录",
                "evidence_ids": [evidence_id],
                "importance": "primary" if _CLAIM_CUES.search(item["verbatim_text"]) else "context",
                "provenance": {
                    "kind": "paper_excerpt",
                    "agent": "knowledge-modeling:deterministic",
                    "source_ids": [evidence_id],
                    "verification": "verified",
                },
            }
        )
    return claims


def _formula_latex(text: str, label: object) -> str:
    value = _prepare_math_text(text)
    if label:
        value = re.sub(rf"\s*\({re.escape(str(label))}\)\s*$", "", value)
    # PDF extractors commonly insert visual-layout spaces around arguments.
    # Removing only comma and closing-parenthesis padding preserves operators
    # while giving stable function signatures such as Attention(Q,K,V).
    value = re.sub(r",\s*", ",", value)
    value = re.sub(r"\s+\)", ")", value)
    value = re.sub(
        r"√\s*([A-Za-z\u0391-\u03a9\u03b1-\u03c9][A-Za-z0-9_\u0391-\u03a9\u03b1-\u03c9]*)",
        lambda match: r"\sqrt{" + _canonical_symbol(match.group(1)) + "}",
        value,
    )
    value = re.sub(r"\b([A-Z])([A-Z])T\b", r"\1\2^{T}", value)
    value = _MATH_TOKEN_RE.sub(
        lambda match: match.group(0) if match.group(0).casefold() in _MATH_FUNCTIONS else _canonical_symbol(match.group(0)),
        value,
    )
    value = re.sub(r"_([0-9]{2,}|[A-Za-z]{2,})\b", r"_{\1}", value)
    replacements = {"∑": r"\sum", "Σ": r"\Sigma", "∫": r"\int", "√": r"\sqrt", "≤": r"\le", "≥": r"\ge", "→": r"\to", "≈": r"\approx", "×": r"\times", "·": r"\cdot", "−": "-", **_GREEK_LATEX}
    for original, latex in replacements.items():
        value = value.replace(original, latex)
    return value


def _prepare_math_text(text: str) -> str:
    """Repair a few common PDF glyph-boundary losses without paper-specific rules."""

    value = _normalize_text(text).replace("ϵ", "ε").replace("µ", "μ")
    # Superscripts can be emitted before their subscript by PDF extractors,
    # for example ``d−0.5 model`` for d_model^{-0.5}. Restore the identifier
    # before tokenization so ``d`` and ``model`` cannot become two variables.
    value = re.sub(
        r"\b([A-Za-z])\s*([−-]\s*\d+(?:\.\d+)?)\s+([A-Za-z]{2,})\b",
        lambda match: f"{match.group(1)}_{match.group(3)}^{match.group(2).replace(' ', '')}",
        value,
    )
    value = re.sub(r"(?<=[A-Za-z])(?=[\u0391-\u03a9\u03b1-\u03c9])", " ", value)
    value = re.sub(r"(?<![A-Za-z])([a-z\u03b1-\u03c9])(?=[A-Z\u0391-\u03a9])", r"\1 ", value)
    value = re.sub(r"(?<=[A-Za-z\u0391-\u03a9\u03b1-\u03c9])\s+(?=\d+\b)", "_", value)
    return value


def _canonical_symbol(token: str) -> str:
    token = token.replace("ϵ", "ε").replace("µ", "μ").strip()
    digit = re.fullmatch(r"([A-Za-z\u0391-\u03a9\u03b1-\u03c9])_?(\d+)", token)
    if digit:
        return f"{digit.group(1)}_{digit.group(2)}"
    explicit_subscript = re.fullmatch(r"([A-Za-z\u0391-\u03a9\u03b1-\u03c9]+)_([A-Za-z0-9]+)", token)
    if explicit_subscript:
        return f"{explicit_subscript.group(1)}_{{{explicit_subscript.group(2)}}}"
    # PDF extraction commonly collapses d_k, d_v and d_model. Restrict this
    # repair to equation regions and the conventional single-letter base d.
    compact_dimension = re.fullmatch(r"d([a-z]{1,10})", token)
    if compact_dimension:
        suffix = compact_dimension.group(1)
        return f"d_{suffix}" if len(suffix) == 1 else f"d_{{{suffix}}}"
    compact_index = re.fullmatch(r"([A-Z\u0391-\u03a9\u03b1-\u03c9])([a-z])", token)
    if compact_index:
        return f"{compact_index.group(1)}_{compact_index.group(2)}"
    named_index = re.fullmatch(r"([a-z]{2,15})(i)", token)
    if named_index and token.casefold() not in _MATH_FUNCTIONS:
        return f"{named_index.group(1)}_{named_index.group(2)}"
    return token


def _extract_formula_variables(text: str) -> list[str]:
    """Extract identifier candidates from equations while excluding prose/functions."""

    prepared = _prepare_math_text(text)
    equals = [match.start() for match in re.finditer(r"=", prepared)]
    if not equals:
        return []
    lhs_tokens = [
        match.group(1)
        for match in re.finditer(r"([A-Za-z\u0391-\u03a9\u03b1-\u03c9][A-Za-z0-9_\u0391-\u03a9\u03b1-\u03c9]*)\s*=", prepared)
    ]
    prefix = prepared[: equals[0]].strip()
    formula_starts_line = len(prefix) <= 32 and len(re.findall(r"[A-Za-z]{2,}", prefix)) <= 2
    candidates = list(lhs_tokens)
    if formula_starts_line:
        math_region = prepared
        sentence_end = re.search(r"\.\s+[A-Z]", math_region[equals[0] :])
        if sentence_end:
            math_region = math_region[: equals[0] + sentence_end.start() + 1]
        for match in _MATH_TOKEN_RE.finditer(math_region):
            token = match.group(0)
            following = math_region[match.end() :].lstrip()
            if token.casefold() in _MATH_FUNCTIONS or following.startswith("(") and token.isalpha() and len(token) > 1:
                continue
            candidates.append(token)
    symbols: list[str] = []
    for token in candidates:
        symbol = _canonical_symbol(token)
        if symbol.casefold() in _VARIABLE_STOP or symbol.casefold() in _MATH_FUNCTIONS:
            continue
        if symbol not in symbols:
            symbols.append(symbol)
    return symbols


def _symbol_pattern(symbol: str) -> str:
    """Match normalized, braced, spaced, or PDF-collapsed forms of a symbol."""

    normalized = symbol.replace("ϵ", "ε")
    if "_" in normalized:
        base, suffix = normalized.split("_", 1)
        suffix = suffix.strip("{}")
        return rf"{re.escape(base)}\s*(?:_\s*\{{?\s*{re.escape(suffix)}\s*\}}?|\s*{re.escape(suffix)})"
    return re.escape(normalized)


def _definition_evidence(
    symbol: str,
    formula_page: int,
    formula_block_ids: list[str],
    evidence: list[dict[str, Any]],
    formula_text: str | None = None,
) -> str | None:
    symbol_pattern = _symbol_pattern(symbol)
    direct_definition = re.compile(
        rf"(?<![A-Za-z0-9]){symbol_pattern}(?![A-Za-z0-9])\s*(?:=|∈|(?:is|denotes|represents|indicates|refers\s+to)\b)",
        re.IGNORECASE,
    )
    where_group = re.compile(
        rf"\bwhere\b[^.;:]{{0,160}}(?<![A-Za-z0-9]){symbol_pattern}(?![A-Za-z0-9])[^.;:]{{0,100}}\b(?:is|are|denotes?|represents?|indicates?)\b",
        re.IGNORECASE,
    )
    noun_first = re.compile(
        rf"\b(?:quer(?:y|ies)|keys?|values?|matri(?:x|ces)|vectors?|dimensions?|sizes?|positions?|numbers?|parameters?|rates?)\b"
        rf"[^.;:]{{0,80}}(?<![A-Za-z0-9]){symbol_pattern}(?![A-Za-z0-9])",
        re.IGNORECASE,
    )
    formula_evidence = {
        item["id"]
        for item in evidence
        if any(block_id in formula_block_ids for block_id in item["locator"]["block_ids"])
    }
    candidates = sorted(
        evidence,
        key=lambda item: (
            abs(item["locator"]["page"] - formula_page),
            0 if item["id"] not in formula_evidence else 1,
            item["id"],
        ),
    )
    for item in candidates:
        if abs(item["locator"]["page"] - formula_page) > 1:
            continue
        prepared = _prepare_math_text(item["verbatim_text"])
        if direct_definition.search(prepared) or where_group.search(prepared) or noun_first.search(prepared):
            return item["id"]
    # A leading identifier on the left side of an equation is defined or
    # constrained by that equation itself. This safely recovers symbols such
    # as A_11 in ``A11 - A10 = ...`` without treating every RHS token as a
    # separately defined variable.
    if formula_text:
        prepared_formula = _prepare_math_text(formula_text)
        first = _MATH_TOKEN_RE.search(prepared_formula)
        if first and "=" in prepared_formula and _canonical_symbol(first.group(0)) == symbol:
            return min(formula_evidence) if formula_evidence else None
    formula_candidates = [item for item in candidates if item["id"] in formula_evidence]
    for item in formula_candidates:
        prepared = _prepare_math_text(item["verbatim_text"])
        first = _MATH_TOKEN_RE.search(prepared)
        if first and "=" in prepared and _canonical_symbol(first.group(0)) == symbol:
            return item["id"]
    return None


def _build_formulas_variables(
    parsed: Mapping[str, Any], pages: list[dict[str, Any]], evidence: list[dict[str, Any]], source: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    blocks, _ = _block_indexes(pages, [])
    evidence_by_block = {item["locator"]["block_ids"][0]: item["id"] for item in evidence if len(item["locator"]["block_ids"]) == 1}
    formulas: list[dict[str, Any]] = []
    variables_by_symbol: dict[str, dict[str, Any]] = {}
    review_items: list[dict[str, Any]] = []
    for raw in parsed.get("formula_candidates", []):
        block_ids = [str(item) for item in raw.get("block_ids", [])]
        page_number = int(raw.get("page", 0))
        if not block_ids or any(block_id not in blocks or blocks[block_id][0] != page_number for block_id in block_ids):
            continue
        joined = " ".join(blocks[block_id][1]["text"].strip() for block_id in block_ids).strip()
        char_start = int(raw.get("char_start", 0))
        char_end = int(raw.get("char_end", len(joined)))
        if char_start < 0 or char_end <= char_start or char_end > len(joined):
            continue
        locator = {
            "page": page_number,
            "block_ids": block_ids,
            "char_start": char_start,
            "char_end": char_end,
            "bbox": list(raw.get("bbox") or blocks[block_ids[0]][1]["bbox"]),
            "pdf_url": _pdf_url(source, page_number),
        }
        formula_id = f"formula:{len(formulas) + 1:04d}"
        text = str(raw.get("text") or reconstruct_evidence(pages, locator)[0])
        variable_ids: list[str] = []
        for symbol in _extract_formula_variables(text):
            definition_id = _definition_evidence(symbol, page_number, block_ids, evidence, text)
            if not definition_id:
                review_items.append(
                    {
                        "kind": "variable_definition_missing",
                        "formula_id": formula_id,
                        "symbol": symbol,
                        "page": page_number,
                        "reason": "The symbol occurs in the formula, but no explicit nearby definition or assignment was found.",
                    }
                )
                continue
            variable = variables_by_symbol.get(symbol)
            if variable is None:
                variable = {
                    "id": f"variable:{len(variables_by_symbol) + 1:04d}",
                    "symbol": symbol,
                    "definition_evidence_id": definition_id,
                    "formula_ids": [],
                }
                variables_by_symbol[symbol] = variable
            variable["formula_ids"].append(formula_id)
            variable_ids.append(variable["id"])
        source_ids = [evidence_by_block[block_id] for block_id in block_ids if block_id in evidence_by_block]
        formulas.append(
            {
                "id": formula_id,
                "label": str(raw["label"]) if raw.get("label") is not None else None,
                "latex": _formula_latex(text, raw.get("label")),
                "mathml": None,
                "locator": locator,
                "variable_ids": variable_ids,
                "provenance": {
                    "kind": "paper_verbatim",
                    "agent": "knowledge-modeling:deterministic",
                    "source_ids": source_ids,
                    "verification": "verified" if raw.get("confidence") == "high" else "pending",
                },
            }
        )
        if raw.get("confidence") != "high":
            review_items.append(
                {
                    "kind": "formula_verification_pending",
                    "formula_id": formula_id,
                    "page": page_number,
                    "reason": "The formula has no reliable displayed equation number and needs visual verification.",
                }
            )
    return formulas, list(variables_by_symbol.values()), review_items


def _caption_locator(raw: Mapping[str, Any], source: Mapping[str, Any]) -> dict[str, Any] | None:
    block_ids = [str(item) for item in raw.get("caption_block_ids", [])]
    page = int(raw.get("page", 0))
    if not block_ids or page < 1:
        return None
    locator: dict[str, Any] = {"page": page, "block_ids": block_ids, "pdf_url": _pdf_url(source, page)}
    if raw.get("bbox") is not None:
        locator["bbox"] = [float(value) for value in raw["bbox"]]
    return locator


def _build_visuals(
    parsed: Mapping[str, Any], source: Mapping[str, Any], evidence_by_block: Mapping[str, str]
) -> list[dict[str, Any]]:
    visuals: list[dict[str, Any]] = []
    asset_counts: dict[str, int] = {}
    for figure in parsed.get("figures", []):
        key = str(figure.get("asset_id") or figure.get("asset_path") or "")
        if key:
            asset_counts[key] = asset_counts.get(key, 0) + 1
    for raw in parsed.get("figures", []):
        locator = _caption_locator(raw, source)
        block_ids = locator["block_ids"] if locator else []
        evidence_ids = [evidence_by_block[block_id] for block_id in block_ids if block_id in evidence_by_block]
        visual_id = f"visual:{len(visuals) + 1:04d}"
        asset_key = str(raw.get("asset_id") or raw.get("asset_path") or "")
        asset_path = (
            str(raw["asset_path"])
            if raw.get("asset_path") and raw.get("asset_validation") != "failed" and asset_counts.get(asset_key) == 1
            else None
        )
        visuals.append(
            {
                "id": visual_id,
                "kind": "paper_original",
                "visual_type": "figure",
                "title": f"Figure {raw.get('label') or len(visuals) + 1}",
                "caption": str(raw.get("caption") or "") or None,
                "locator": locator,
                "asset_path": asset_path,
                "derived_from": [],
                "interactive_targets": [
                    {
                        "id": f"target:{len(visuals) + 1:04d}:caption",
                        "label": "Caption",
                        "evidence_ids": evidence_ids,
                        "interactive": bool(evidence_ids),
                        "value": raw.get("label"),
                        "position": {},
                    }
                ],
                "provenance": {
                    "kind": "paper_verbatim",
                    "agent": "knowledge-modeling:deterministic",
                    "source_ids": evidence_ids,
                    "verification": "verified" if locator and asset_path else "pending",
                },
            }
        )
    return visuals


def _build_tables(
    parsed: Mapping[str, Any], source: Mapping[str, Any], evidence_by_block: Mapping[str, str]
) -> list[dict[str, Any]]:
    tables: list[dict[str, Any]] = []
    for raw in parsed.get("tables", []):
        locator = _caption_locator(raw, source)
        if locator is None:
            continue
        caption_evidence = [evidence_by_block[item] for item in locator["block_ids"] if item in evidence_by_block]
        discussed_cells: list[dict[str, Any]] = []
        # Parser-supplied cell bindings are accepted only when they already
        # point to registered Evidence.  No value-presence heuristic is used.
        known_evidence = set(evidence_by_block.values())
        for cell in raw.get("discussed_cells", []):
            evidence_ids = [str(item) for item in cell.get("evidence_ids", [])]
            if evidence_ids and all(item in known_evidence for item in evidence_ids):
                discussed_cells.append(
                    {"row": int(cell["row"]), "column": int(cell["column"]), "evidence_ids": evidence_ids}
                )
        asset_path = str(raw["asset_path"]) if raw.get("asset_path") and raw.get("asset_validation") != "failed" else None
        tables.append(
            {
                "id": f"table:{len(tables) + 1:04d}",
                "caption": str(raw.get("caption") or ""),
                "locator": locator,
                "columns": [str(item) for item in raw.get("columns", [])],
                "rows": copy.deepcopy(raw.get("rows", [])),
                "asset_path": asset_path,
                "discussed_cells": discussed_cells,
                "provenance": {
                    "kind": "paper_verbatim",
                    "agent": "knowledge-modeling:deterministic",
                    "source_ids": caption_evidence,
                    "verification": "verified" if asset_path or (raw.get("columns") and raw.get("rows")) else "pending",
                },
            }
        )
    return tables


def _reference_number(raw_reference: str, fallback: int) -> int:
    match = re.match(r"\s*\[(\d{1,4})\]", raw_reference)
    return int(match.group(1)) if match else fallback


def _reference_url(raw_reference: str) -> str | None:
    url = _URL_RE.search(raw_reference)
    if url:
        return url.group(0).rstrip(".,;")
    doi = _DOI_RE.search(raw_reference)
    if doi:
        return f"https://doi.org/{doi.group(0).rstrip('.,;')}"
    arxiv = _ARXIV_RE.search(raw_reference)
    if arxiv:
        return f"https://arxiv.org/abs/{arxiv.group(1)}"
    return None


def _citation_mentions(evidence: list[dict[str, Any]]) -> dict[int, list[str]]:
    mentions: dict[int, list[str]] = {}
    for item in evidence:
        for match in _INLINE_CITATION_RE.finditer(item["verbatim_text"]):
            expression = match.group(1)
            for part in re.split(r"\s*,\s*", expression):
                if "-" in part:
                    left, right = (int(value.strip()) for value in part.split("-", 1))
                    if 0 <= right - left <= 25:
                        numbers = range(left, right + 1)
                    else:
                        numbers = ()
                else:
                    numbers = (int(part),)
                for number in numbers:
                    mentions.setdefault(number, []).append(item["id"])
    return mentions


def _reference_author_year_keys(reference: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Extract first-author surname/year keys from one or more reference entries.

    Some PDF parsers merge multiple bibliography entries into one block.  A
    reference start still normally has ``authors. YEAR. title``; looking
    backwards from that year is more robust than assuming one parsed block is
    one bibliography entry.
    """

    supplied = reference.get("_author_year_keys")
    if isinstance(supplied, list):
        return [(str(item[0]).casefold(), str(item[1]).casefold()) for item in supplied if isinstance(item, (list, tuple)) and len(item) == 2]
    raw_reference = str(reference.get("raw_reference") or "")
    keys: list[tuple[str, str]] = []
    for match in re.finditer(r"\.\s*((?:19|20)\d{2})([a-z]?)\.\s+", raw_reference, re.IGNORECASE):
        prefix = raw_reference[: match.start()]
        start = prefix.rfind(". ")
        author_text = prefix[start + 2 :].strip() if start >= 0 else prefix.strip()
        if not author_text or ("," not in author_text and " and " not in author_text.casefold()):
            continue
        first_author = re.split(r",|\band\b", author_text, maxsplit=1, flags=re.IGNORECASE)[0].strip()
        surname_tokens = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ'’-]+", first_author)
        if not surname_tokens:
            continue
        keys.append((surname_tokens[-1].casefold(), match.group(1) + match.group(2).casefold()))
    return list(dict.fromkeys(keys))


def _split_reference_records(references: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Split parser-merged author-year bibliography blocks conservatively.

    Numbered bibliography items already have stable boundaries and are kept as
    is. For unnumbered blocks, ``authors. YEAR. title`` year delimiters provide
    recoverable records even when a page/column parser merged many entries.
    Internal matching hints are removed before Paper IR serialization.
    """

    # Preserve parser-established entry boundaries. Concatenating every
    # unnumbered record loses each entry's leading authors when it is split
    # again at the year marker. A genuinely merged parser record is still
    # handled below when that one record contains multiple year markers.
    source = [dict(item) for item in references]
    numbered_re = re.compile(r"(?<!\S)\[(\d{1,4})\]\s+(?=[A-ZÀ-ÖØ-Þ])")
    combined = " ".join(_normalize_text(item.get("raw_reference")) for item in source).strip()
    numbered = list(numbered_re.finditer(combined))
    if numbered:
        records: list[dict[str, Any]] = []
        for index, marker in enumerate(numbered):
            next_start = numbered[index + 1].start() if index + 1 < len(numbered) else len(combined)
            segment = combined[marker.start() : next_start].strip()
            if not segment:
                continue
            item = dict(source[min(index, len(source) - 1)])
            item["raw_reference"] = segment
            body = numbered_re.sub("", segment, count=1)
            year_match = re.search(r"\.\s*((?:19|20)\d{2})([a-z]?)\.\s+", body, re.IGNORECASE)
            if year_match:
                after_year = body[year_match.end() :]
                item["title"] = re.split(r"\.\s+", after_year, maxsplit=1)[0].strip(" ,;.") or None
                item["year"] = int(year_match.group(1))
                author_text = body[: year_match.start()].strip(" ,;.")
                first_author = re.split(r",|\band\b", author_text, maxsplit=1, flags=re.IGNORECASE)[0].strip()
                surname_tokens = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ'’-]+", first_author)
                if surname_tokens:
                    item["_author_year_keys"] = [[surname_tokens[-1].casefold(), year_match.group(1) + year_match.group(2).casefold()]]
            records.append(item)
        return records
    marker_re = re.compile(r"\.\s*((?:19|20)\d{2})([a-z]?)\.\s+", re.IGNORECASE)
    if source and any(not list(marker_re.finditer(_normalize_text(item.get("raw_reference")))) for item in source):
        merged = dict(source[0])
        merged["raw_reference"] = " ".join(_normalize_text(item.get("raw_reference")) for item in source)
        source = [merged]
    records: list[dict[str, Any]] = []
    for reference in source:
        raw = _normalize_text(reference.get("raw_reference"))
        if not raw:
            continue
        markers = list(marker_re.finditer(raw))
        if len(markers) <= 1:
            records.append(dict(reference))
            continue
        for index, marker in enumerate(markers):
            next_start = markers[index + 1].start() if index + 1 < len(markers) else len(raw)
            after_year = raw[marker.end() : next_start]
            title = re.split(r"\.\s+", after_year, maxsplit=1)[0].strip(" ,;.") or None
            author_window = raw[max(0, marker.start() - 500) : marker.start()]
            starts = [
                candidate.end()
                for candidate in re.finditer(r"(?:^|\.\s+)(?=[A-ZÀ-ÖØ-Þ][^.,]{1,60},)", author_window)
            ]
            author_text = author_window[starts[-1] :] if starts else author_window
            first_author = author_text.split(",", 1)[0].strip()
            surname_tokens = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ'’-]+", first_author)
            surnames = [surname_tokens[-1].casefold()] if surname_tokens else []
            segment = f"{marker.group(1)}{marker.group(2)}. {after_year}".strip()
            item = dict(reference)
            item["raw_reference"] = segment
            item["title"] = title
            item["year"] = int(marker.group(1))
            item["_author_year_keys"] = [[surname, marker.group(1) + marker.group(2).casefold()] for surname in dict.fromkeys(surnames)]
            records.append(item)
    return records


def _author_year_mentions(
    parsed: Mapping[str, Any], evidence: list[dict[str, Any]], references: list[Mapping[str, Any]]
) -> dict[int, list[str]]:
    """Bind author-year citations, including markers split over adjacent blocks."""

    block_roles = {
        block["id"]: block.get("role")
        for page in parsed.get("pages", [])
        for block in page.get("blocks", [])
    }
    body = [
        item for item in evidence
        if all(block_roles.get(block_id) == "body" for block_id in item.get("locator", {}).get("block_ids", []))
    ]
    block_body = [item for item in body if not str(item["id"]).startswith("evidence:claim:")]
    block_positions = {item["id"]: index for index, item in enumerate(block_body)}
    mentions: dict[int, list[str]] = {}
    for reference_index, reference in enumerate(references, 1):
        keys = _reference_author_year_keys(reference)
        direct: list[str] = []
        for surname, year in keys:
            year_base = year[:4]
            suffix = re.escape(year[4:]) if len(year) > 4 else r"[a-z]?"
            pattern = re.compile(
                rf"\b{re.escape(surname)}\b[^();]{{0,60}}?\b{re.escape(year_base)}{suffix}\b",
                re.IGNORECASE,
            )
            direct.extend(item["id"] for item in body if pattern.search(item["verbatim_text"]))
        if direct:
            # A direct match is the smallest trustworthy citation context.  Do
            # not also attach neighbouring paragraphs merely because a wider
            # search window happens to contain the same author and year.
            expanded: list[str] = []
            for evidence_id in direct:
                position = block_positions.get(evidence_id)
                item = block_body[position] if position is not None else None
                if item is not None and re.match(r"^\s*al\.[,;]?", item["verbatim_text"], re.IGNORECASE) and position:
                    previous = block_body[position - 1]
                    if (
                        previous.get("locator", {}).get("page") == item.get("locator", {}).get("page")
                        and previous.get("section_id") == item.get("section_id")
                    ):
                        expanded.append(previous["id"])
                expanded.append(evidence_id)
            mentions[reference_index] = list(dict.fromkeys(expanded))
            continue

        # Some PDF layouts split ``Surname et al.,`` and ``2025`` across two
        # adjacent body blocks.  Only when no single Evidence item matches do
        # we admit the shortest same-page, same-section pair.
        split_matches: list[str] = []
        for index, item in enumerate(block_body[:-1]):
            following = block_body[index + 1]
            if (
                item.get("locator", {}).get("page") != following.get("locator", {}).get("page")
                or item.get("section_id") != following.get("section_id")
            ):
                continue
            text = f"{item['verbatim_text']} {following['verbatim_text']}"
            if any(
                re.search(
                    rf"\b{re.escape(surname)}\b[^();]{{0,60}}?\b{re.escape(year[:4])}{re.escape(year[4:]) if len(year) > 4 else '[a-z]?'}\b",
                    text,
                    re.IGNORECASE,
                )
                for surname, year in keys
            ):
                split_matches.extend([item["id"], following["id"]])
        if split_matches:
            mentions[reference_index] = list(dict.fromkeys(split_matches))
    return mentions


def _build_citations(parsed: Mapping[str, Any], evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    mentions = _citation_mentions(evidence)
    references = _split_reference_records(parsed.get("references", []))
    author_year_mentions = _author_year_mentions(parsed, evidence, references)
    citations: list[dict[str, Any]] = []
    for index, raw in enumerate(references, 1):
        reference = _normalize_text(raw.get("raw_reference"))
        if not reference:
            continue
        number = _reference_number(reference, index)
        years = _YEAR_RE.findall(reference)
        citations.append(
            {
                "id": f"citation:{len(citations) + 1:04d}",
                "raw_reference": reference,
                "title": raw.get("title"),
                "authors": [str(item) for item in raw.get("authors", [])],
                "year": int(years[-1]) if years else None,
                "external_url": _reference_url(reference),
                "cluster": None,
                "cited_in_evidence_ids": list(dict.fromkeys([*mentions.get(number, []), *author_year_mentions.get(index, [])])),
                "verification": "unverified",
            }
        )
    return citations


def _abstract(pages: list[dict[str, Any]], sections: list[dict[str, Any]]) -> str:
    blocks = {block["id"]: block for page in pages for block in page["blocks"]}
    for section in sections:
        if "abstract" in section["title"].casefold():
            values = [blocks[item]["text"].strip() for item in section["block_ids"] if item in blocks and blocks[item]["role"] == "body"]
            return _normalize_text(" ".join(values))
    for page in pages[:2]:
        for index, block in enumerate(page["blocks"]):
            match = re.match(r"\s*abstract\s*[:.—-]?\s*(.*)$", block["text"], re.IGNORECASE | re.DOTALL)
            if match:
                return _normalize_text(match.group(1) or " ".join(item["text"] for item in page["blocks"][index + 1 : index + 3]))
    return ""


def _paper_metadata(parsed: Mapping[str, Any], pages: list[dict[str, Any]], sections: list[dict[str, Any]], source: Mapping[str, Any]) -> dict[str, Any]:
    supplied = parsed.get("paper") or parsed.get("metadata") or {}
    first_page_blocks = [block for block in pages[0]["blocks"] if block["role"] not in {"header", "footer"}]
    title = str(supplied.get("title") or "").strip()
    if not title:
        excluded = re.compile(r"\b(?:arxiv|abstract|copyright|all rights reserved|permission|attribution|preprint)\b", re.I)
        heading_candidates: list[tuple[float, int, str]] = []
        for block in first_page_blocks:
            first_line = next((line.strip() for line in block["text"].splitlines() if line.strip()), "")
            if block.get("role") == "heading" and first_line and not excluded.search(first_line):
                heading_candidates.append((float(block.get("font_size_median") or 0), -int(block.get("order") or 0), first_line))
        if heading_candidates:
            title = max(heading_candidates)[2]
        else:
            for block in first_page_blocks:
                first_line = next((line.strip() for line in block["text"].splitlines() if line.strip()), "")
                if first_line and not excluded.search(first_line):
                    title = first_line
                    break
    original_url = source.get("original_url")
    arxiv_match = _ARXIV_RE.search(str(supplied.get("arxiv_id") or original_url or source.get("local_pdf") or ""))
    year = supplied.get("year")
    return {
        "id": str(supplied.get("id") or f"paper:{source['sha256'][:16]}"),
        "title": title or "Untitled paper",
        "authors": [str(item) for item in supplied.get("authors", [])],
        "year": int(year) if year not in {None, ""} else None,
        "venue": supplied.get("venue"),
        "doi": supplied.get("doi"),
        "arxiv_id": str(supplied.get("arxiv_id") or arxiv_match.group(1)) if supplied.get("arxiv_id") or arxiv_match else None,
        "abstract": str(supplied.get("abstract") or _abstract(pages, sections)),
        "language": str(supplied.get("language") or "en"),
        "external_url": supplied.get("external_url") or original_url,
    }


def _relations(
    claims: list[dict[str, Any]], formulas: list[dict[str, Any]], variables: list[dict[str, Any]],
    visuals: list[dict[str, Any]], citations: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    edges: list[tuple[str, str, str]] = []
    for claim in claims:
        edges.extend(("supports", evidence_id, claim["id"]) for evidence_id in claim["evidence_ids"])
    for variable in variables:
        edges.append(("defines", variable["definition_evidence_id"], variable["id"]))
        edges.extend(("mentions", formula_id, variable["id"]) for formula_id in variable.get("formula_ids", []))
    for visual in visuals:
        if visual["kind"] == "derived":
            edges.extend(("derived_from", visual["id"], source_id) for source_id in visual.get("derived_from", []))
        for target in visual.get("interactive_targets", []):
            edges.extend(("visualizes", visual["id"], evidence_id) for evidence_id in target.get("evidence_ids", []))
    for citation in citations:
        edges.extend(("cites", evidence_id, citation["id"]) for evidence_id in citation["cited_in_evidence_ids"])
    unique = list(dict.fromkeys(edges))
    return [
        {"id": f"relation:{index:05d}", "type": edge_type, "source_id": source_id, "target_id": target_id}
        for index, (edge_type, source_id, target_id) in enumerate(unique, 1)
    ]


def _upsert_entities(existing: list[dict[str, Any]], patches: Iterable[Mapping[str, Any]], group: str) -> list[dict[str, Any]]:
    output = copy.deepcopy(existing)
    indexes = {item["id"]: index for index, item in enumerate(output)}
    for raw_patch in patches:
        patch = copy.deepcopy(dict(raw_patch))
        entity_id = str(patch.get("id") or "")
        if not entity_id:
            raise ModelingError(f"overlay.{group} item requires id")
        remove = bool(patch.pop("remove", False))
        if remove:
            if entity_id in indexes:
                output.pop(indexes[entity_id])
                indexes = {item["id"]: index for index, item in enumerate(output)}
            continue
        if entity_id in indexes:
            output[indexes[entity_id]].update(patch)
        else:
            output.append(patch)
            indexes[entity_id] = len(output) - 1
    return output


def _apply_overlay(ir: dict[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    unknown = set(overlay) - _ALLOWED_OVERLAY_KEYS
    if unknown:
        raise ModelingError(f"unsupported overlay fields: {sorted(unknown)}")
    result = copy.deepcopy(ir)
    stale_items: list[dict[str, Any]] = []
    skipped_variable_ids: set[str] = set()
    if "paper" in overlay:
        result["paper"].update(copy.deepcopy(dict(overlay["paper"])))
    entity_patches = {group: list(overlay.get(group, [])) for group in ("claims", "formulas", "variables", "visuals", "tables", "citations", "relations")}
    claims_by_id = {item["id"]: item for item in result["claims"]}
    for patch in entity_patches["claims"]:
        existing = claims_by_id.get(str(patch.get("id")))
        if existing and "summary" in patch and patch["summary"] != existing.get("summary"):
            # Any human-authored rewrite is generated content, even when it
            # replaces a deterministic extractive claim in place.
            patch["display_label"] = "生成式总结"
            patch["provenance"] = {
                "kind": "generated_summary",
                "agent": "knowledge-modeling:human-overlay",
                "source_ids": list(patch.get("evidence_ids", existing.get("evidence_ids", []))),
                "verification": "verified",
            }
    variable_patches = {str(item.get("id")): item for item in entity_patches["variables"]}
    formula_by_id = {item["id"]: item for item in result["formulas"]}
    accepted_formula_patches: list[Mapping[str, Any]] = []
    for patch in entity_patches["formulas"]:
        target = formula_by_id.get(str(patch.get("id")))
        linked_ids = [str(item) for item in patch.get("variable_ids", [])]
        linked_symbols = [str(variable_patches[item].get("symbol") or "") for item in linked_ids if item in variable_patches]
        if target and linked_symbols and not all(
            re.search(_symbol_pattern(_canonical_symbol(symbol.replace(" ", "_"))), _prepare_math_text(target["latex"]), re.IGNORECASE)
            for symbol in linked_symbols
        ):
            skipped_variable_ids.update(linked_ids)
            stale_items.append(
                {
                    "kind": "stale_formula_overlay",
                    "formula_id": patch.get("id"),
                    "reason": "Overlay variables no longer match the formula after parser output changed; the patch was not applied.",
                }
            )
            continue
        accepted_formula_patches.append(patch)
    entity_patches["formulas"] = accepted_formula_patches
    entity_patches["variables"] = [item for item in entity_patches["variables"] if str(item.get("id")) not in skipped_variable_ids]
    for group in ("claims", "formulas", "variables", "visuals", "tables", "citations", "relations"):
        if group in overlay:
            result[group] = _upsert_entities(result[group], entity_patches[group], group)
    if "evidence" in overlay:
        patched = _upsert_entities(result["evidence"], overlay["evidence"], "evidence")
        rebuilt: list[dict[str, Any]] = []
        for item in patched:
            text, bbox = reconstruct_evidence(result["pages"], item["locator"])
            supplied = item.get("verbatim_text")
            if supplied is not None and supplied != text:
                raise ModelingError(f"{item['id']}: overlay verbatim_text does not match locator")
            item["verbatim_text"] = text
            item["text_sha256"] = _sha256(text)
            item["locator"] = dict(item["locator"])
            item["locator"]["bbox"] = bbox
            item["locator"]["pdf_url"] = _pdf_url(result["source"], item["locator"]["page"])
            item["provenance"] = {
                "kind": "paper_verbatim",
                "agent": "knowledge-modeling:human-overlay",
                "source_ids": list(item["locator"]["block_ids"]),
                "verification": "verified",
            }
            rebuilt.append(item)
        result["evidence"] = rebuilt
    if "review" in overlay:
        result["review"].update(copy.deepcopy(dict(overlay["review"])))
    if stale_items:
        result["review"]["status"] = "needs_review"
        result["review"].setdefault("items", []).extend(stale_items)
    return result


def _validate_internal(ir: Mapping[str, Any]) -> None:
    """Enforce strong entity types and source fidelity before publishing IR."""

    groups = ("sections", "evidence", "claims", "formulas", "variables", "visuals", "tables", "citations")
    types: dict[str, str] = {}
    entities: dict[str, Mapping[str, Any]] = {}
    for group in groups:
        for item in ir[group]:
            entity_id = item["id"]
            if entity_id in types:
                raise ModelingError(f"duplicate entity id: {entity_id}")
            types[entity_id] = group
            entities[entity_id] = item
    section_blocks = {section["id"]: set(section["block_ids"]) for section in ir["sections"]}
    for item in ir["evidence"]:
        text, _ = reconstruct_evidence(ir["pages"], item["locator"])
        if text != item["verbatim_text"] or _sha256(text) != item["text_sha256"]:
            raise ModelingError(f"{item['id']}: Evidence cannot be reconstructed")
        if item["provenance"].get("kind") != "paper_verbatim" or item["provenance"].get("verification") != "verified":
            raise ModelingError(f"{item['id']}: Evidence provenance is not verified paper_verbatim")
        section_id = item.get("section_id")
        if section_id and (section_id not in section_blocks or not set(item["locator"]["block_ids"]).issubset(section_blocks[section_id])):
            raise ModelingError(f"{item['id']}: Evidence section does not contain its blocks")
    claim_fingerprints: list[str] = []
    for claim in ir["claims"]:
        evidence_ids = claim.get("evidence_ids", [])
        if not evidence_ids or any(types.get(item) != "evidence" for item in evidence_ids):
            raise ModelingError(f"{claim['id']}: claim must reference Evidence only")
        provenance_kind = claim.get("provenance", {}).get("kind")
        expected_label = {"paper_excerpt": "原文摘录", "generated_summary": "生成式总结"}.get(provenance_kind)
        if expected_label is None or claim.get("display_label") != expected_label:
            raise ModelingError(f"{claim['id']}: claim label does not match its provenance")
        summary = str(claim.get("summary") or "").strip()
        if len(summary) < 20 or not re.search(r"[.!?。！？][\"'”’]?\s*$", summary) or summary.endswith(("…", "-")):
            raise ModelingError(f"{claim['id']}: claim summary must be a complete sentence")
        if provenance_kind == "paper_excerpt":
            if len(evidence_ids) != 1 or summary != _claim_summary(str(entities[evidence_ids[0]].get("verbatim_text") or "")):
                raise ModelingError(f"{claim['id']}: paper excerpt must match its source Evidence")
        elif any(_claim_fingerprint(summary) == _claim_fingerprint(str(entities[item].get("verbatim_text") or "")) for item in evidence_ids):
            raise ModelingError(f"{claim['id']}: generated summary must differ from source Evidence")
        if _claim_is_duplicate(summary, claim_fingerprints):
            raise ModelingError(f"{claim['id']}: duplicate claim summary")
        claim_fingerprints.append(_claim_fingerprint(summary))
    variables = {item["id"]: item for item in ir["variables"]}
    formulas = {item["id"]: item for item in ir["formulas"]}
    for formula in formulas.values():
        if not formula.get("latex"):
            raise ModelingError(f"{formula['id']}: formula has empty latex")
        if any(variable_id not in variables for variable_id in formula.get("variable_ids", [])):
            raise ModelingError(f"{formula['id']}: formula references an unknown variable")
        for variable_id in formula.get("variable_ids", []):
            if formula["id"] not in variables[variable_id].get("formula_ids", []):
                raise ModelingError(f"{formula['id']}: formula-variable references are not bidirectional")
    for variable in variables.values():
        if types.get(variable.get("definition_evidence_id")) != "evidence":
            raise ModelingError(f"{variable['id']}: variable definition is not Evidence")
        if any(formula_id not in formulas or variable["id"] not in formulas[formula_id].get("variable_ids", []) for formula_id in variable.get("formula_ids", [])):
            raise ModelingError(f"{variable['id']}: variable-formula references are not bidirectional")
    target_ids: set[str] = set()
    for visual in ir["visuals"]:
        if visual["kind"] == "derived":
            if visual.get("provenance", {}).get("kind") != "derived_visual" or not visual.get("derived_from"):
                raise ModelingError(f"{visual['id']}: derived visual lacks verified sources")
            if any(source_id not in types or source_id == visual["id"] for source_id in visual["derived_from"]):
                raise ModelingError(f"{visual['id']}: derived visual has an invalid source")
        for target in visual.get("interactive_targets", []):
            if target["id"] in target_ids:
                raise ModelingError(f"duplicate visual target id: {target['id']}")
            target_ids.add(target["id"])
            evidence_ids = target.get("evidence_ids", [])
            if any(types.get(item) != "evidence" for item in evidence_ids) or (target.get("interactive") and not evidence_ids):
                raise ModelingError(f"{target['id']}: visual target has invalid Evidence")
    for table in ir["tables"]:
        rows = table.get("rows", [])
        column_count = len(table.get("columns", []))
        if rows and (not column_count or any(len(row) != column_count for row in rows)):
            raise ModelingError(f"{table['id']}: table rows and columns are inconsistent")
        seen_cells: set[tuple[int, int]] = set()
        for cell in table.get("discussed_cells", []):
            coordinate = (cell["row"], cell["column"])
            if coordinate in seen_cells or coordinate[0] < 0 or coordinate[1] < 0:
                raise ModelingError(f"{table['id']}: invalid or duplicate discussed cell")
            if coordinate[0] >= len(rows) or coordinate[1] >= column_count:
                raise ModelingError(f"{table['id']}: discussed cell lies outside table")
            if any(types.get(item) != "evidence" for item in cell.get("evidence_ids", [])):
                raise ModelingError(f"{table['id']}: discussed cell has invalid Evidence")
            seen_cells.add(coordinate)
    for citation in ir["citations"]:
        if any(types.get(item) != "evidence" for item in citation.get("cited_in_evidence_ids", [])):
            raise ModelingError(f"{citation['id']}: citation context is not Evidence")
        url = citation.get("external_url")
        if url and not str(url).startswith(("http://", "https://")):
            raise ModelingError(f"{citation['id']}: external_url must use HTTP(S)")
    relation_ids: set[str] = set()
    allowed = {
        "supports": ("evidence", "claims"),
        "defines": ("evidence", "variables"),
        "mentions": ("formulas", "variables"),
        "visualizes": ("visuals", "evidence"),
        "cites": ("evidence", "citations"),
        "same_lineage": ("citations", "citations"),
    }
    for relation in ir["relations"]:
        relation_id = relation["id"]
        if relation_id in relation_ids:
            raise ModelingError(f"duplicate relation id: {relation_id}")
        relation_ids.add(relation_id)
        source_id, target_id = relation["source_id"], relation["target_id"]
        if source_id == target_id or source_id not in types or target_id not in types:
            raise ModelingError(f"{relation_id}: relation has invalid endpoints")
        if relation["type"] == "derived_from":
            valid = types[source_id] == "visuals"
        else:
            expected = allowed.get(relation["type"])
            valid = bool(expected and types[source_id] == expected[0] and types[target_id] == expected[1])
        if not valid:
            raise ModelingError(f"{relation_id}: invalid domain/range for {relation['type']}")


def _normalize_overlay_entities(ir: dict[str, Any]) -> None:
    for claim in ir["claims"]:
        claim.setdefault("display_label", "生成式总结")
        claim.setdefault("importance", "secondary")
        claim.setdefault(
            "provenance",
            {"kind": "generated_summary", "agent": "knowledge-modeling:human-overlay", "source_ids": list(claim.get("evidence_ids", [])), "verification": "verified"},
        )
    for citation in ir["citations"]:
        citation.setdefault("title", None)
        citation.setdefault("authors", [])
        citation.setdefault("year", None)
        citation.setdefault("external_url", None)
        citation.setdefault("cluster", None)
        citation.setdefault("cited_in_evidence_ids", [])
        citation.setdefault("verification", "unverified")
    ir.setdefault("review", {"status": "pending", "items": []})


def build_paper_ir(parsed_document: Mapping[str, Any], *, overlay: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Build a schema-shaped Paper IR without mutating parsed input."""

    if parsed_document.get("schema_version") != IR_SCHEMA_VERSION:
        raise ModelingError(f"unsupported parsed_document schema: {parsed_document.get('schema_version')!r}")
    pages = _normalize_pages(parsed_document.get("pages", []))
    source = _normalize_source(parsed_document.get("source", {}), len(pages))
    declared_count = int(parsed_document.get("source", {}).get("page_count", len(pages)))
    if declared_count != len(pages):
        raise ModelingError("source.page_count does not match parsed pages")
    input_hashes = parsed_document.get("input_hashes", [])
    if input_hashes and source["sha256"] not in input_hashes:
        raise ModelingError("source.sha256 is absent from parsed_document.input_hashes")
    if parsed_document.get("status", "passed") != "passed":
        approval = overlay.get("approval", {}) if overlay else {}
        if not (
            approval.get("status") == "approved"
            and approval.get("base_source_sha256") == source["sha256"]
            and str(approval.get("reviewer") or "").strip()
            and str(approval.get("reason") or "").strip()
        ):
            raise ModelingError("non-passed parsed_document requires a source-bound approved human overlay")
    sections = _normalize_sections(parsed_document.get("sections", []), pages)
    evidence, evidence_by_block = _build_evidence(pages, sections, source)
    claims = _build_claims(pages, sections, evidence, source)
    formulas, variables, formula_review_items = _build_formulas_variables(parsed_document, pages, evidence, source)
    visuals = _build_visuals(parsed_document, source, evidence_by_block)
    tables = _build_tables(parsed_document, source, evidence_by_block)
    citations = _build_citations(parsed_document, evidence)
    ir = {
        "schema_version": IR_SCHEMA_VERSION,
        "paper": _paper_metadata(parsed_document, pages, sections, source),
        "source": source,
        "pages": pages,
        "sections": sections,
        "evidence": evidence,
        "claims": claims,
        "formulas": formulas,
        "variables": variables,
        "visuals": visuals,
        "tables": tables,
        "citations": citations,
        "relations": _relations(claims, formulas, variables, visuals, citations),
        "review": {"status": "needs_review" if formula_review_items else "pending", "items": formula_review_items},
    }
    if overlay:
        ir = _apply_overlay(ir, overlay)
        _normalize_overlay_entities(ir)
        # Relations generated before an overlay remain stable; explicit overlay
        # relations can supplement them without silently inventing new edges.
    _validate_internal(ir)
    schema_errors = validate_schema(ir, Path(__file__).resolve().parents[2])
    if schema_errors:
        raise ModelingError("Paper IR schema validation failed: " + "; ".join(schema_errors[:5]))
    return ir


def model_parsed_file(
    parsed_path: str | Path,
    *,
    output_path: str | Path,
    overlay_path: str | Path | None = None,
) -> dict[str, Any]:
    """Read parsed JSON, optionally apply a human overlay, and atomically save IR."""

    parsed = json.loads(Path(parsed_path).read_text(encoding="utf-8"))
    overlay = json.loads(Path(overlay_path).read_text(encoding="utf-8")) if overlay_path else None
    ir = build_paper_ir(parsed, overlay=overlay)
    atomic_write_json(Path(output_path), ir)
    return ir
