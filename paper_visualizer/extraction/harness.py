"""Run the optimized paper-only harness and bind its output to Paper IR Evidence.

The research harness is allowed to propose summaries, but production AiReader only
publishes a proposal when its quoted support can be matched back to deterministic PDF
Evidence on the declared page.  Unmatched proposals remain visible in the semantic
extraction artifact and never enter the reader-facing Paper IR.
"""

from __future__ import annotations

import concurrent.futures
import copy
import difflib
import hashlib
import re
from pathlib import Path
from typing import Any, Mapping, Protocol

from alignment_harness.prompts import PAPER_SCHEMA, paper_prompt
from alignment_harness.preprocess import split_paper_context


SCHEMA_VERSION = "1.0.0"
HARNESS_VERSION = "paper-alignment-candidate-v1"
OPTIMIZED_PAPER_INSTRUCTION = """Identify the research problem, motivation, main method, experiments, key results,
conclusion, and explicit limitations. First make a compact overview, then group by paper
section, then retain only details that support a higher-level item. Attach short verbatim
evidence and page numbers whenever available. Register the paper's figures, tables, and
equations by label, caption, page, and communicative purpose.

For each registered figure, separately extract its internal legends, axis labels,
dataset names, method names, and parameter annotations as verbatim strings. If a string
is ambiguous or partially legible, retain it with an explicit `?` uncertainty marker
rather than normalizing it. Record numeric readings only when ticks or annotations are
unambiguous, and attach an explicit confidence note. If a name appears in multiple forms,
retain every form with its source page and flag the inconsistency instead of choosing one.
At every level distinguish direct observation, evidence-backed synthesis, and inference.
Explicitly list pages or sections unavailable to the extractor so coverage gaps cannot
be mistaken for absent content."""


class HarnessRunner(Protocol):
    model: str

    def run(
        self,
        prompt: str,
        cwd: Path,
        *,
        allow_read: bool = True,
        schema: dict[str, Any] | None = None,
    ) -> object: ...


def _compact_text(value: object) -> str:
    return " ".join(str(value or "").split())


def _fingerprint(value: object) -> str:
    return re.sub(r"[^\w]+", "", _compact_text(value).casefold())


