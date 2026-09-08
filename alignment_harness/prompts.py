from __future__ import annotations

import json
from pathlib import Path
from typing import Any


COMMON_JSON_RULE = """
Return one JSON object only: no Markdown fence and no prose outside JSON.
Never invent a claim, number, quotation, page, figure, or table. Use null or an
empty list when evidence is absent. Separate directly observed content from
interpretation. Organize information at three levels: overview, section, detail.
""".strip()


PAPER_OUTPUT_SPEC = {
    "paper_id": "string",
    "title": "string|null",
    "overview": [{"role": "problem|method|results|conclusion|limitation", "summary": "string", "evidence": [{"page": "int|null", "quote": "string"}], "confidence": "0..1"}],
    "sections": [{"heading": "string", "role": "string", "summary": "string", "details": [{"claim": "string", "evidence": [{"page": "int|null", "quote": "string"}]}]}],
    "visual_references": [{"kind": "figure|table|equation", "label": "string|null", "page": "int|null", "caption": "string|null", "purpose": "string"}],
    "warnings": ["string"],
}


PRESENTATION_OUTPUT_SPEC = {
    "paper_id": "string",
    "presentation_type": "poster|ppt",
    "title": "string|null",
    "overview": [{"role": "problem|method|results|conclusion|limitation", "summary": "string", "observed_text": "string|null", "location": "string", "visual_weight": "0..1"}],
    "sections": [{"heading": "string", "role": "string", "summary": "string", "details": [{"claim": "string", "observed_text": "string|null", "location": "string"}]}],
    "visual_references": [{"kind": "figure|table|equation|diagram", "location": "string", "visible_label": "string|null", "observable_description": "string", "possible_paper_source": "string|null"}],
    "narrative_order": ["string"],
    "warnings": ["string"],
}


def presentation_prompt(
    pair: dict[str, Any], source: Path | None, embedded_content: str | None = None,
    unit_label: str = "document",
) -> str:
    input_instruction = (
        f"Use the Read tool to inspect the local {pair['presentation_type']} file at {source}."
        if source
        else f"Inspect this extracted slide content:\nPRESENTATION_CONTENT_BEGIN\n{embedded_content}\nPRESENTATION_CONTENT_END"
    )
    return f"""You are the presentation-side extraction agent. {input_instruction} Extract what the author chose,
how it is compressed and ordered, and the visible evidence. Do not use or infer from
the paper PDF. For a poster, respect spatial panels; for slides, respect page sequence.

Pair id: {pair['id']}
Expected title: {pair.get('title')}
Current unit: {unit_label}. Extract only content visible in this unit; do not guess missing units.
Output shape (descriptive, not literal values):
{json.dumps(PRESENTATION_OUTPUT_SPEC, ensure_ascii=False)}

Limit overview to 2 items, sections to 3, details to 2 per section, and visual references
to 4. {COMMON_JSON_RULE}"""


def paper_prompt(
    pair: dict[str, Any], source: Path, harness_instruction: str, original_pdf: Path | None = None,
    embedded_content: str | None = None, unit_label: str = "document",
) -> str:
    provenance = f" It was deterministically extracted from {original_pdf}." if original_pdf else ""
    source_instruction = (
        f"Use the Read tool to inspect the local page-addressable paper context at {source}."
        if embedded_content is None
        else f"Use the page-addressable PDF text below:\nPAPER_CONTEXT_BEGIN\n{embedded_content}\nPAPER_CONTEXT_END"
    )
    return f"""You are the paper-side extraction agent. {source_instruction}{provenance} Extract a coarse-to-fine account that would let a reader
create an accurate scholarly poster or talk. You cannot see the paired presentation.

Pair id: {pair['id']}
Expected title: {pair.get('title')}
Current unit: {unit_label}. Extract only claims supported in this page group.
Current harness instruction:
{harness_instruction}

Output shape (descriptive, not literal values):
{json.dumps(PAPER_OUTPUT_SPEC, ensure_ascii=False)}

Limit overview to 2 items, sections to 3, details to 2 per section, and visual references
to 4. {COMMON_JSON_RULE}"""