def build_parsed_context(parsed: Mapping[str, Any], max_chars: int = 60_000) -> str:
    """Build a page-addressable model context from AiReader's deterministic parse."""

    pages = list(parsed.get("pages") or [])
    source = parsed.get("source") or {}
    per_page = max(1_500, max_chars // max(len(pages), 1))
    header = [
        "# AiReader deterministic PDF extraction",
        "",
        f"- source_sha256: {source.get('sha256') or 'unknown'}",
        f"- page_count: {len(pages)}",
        "- provenance: every text record includes its immutable AiReader block id",
        "",
    ]
    body: list[str] = []
    truncated_pages: list[int] = []
    for page in pages:
        number = int(page.get("number") or 0)
        records = []
        for block in page.get("blocks") or []:
            text = _compact_text(block.get("text"))
            if not text:
                continue
            records.append(
                f"[{block.get('id') or 'unknown-block'} role={block.get('role') or 'body'}] {text}"
            )
        page_text = "\n".join(records)
        if len(page_text) > per_page:
            page_text = page_text[:per_page].rstrip() + "\n[PAGE_TRUNCATED]"
            truncated_pages.append(number)
        body.extend([f"## Page {number}", "", page_text, ""])
    if truncated_pages:
        header.insert(-1, "- truncated_pages: " + ",".join(map(str, truncated_pages)))
    return "\n".join(header + body)


def _clean_model_result(data: Mapping[str, Any]) -> dict[str, Any]:
    def objects(name: str) -> list[dict[str, Any]]:
        return [dict(item) for item in data.get(name) or [] if isinstance(item, Mapping)]

    return {
        "paper_id": _compact_text(data.get("paper_id")) or "unknown",
        "title": _compact_text(data.get("title")) or None,
        "overview": objects("overview"),
        "sections": objects("sections"),
        "visual_references": objects("visual_references"),
        "warnings": [_compact_text(item) for item in data.get("warnings") or [] if _compact_text(item)],
    }


def _aggregate(parts: list[tuple[str, Mapping[str, Any]]], paper_id: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "paper_id": paper_id,
        "title": None,
        "overview": [],
        "sections": [],
        "visual_references": [],
        "warnings": [],
    }
    seen: dict[str, set[str]] = {"overview": set(), "sections": set(), "visual_references": set()}
    identity = {"overview": "summary", "sections": "heading", "visual_references": "label"}
    for unit, raw in parts:
        part = _clean_model_result(raw)
        if result["title"] is None and part["title"]:
            result["title"] = part["title"]
        for key in ("overview", "sections", "visual_references"):
            for item in part[key]:
                marker = _fingerprint(item.get(identity[key]) or item)
                if not marker or marker in seen[key]:
                    continue
                seen[key].add(marker)
                enriched = dict(item)
                enriched.setdefault("source_units", [unit])
                result[key].append(enriched)
        result["warnings"].extend(f"{unit}: {warning}" for warning in part["warnings"])
    return result


def validate_semantic_extraction(value: object) -> list[str]:
    if not isinstance(value, Mapping):
        return ["semantic extraction must be an object"]
    errors: list[str] = []
    if value.get("schema_version") != SCHEMA_VERSION:
        errors.append("unsupported semantic extraction schema_version")
    if value.get("harness_version") != HARNESS_VERSION:
        errors.append("unexpected semantic extraction harness_version")
    if value.get("status") not in {"complete", "partial"}:
        errors.append("semantic extraction status must be complete or partial")
    for key in ("overview", "sections", "visual_references", "warnings", "units"):
        if not isinstance(value.get(key), list):
            errors.append(f"semantic extraction {key} must be a list")
    coverage = value.get("coverage")
    if not isinstance(coverage, Mapping) or int(coverage.get("succeeded_units") or 0) < 1:
        errors.append("semantic extraction has no successful page units")
    return errors


def extract_semantic_content(
    parsed: Mapping[str, Any],
    *,
    paper_id: str,
    runner: HarnessRunner,
    cwd: Path,
    instruction: str = OPTIMIZED_PAPER_INSTRUCTION,
    unit_attempts: int = 3,
) -> dict[str, Any]:
    """Run bounded paper agents over parsed page groups and aggregate their output."""

    context = build_parsed_context(parsed)
    units = split_paper_context(context)
    if not units:
        raise ValueError("parsed document contains no page-addressable extraction units")
    supplied = parsed.get("paper") or parsed.get("metadata") or {}
    pair = {"id": paper_id, "title": supplied.get("title")}
    successes: list[tuple[str, Mapping[str, Any], object]] = []
    failure_details: dict[str, str] = {}
    unit_content = dict(units)

    def invoke(label: str, content: str) -> tuple[Mapping[str, Any], object]:
        response = runner.run(
            paper_prompt(
                pair,
                Path("parsed_document.json"),
                instruction,
                embedded_content=content,
                unit_label=label,
            ),
            cwd,
            allow_read=False,
            schema=PAPER_SCHEMA,
        )
        data = getattr(response, "data")
        if not isinstance(data, Mapping):
            raise TypeError("agent result data is not an object")
        return data, response

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(2, len(units))) as pool:
        futures = {
            pool.submit(invoke, label, content): label
            for label, content in units
        }
        for future in concurrent.futures.as_completed(futures):
            label = futures[future]
            try:
                data, response = future.result()
                successes.append((label, data, response))
            except Exception as exc:
                failure_details[label] = f"{type(exc).__name__}: {exc}"

    # Provider-side concurrent streams occasionally close mid-response. Retry only
    # failed units, sequentially, so successful calls and their cost are never repeated.
    retried_units = 0
    recovered_units = 0
    for attempt in range(2, max(1, unit_attempts) + 1):
        if not failure_details:
            break
        for label in list(failure_details):
            retried_units += 1
            try:
                data, response = invoke(label, unit_content[label])
                successes.append((label, data, response))
                del failure_details[label]
                recovered_units += 1
            except Exception as exc:
                failure_details[label] = f"attempt {attempt}: {type(exc).__name__}: {exc}"
    failures = [f"{label}: {detail[:1200]}" for label, detail in failure_details.items()]
    if not successes:
        raise RuntimeError("all semantic extraction units failed: " + "; ".join(failures))
    order = {label: index for index, (label, _) in enumerate(units)}
    successes.sort(key=lambda item: order[item[0]])
    aggregate = _aggregate([(label, data) for label, data, _ in successes], paper_id)
    aggregate["warnings"].extend(failures)
    result = {
        "schema_version": SCHEMA_VERSION,
        "harness_version": HARNESS_VERSION,
        "status": "partial" if failures else "complete",
        "model": str(getattr(runner, "model", "unknown")),
        "instruction_sha256": hashlib.sha256(instruction.encode("utf-8")).hexdigest(),
        **aggregate,
        "coverage": {
            "page_count": len(parsed.get("pages") or []),
            "attempted_units": len(units),
            "succeeded_units": len(successes),
            "failed_units": len(failures),
            "retried_units": retried_units,
            "recovered_units": recovered_units,
        },
        "units": [
            {
                "label": label,
                "cost_usd": float(getattr(response, "cost_usd", 0.0) or 0.0),
            }
            for label, _, response in successes
        ],
    }
    errors = validate_semantic_extraction(result)
    if errors:
        raise ValueError("invalid semantic extraction: " + "; ".join(errors))
    return result


def _match_evidence(
    reference: Mapping[str, Any], evidence: list[Mapping[str, Any]]
) -> str | None:
    quote = _compact_text(reference.get("quote"))
    if len(quote) < 12:
        return None
    try:
        page = int(reference.get("page"))
    except (TypeError, ValueError):
        return None
    quote_key = _fingerprint(quote)
    if len(quote_key) < 10:
        return None
    candidates = [item for item in evidence if int((item.get("locator") or {}).get("page") or 0) == page]
    ranked: list[tuple[float, str]] = []
    for item in candidates:
        text = _compact_text(item.get("verbatim_text"))
        text_key = _fingerprint(text)
        if quote_key in text_key or (len(text_key) >= 30 and text_key in quote_key):
            score = min(len(quote_key), len(text_key)) / max(len(quote_key), len(text_key)) + 1.0
        else:
            score = difflib.SequenceMatcher(None, quote_key, text_key).ratio()
        ranked.append((score, str(item.get("id") or "")))
    if not ranked:
        return None
    score, evidence_id = max(ranked)
    return evidence_id if score >= 0.92 else None


def _claim_candidates(extraction: Mapping[str, Any]) -> list[tuple[str, str, list[Mapping[str, Any]]]]:
    candidates: list[tuple[str, str, list[Mapping[str, Any]]]] = []
    for item in extraction.get("overview") or []:
        if isinstance(item, Mapping):
            candidates.append(("primary", _compact_text(item.get("summary")), list(item.get("evidence") or [])))
    for section in extraction.get("sections") or []:
        if not isinstance(section, Mapping):
            continue
        for detail in section.get("details") or []:
            if isinstance(detail, Mapping):
                candidates.append(("secondary", _compact_text(detail.get("claim")), list(detail.get("evidence") or [])))
    return candidates


def _complete_sentence(summary: str) -> str | None:
    value = summary.strip()[:1200].rstrip()
    if len(value) < 20 or value.endswith(("…", "...", "-", "—", ":", ";", ",")):
        return None
    if not re.search(r"[.!?。！？][\"'”’]?\s*$", value):
        value += "."
    return value


def apply_semantic_extraction(
    ir: Mapping[str, Any], extraction: Mapping[str, Any]
) -> dict[str, Any]:
    """Prepend evidence-bound harness claims to a strict Paper IR.

    Existing deterministic claims remain as a fallback. Harness claims use IDs that
    sort before them, so downstream selection prefers the optimized information choices.
    """

    result = copy.deepcopy(dict(ir))
    evidence = list(result.get("evidence") or [])
    claims = list(result.get("claims") or [])
    relations = list(result.get("relations") or [])
    existing = {_fingerprint(item.get("summary")) for item in claims}
    added = 0
    rejected = 0
    for importance, raw_summary, references in _claim_candidates(extraction):
        summary = _complete_sentence(raw_summary)
        evidence_ids = list(
            dict.fromkeys(
                evidence_id
                for reference in references
                if isinstance(reference, Mapping)
                for evidence_id in [_match_evidence(reference, evidence)]
                if evidence_id
            )
        )
        fingerprint = _fingerprint(summary)
        evidence_fingerprints = {
            _fingerprint(next(item for item in evidence if item.get("id") == evidence_id).get("verbatim_text"))
            for evidence_id in evidence_ids
        }
        if not summary or not evidence_ids or fingerprint in existing or fingerprint in evidence_fingerprints:
            rejected += 1
            continue
        added += 1
        claim_id = f"claim:0000-harness-{added:04d}"
        claims.append(
            {
                "id": claim_id,
                "summary": summary,
                "display_label": "生成式总结",
                "evidence_ids": evidence_ids,
                "importance": importance,
                "provenance": {
                    "kind": "generated_summary",
                    "agent": f"semantic-extraction:{HARNESS_VERSION}",
                    "source_ids": evidence_ids,
                    "verification": "pending",
                },
            }
        )
        for evidence_id in evidence_ids:
            relations.append(
                {
                    "id": f"relation:0000-harness-{added:04d}-{len(relations) + 1:05d}",
                    "type": "supports",
                    "source_id": evidence_id,
                    "target_id": claim_id,
                }
            )
        existing.add(fingerprint)
    claims.sort(key=lambda item: str(item.get("id") or ""))
    result["claims"] = claims
    result["relations"] = relations
    review = result.setdefault("review", {"status": "pending", "items": []})
    review.setdefault("items", []).append(
        {
            "kind": "semantic_extraction_harness",
            "harness_version": extraction.get("harness_version"),
            "instruction_sha256": extraction.get("instruction_sha256"),
            "matched_claims": added,
            "rejected_unverified_claims": rejected,
            "policy": "Only summaries with a same-page match to deterministic PDF Evidence enter Paper IR.",
        }
    )
    if added:
        review["status"] = "needs_review"
    return result