def alignment_prompt(pair: dict[str, Any], presentation: dict[str, Any], paper: dict[str, Any]) -> str:
    return f"""You are an independent alignment judge. Compare the presentation-side
extraction with the paper-only extraction. Treat the presentation as a noisy reference,
not ground truth. Reward paper content that matches the author's selection and coarse-to-
fine organization, while penalizing unsupported detail and missing emphasized content.

Pair: {pair['id']}
PRESENTATION={json.dumps(presentation, ensure_ascii=False)}
PAPER={json.dumps(paper, ensure_ascii=False)}

Return exactly one JSON object with: overall_score (0..1), scores containing coverage,
hierarchy, evidence_grounding, visual_alignment, and faithfulness (each 0..1), matched
(list of short strings), missing_from_paper_harness (list), unsupported_or_overstated
(list), presentation_only_external (list), and recommendations (list of concrete prompt
or orchestration changes). {COMMON_JSON_RULE}"""


def optimizer_prompt(current_instruction: str, audits: list[dict[str, Any]]) -> str:
    return f"""You are the meta-harness optimizer. Improve only the paper-side extraction
instruction using the alignment audits below. Do not tailor it to names or facts from one
paper. Preserve evidence requirements and coarse-to-fine output. Make one conservative,
generalizable revision suitable for unseen papers.

CURRENT_INSTRUCTION={current_instruction}
AUDITS={json.dumps(audits, ensure_ascii=False)}

Return exactly one JSON object with keys: revised_instruction (string), rationale (list
of strings), expected_improvements (list), risks (list). {COMMON_JSON_RULE}"""


BASELINE_PAPER_INSTRUCTION = """Identify the research problem, motivation, main method,
experiments, key results, conclusion, and explicit limitations. First make a compact
overview, then group by paper section, then retain only details that support a higher-level
item. Attach short verbatim evidence and page numbers whenever available. Register the
paper's figures, tables, and equations by label, caption, page, and communicative purpose."""


def _object_array() -> dict[str, Any]:
    return {"type": "array", "items": {"type": "object", "additionalProperties": True}}


PAPER_SCHEMA = {
    "type": "object",
    "properties": {
        "paper_id": {"type": "string"},
        "title": {"type": ["string", "null"]},
        "overview": _object_array(),
        "sections": _object_array(),
        "visual_references": _object_array(),
        "warnings": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["paper_id", "title", "overview", "sections", "visual_references", "warnings"],
    "additionalProperties": False,
}

PRESENTATION_SCHEMA = {
    "type": "object",
    "properties": {
        "paper_id": {"type": "string"},
        "presentation_type": {"type": "string", "enum": ["poster", "ppt"]},
        "title": {"type": ["string", "null"]},
        "overview": _object_array(),
        "sections": _object_array(),
        "visual_references": _object_array(),
        "narrative_order": {"type": "array", "items": {"type": "string"}},
        "warnings": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["paper_id", "presentation_type", "title", "overview", "sections", "visual_references", "narrative_order", "warnings"],
    "additionalProperties": False,
}

ALIGNMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "overall_score": {"type": "number", "minimum": 0, "maximum": 1},
        "scores": {"type": "object", "additionalProperties": {"type": "number"}},
        "matched": {"type": "array", "items": {"type": "string"}},
        "missing_from_paper_harness": {"type": "array", "items": {"type": "string"}},
        "unsupported_or_overstated": {"type": "array", "items": {"type": "string"}},
        "presentation_only_external": {"type": "array", "items": {"type": "string"}},
        "recommendations": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["overall_score", "scores", "matched", "missing_from_paper_harness", "unsupported_or_overstated", "presentation_only_external", "recommendations"],
    "additionalProperties": False,
}

OPTIMIZER_SCHEMA = {
    "type": "object",
    "properties": {
        "revised_instruction": {"type": "string"},
        "rationale": {"type": "array", "items": {"type": "string"}},
        "expected_improvements": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["revised_instruction", "rationale", "expected_improvements", "risks"],
    "additionalProperties": False,
}
