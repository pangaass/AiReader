"""Build a paper-agnostic reading plan from the validated Paper IR.

The plan stores presentation choices and IR references, never Evidence text,
captions, formula markup, table values, or bibliographic metadata. Renderers
must dereference those immutable facts from Paper IR.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping

from jsonschema import Draft202012Validator

from ..artifacts import atomic_write_json
from ..validation import validate_ir


SCHEMA_VERSION = "1.0.0"
STAGE_VERSION = "2.0.5"
SECTION_ORDER = (
    ("one_minute_read", "一分钟速读"),
    ("research_task", "研究任务"),
    ("existing_methods", "已有方法"),
    ("motivation", "本文动机"),
    ("method_overview", "方法总览"),
    ("method_details", "方法拆解"),
    ("training_inference", "训练与推理"),
    ("experimental_setup", "实验设计"),
    ("main_results", "主要结果"),
    ("analysis", "消融与分析"),
    ("conclusion_limitations", "结论与局限"),
)

_METHOD = re.compile(
    r"\b(method|approach|model|architecture|algorithm|train|framework|objective|propos|introduc|"
    r"comput|concatenat|project|encode|decode|generat|normaliz|optim|update)\w*\b",
    re.I,
)
_EXPERIMENT = re.compile(
    r"\b(experiments?|experimental|results?|evaluation|benchmark|ablation|accuracy|outperform\w*|performance|metrics?)\b",
    re.I,
)
_PROBLEM = re.compile(r"\b(problem|challenge|limitation|motivat|question|gap|failure|difficult|lack|need)\w*\b", re.I)
_RELATED = re.compile(r"\b(related work|background|prior work|literature)\b", re.I)
_METHOD_SECTION = re.compile(r"\b(method|approach|model|architecture|algorithm|training|objective|implementation)\b", re.I)
_EXPERIMENT_SECTION = re.compile(r"\b(experiment|result|evaluation|benchmark|ablation|analysis|finding)s?\b", re.I)
_PROBLEM_SECTION = re.compile(r"\b(motivation|problem|research question|diagnosis|diagnostic)\b", re.I)
_TASK_SECTION = re.compile(r"\b(task|problem setup|preliminar)\b", re.I)
_OVERVIEW_SECTION = re.compile(r"\b(overview|framework|architecture|system)\b", re.I)
_TRAINING_SECTION = re.compile(r"\b(train|training|inference|objective|loss|optimization|implementation)\b", re.I)
_SETUP_SECTION = re.compile(r"\b(setup|setting|dataset|metric|baseline|study(?:\s+\d+)?|participants?|procedure|implementation detail)\b", re.I)
_RESULT_SECTION = re.compile(r"\b(main results?|results?|evaluation|performance|comparison)\b", re.I)
_ANALYSIS_SECTION = re.compile(r"\b(ablation|analysis|discussion|robustness|sensitivity|case stud|qualitative|error analysis)\b", re.I)
_CONCLUSION_SECTION = re.compile(r"\b(conclusion|limitation|future|broader impact|opportunit)\w*\b", re.I)
_PRIOR_WORK_SENTENCE = re.compile(
    r"\b(?:previous|prior|earlier|existing|recent)(?:\s+[a-z-]+){0,4}\s+"
    r"(?:work|approach|method|model|system)s?\b|\bhas been (?:proposed|studied|used)\b",
    re.I,
)
_BODY_ROLES = {"body"}


class PlanningError(ValueError):
    """Raised when a content plan would lose source fidelity."""


def _digest(value: Mapping[str, Any]) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _indexes(ir: Mapping[str, Any]) -> tuple[dict[str, Mapping[str, Any]], dict[str, str]]:
    entities: dict[str, Mapping[str, Any]] = {}
    types: dict[str, str] = {}
    for group in ("sections", "evidence", "claims", "formulas", "variables", "visuals", "tables", "citations", "relations"):
        for item in ir.get(group, []):
            entity_id = str(item["id"])
            entities[entity_id] = item
            types[entity_id] = group[:-1] if group.endswith("s") else group
    return entities, types


def _evidence_closure(entity_id: str, entities: Mapping[str, Mapping[str, Any]], types: Mapping[str, str], seen: set[str] | None = None) -> list[str]:
    seen = set() if seen is None else seen
    if entity_id in seen or entity_id not in entities:
        return []
    seen.add(entity_id)
    item = entities[entity_id]
    kind = types[entity_id]
    if kind == "evidence":
        return [entity_id]
    refs: list[str] = []
    if kind == "claim":
        refs = list(item.get("evidence_ids", []))
    elif kind == "formula":
        refs = list(item.get("provenance", {}).get("source_ids", []))
    elif kind == "variable":
        refs = [item.get("definition_evidence_id")]
    elif kind == "visual":
        refs = list(item.get("derived_from", [])) + list(item.get("provenance", {}).get("source_ids", []))
        refs += [eid for target in item.get("interactive_targets", []) for eid in target.get("evidence_ids", [])]
    elif kind == "table":
        refs = list(item.get("provenance", {}).get("source_ids", []))
        refs += [eid for cell in item.get("discussed_cells", []) for eid in cell.get("evidence_ids", [])]
    elif kind == "citation":
        refs = list(item.get("cited_in_evidence_ids", []))
    elif kind == "relation":
        refs = [item.get("source_id"), item.get("target_id")]
    result: list[str] = []
    for ref in refs:
        if ref:
            result.extend(_evidence_closure(str(ref), entities, types, seen))
    return list(dict.fromkeys(result))


def _claim_context(claim: Mapping[str, Any], entities: Mapping[str, Mapping[str, Any]]) -> tuple[str, list[str]]:
    parts = [str(claim.get("summary", ""))]
    section_titles: list[str] = []
    for evidence_id in claim.get("evidence_ids", []):
        evidence = entities.get(evidence_id, {})
        parts.append(str(evidence.get("verbatim_text", "")))
        section = entities.get(str(evidence.get("section_id", "")), {})
        title = str(section.get("title", "")).strip()
        if title:
            section_titles.append(title)
    return " ".join(parts), list(dict.fromkeys(section_titles))


def _claim_bucket(claim: Mapping[str, Any], entities: Mapping[str, Mapping[str, Any]]) -> tuple[str, bool]:
    text, section_titles = _claim_context(claim, entities)
    title_text = " ".join(section_titles)

    # Section semantics outrank loose lexical cues. In particular, words such
    # as "improve" occur frequently in method motivation and prior-work prose
    # and must not by themselves turn those sentences into experiment claims.
    if _CONCLUSION_SECTION.search(title_text):
        return "conclusion_limitations", False
    if _RELATED.search(title_text):
        return "existing_methods", False
    if _ANALYSIS_SECTION.search(title_text):
        return "analysis", False
    if _SETUP_SECTION.search(title_text):
        return "experimental_setup", False
    if _RESULT_SECTION.search(title_text) or _EXPERIMENT_SECTION.search(title_text):
        return "main_results", False
    if _TRAINING_SECTION.search(title_text):
        return "training_inference", False
    if _OVERVIEW_SECTION.search(title_text) and _METHOD_SECTION.search(title_text):
        return "method_overview", False
    if _METHOD_SECTION.search(title_text):
        return "method_details", False
    if _PROBLEM_SECTION.search(title_text) and not _EXPERIMENT.search(text):
        return "motivation", False
    if _TASK_SECTION.search(title_text):
        return "research_task", False

    if _PRIOR_WORK_SENTENCE.search(text) and re.search(r"\[[0-9,\s-]+\]|\bet al\.", text, re.I):
        return "existing_methods", False
    if re.search(r"\bwe\s+(?:propose|present|introduce)\b", text, re.I):
        return "method_overview", False
    if _CONCLUSION_SECTION.search(text):
        return "conclusion_limitations", False
    if _ANALYSIS_SECTION.search(text):
        return "analysis", False
    if _SETUP_SECTION.search(text):
        return "experimental_setup", False
    if _EXPERIMENT.search(text):
        return "main_results", False
    if _TRAINING_SECTION.search(text):
        return "training_inference", False
    if _METHOD.search(text):
        return "method_details", False
    if _PROBLEM.search(text):
        return "motivation", False
    return "research_task", True


def _source_section_bucket(title: str) -> str | None:
    """Map an original paper section to the reader-facing story it can support."""

    if _CONCLUSION_SECTION.search(title):
        return "conclusion_limitations"
    if _RELATED.search(title):
        return "existing_methods"
    if _ANALYSIS_SECTION.search(title):
        return "analysis"
    if _SETUP_SECTION.search(title):
        return "experimental_setup"
    if _RESULT_SECTION.search(title) or _EXPERIMENT_SECTION.search(title):
        return "main_results"
    if _PROBLEM_SECTION.search(title) or re.search(r"\bdesign goals?\b", title, re.I):
        return "motivation"
    if _TRAINING_SECTION.search(title):
        return "training_inference"
    if _OVERVIEW_SECTION.search(title):
        return "method_overview"
    if _METHOD_SECTION.search(title):
        return "method_details"
    if _TASK_SECTION.search(title) or re.search(r"\b(abstract|introduction)\b", title, re.I):
        return "research_task"
    return None


def _item(item_id: str, content_type: str, title: str, section: str, source_ids: Iterable[str], evidence_ids: Iterable[str], **extra: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "id": item_id,
        "content_type": content_type,
        "title": title,
        "primary_section": section,
        "source_ids": list(dict.fromkeys(source_ids)),
        "evidence_ids": list(dict.fromkeys(evidence_ids)),
        "support_status": "verified",
    }
    value.update(extra)
    return value


def _semantic_title(text: str, *, limit: int = 88) -> str:
    """Use a compact source-derived phrase instead of numbered placeholder headings."""

    value = " ".join(str(text).split()).strip()
    if not value:
        return "论文内容"
    clause = re.split(r"(?<=[。！？!?])\s+|\s*[;；]\s*", value, maxsplit=1)[0]
    if len(clause) <= limit:
        return clause
    shortened = clause[:limit].rsplit(" ", 1)[0].rstrip(" ,，:：;；")
    return (shortened or clause[:limit]).rstrip() + "…"


def _digest_score(slot_title: str, item: Mapping[str, Any]) -> tuple[int, int]:
    text = str(item.get("body") or "")
    if slot_title == "本文研究的问题":
        score = 3 * bool(re.search(r"\b(we (?:explore|study|investigate|ask)|research question|task|goal|how)\b", text, re.I))
    elif slot_title == "本文的方法":
        score = 3 * bool(re.search(r"\bwe (?:propose|present|introduce)\b", text, re.I))
        score += 3 * bool(re.search(r"\b(method|approach|system|interface|model|framework|architecture)\b", text, re.I))
        score -= 2 * bool(re.search(r"\bdesign goals?\b", text, re.I))
    else:
        score = 3 * bool(re.search(r"\b(result|significant|outperform|improv|accuracy|score|less time|higher|lower|evaluation)\w*\b", text, re.I))
    if slot_title == "本文研究的问题" and re.search(r"\b(future|improv|conclusion)\w*\b", text, re.I):
        score -= 2
    return score, -len(text)


def _section_status(kind: str, item_ids: list[str], fallback: bool = False) -> tuple[str, str | None]:
    if item_ids:
        return ("fallback", "未识别到明确语义章节，使用有来源的通用内容。") if fallback else ("complete", None)
    reasons = {
        "research_task": "未发现可追溯的任务定义。",
        "existing_methods": "未发现正文支持的已有方法讨论。",
        "motivation": "未发现可追溯的研究动机。",
        "method_overview": "未发现可追溯的方法总览。",
        "method_details": "未发现可追溯的方法模块。",
        "training_inference": "论文未单独说明训练或推理流程。",
        "experimental_setup": "未发现可追溯的实验设置。",
        "main_results": "未发现正文支持的主要实验结果。",
        "analysis": "论文未提供消融或深入分析。",
        "conclusion_limitations": "未发现可追溯的结论或局限。",
    }
    return "unavailable", reasons.get(kind, "没有可展示的已验证内容。")


def _build_base(ir: Mapping[str, Any]) -> dict[str, Any]:
    entities, types = _indexes(ir)
    items: list[dict[str, Any]] = []
    by_section: dict[str, list[str]] = {kind: [] for kind, _ in SECTION_ORDER}
    fallback_sections: set[str] = set()

    for index, claim in enumerate(sorted(ir.get("claims", []), key=lambda row: row["id"]), 1):
        section, fallback = _claim_bucket(claim, entities)
        fallback_sections.add(section) if fallback else None
        evidence_ids = _evidence_closure(claim["id"], entities, types)
        if not evidence_ids:
            continue
        claim_text = str(claim["summary"])
        content = _item(
            f"content:narrative:{index:04d}", "narrative", _semantic_title(claim_text), section,
            [claim["id"], *evidence_ids], evidence_ids,
            body=claim_text, claim_id=claim["id"], generation="source_grounded",
        )
        items.append(content)
        by_section[section].append(content["id"])

    narrative_ids = {item["id"] for item in items if item["content_type"] == "narrative"}
    retained_narratives: set[str] = set()
    for kind, item_ids in by_section.items():
        limit = 4 if kind in {"research_task", "method_overview", "method_details", "main_results"} else 3
        selected = [item_id for item_id in item_ids if item_id in narrative_ids][:limit]
        retained_narratives.update(selected)
        by_section[kind] = [item_id for item_id in item_ids if item_id not in narrative_ids or item_id in selected]
    items = [item for item in items if item["id"] not in narrative_ids or item["id"] in retained_narratives]

    # Claim extraction is deliberately conservative, so some important source
    # sections (for example Study Design, Discussion, or Limitations) may not
    # yield a claim. Add one compact, evidence-bound story slot for each such
    # missing reader-facing section; the narrative agent will explain it.
    evidence_by_section: dict[str, list[Mapping[str, Any]]] = {}
    for evidence in ir.get("evidence", []):
        section_id = str(evidence.get("section_id") or "")
        if section_id and evidence.get("provenance", {}).get("kind") == "paper_verbatim":
            evidence_by_section.setdefault(section_id, []).append(evidence)
    narrative_counts = {
        kind: sum(
            next((item for item in items if item["id"] == item_id), {}).get("content_type") == "narrative"
            for item_id in item_ids
        )
        for kind, item_ids in by_section.items()
    }
    target_narrative_counts = {
        "research_task": 1, "motivation": 2, "method_overview": 1, "method_details": 5,
        "training_inference": 3, "experimental_setup": 3, "main_results": 3,
        "analysis": 4, "conclusion_limitations": 2,
    }
    parent_kind: str | None = None
    for source_section in ir.get("sections", []):
        title = str(source_section.get("title") or "")
        if "@" in title or re.search(r"\barxiv\s*:", title, re.I) or re.fullmatch(
            r"\s*(time|accuracy|score|loss|epoch|frequency|percentage)\s*(\([^)]*\))?\s*", title, re.I
        ):
            continue
        title_tokens = re.findall(r"\S+", title)
        if title_tokens and sum(bool(re.fullmatch(r"[-+]?\d+(?:\.\d+)?", token)) for token in title_tokens) / len(title_tokens) > 0.5:
            continue
        kind = _source_section_bucket(title)
        top_level = bool(re.match(r"^\s*\d+\s+", title)) or int(source_section.get("level") or 1) == 1
        if top_level:
            if kind:
                parent_kind = kind
            elif parent_kind in {"research_task", "existing_methods", "motivation"}:
                parent_kind = "method_details"
            kind = kind or parent_kind
        elif not kind:
            kind = "method_details" if parent_kind == "method_overview" else parent_kind
        if not kind or kind == "existing_methods" or narrative_counts.get(kind, 0) >= target_narrative_counts.get(kind, 1):
            continue
        candidates = sorted(
            evidence_by_section.get(str(source_section.get("id") or ""), []),
            key=lambda item: (int((item.get("locator") or {}).get("page") or 0), str(item.get("id") or "")),
        )
        candidates = [item for item in candidates if len(str(item.get("verbatim_text") or "").strip()) >= 35][:3]
        if not candidates:
            continue
        evidence_ids = [str(item["id"]) for item in candidates]
        content = _item(
            f"content:section-brief:{kind.replace('_', '-')}:{len(by_section[kind])+1:02d}",
            "narrative", title or "论文内容", kind,
            evidence_ids, evidence_ids,
            body=" ".join(str(item.get("verbatim_text") or "") for item in candidates),
            generation="source_grounded",
        )
        items.append(content)
        by_section[kind].append(content["id"])
        narrative_counts[kind] = narrative_counts.get(kind, 0) + 1

    page_sections: dict[int, str] = {}
    for section in ir.get("sections", []):
        for page in range(int(section["page_start"]), int(section["page_end"]) + 1):
            page_sections.setdefault(page, str(section.get("title", "")))

    for index, visual in enumerate(sorted(ir.get("visuals", []), key=lambda row: row["id"]), 1):
        if visual.get("kind") == "paper_original" and not visual.get("asset_path"):
            continue
        text = f"{visual.get('title', '')} {visual.get('caption', '')} {page_sections.get((visual.get('locator') or {}).get('page'), '')}"
        section = "main_results" if _EXPERIMENT.search(text) else "method_overview"
        evidence_ids = _evidence_closure(visual["id"], entities, types)
        visual_title = visual.get("title") or visual.get("caption") or "论文图"
        content = _item(f"content:visual:{index:04d}", "paper_visual", _semantic_title(str(visual_title)), section, [visual["id"]], evidence_ids, visual_id=visual["id"])
        items.append(content)
        by_section[section].append(content["id"])

    for index, formula in enumerate(sorted(ir.get("formulas", []), key=lambda row: row["id"]), 1):
        evidence_ids = _evidence_closure(formula["id"], entities, types)
        # The equation itself is rendered as MathML below the heading. Keep the
        # heading short so raw extraction/LaTeX is not duplicated above it.
        formula_title = f"公式 {index}"
        locator_page = int((formula.get("locator") or {}).get("page") or 0)
        formula_context = page_sections.get(locator_page, "")
        formula_section = "training_inference" if _TRAINING_SECTION.search(formula_context) else "method_details"
        content = _item(f"content:formula:{index:04d}", "formula", formula_title, formula_section, [formula["id"], *formula.get("variable_ids", [])], evidence_ids, formula_id=formula["id"], variable_ids=list(formula.get("variable_ids", [])))
        items.append(content)
        by_section[formula_section].append(content["id"])

    for index, table in enumerate(sorted(ir.get("tables", []), key=lambda row: row["id"]), 1):
        if not table.get("asset_path") and not (table.get("columns") and table.get("rows")):
            continue
        evidence_ids = _evidence_closure(table["id"], entities, types)
        content = _item(f"content:table:{index:04d}", "table", _semantic_title(str(table.get("caption") or "实验表")), "main_results", [table["id"]], evidence_ids, table_id=table["id"])
        items.append(content)
        by_section["main_results"].append(content["id"])

    for index, citation in enumerate(sorted(ir.get("citations", []), key=lambda row: row["id"]), 1):
        evidence_ids = _evidence_closure(citation["id"], entities, types)
        if not evidence_ids:
            continue
        citation_title = citation.get("title") or citation.get("raw_reference") or "相关工作"
        content = _item(f"content:citation:{index:04d}", "citation", _semantic_title(str(citation_title)), "existing_methods", [citation["id"], *evidence_ids], evidence_ids, citation_id=citation["id"])
        if citation.get("verification") != "verified" or not citation.get("external_url"):
            content["support_status"] = "pending"
        items.append(content)
        by_section["existing_methods"].append(content["id"])

    digest: list[str] = []
    digest_slots = (
        (("research_task", "motivation"), "本文研究的问题"),
        (("method_overview", "method_details", "training_inference"), "本文的方法"),
        (("main_results", "analysis"), "核心实验"),
    )
    for candidate_kinds, title in digest_slots:
        candidate = None
        for kind in candidate_kinds:
            candidates = [
                item_id for item_id in by_section[kind]
                if next(row for row in items if row["id"] == item_id)["content_type"] == "narrative"
            ]
            candidate = max(
                candidates,
                key=lambda item_id: _digest_score(title, next(row for row in items if row["id"] == item_id)),
                default=None,
            )
            if candidate:
                break
        if candidate:
            source_item = next(item for item in items if item["id"] == candidate)
            digest_item = copy.deepcopy(source_item)
            digest_item.update({
                "id": f"content:digest:{len(digest)+1:04d}",
                "title": title,
                "primary_section": "one_minute_read",
            })
            items.append(digest_item)
            digest.append(digest_item["id"])
    by_section["one_minute_read"] = digest

    sections: list[dict[str, Any]] = []
    omissions: list[dict[str, str]] = []
    for kind, title in SECTION_ORDER:
        fallback = kind in fallback_sections or (kind == "one_minute_read" and len(by_section[kind]) < 3 and bool(by_section[kind]))
        status, reason = _section_status(kind, by_section[kind], fallback)
        sections.append({"id": f"content-section:{kind.replace('_', '-')}", "kind": kind, "title": title, "status": status, "item_ids": by_section[kind], "fallback_reason": reason})
        if status == "unavailable":
            omissions.append({"section_kind": kind, "reason": reason or "没有已验证内容。"})

    visual_requests: list[dict[str, Any]] = []
    blocks = {block["id"]: block for page in ir.get("pages", []) for block in page.get("blocks", [])}
    for item in items:
        if item["content_type"] not in {"paper_visual", "table"}:
            continue
        if item["content_type"] == "paper_visual":
            visual = entities[item["visual_id"]]
            original = visual.get("kind") == "paper_original"
            intent = "original_figure" if original else ("relation" if visual.get("visual_type") == "network" else "process")
            caption_policy = "show_official_once" if original else "label_derived_only"
            interaction = "source_jump_only" if original else "body_evidence_only"
        elif item["content_type"] == "table":
            table = entities[item["table_id"]]
            discussed = [eid for cell in table.get("discussed_cells", []) for eid in cell.get("evidence_ids", [])]
            body_only = bool(discussed) and all(blocks.get(entities[eid]["locator"]["block_ids"][0], {}).get("role") in _BODY_ROLES for eid in discussed if eid in entities)
            intent, caption_policy, interaction = "original_table", "show_official_once", "body_evidence_only" if body_only else "static"
            request_evidence_ids = discussed if body_only else item["evidence_ids"]
        else:
            intent, caption_policy, interaction = "relation", "label_derived_only", "body_evidence_only"
        if item["content_type"] != "table":
            request_evidence_ids = item["evidence_ids"]
        section_id = next(section["id"] for section in sections if section["kind"] == item["primary_section"])
        visual_requests.append({"id": f"visual-request:{len(visual_requests)+1:04d}", "section_id": section_id, "purpose": item["title"], "semantic_intent": intent, "candidate_entity_ids": [source for source in item["source_ids"] if types.get(source) in {"visual", "table", "claim"}], "derived_from": item["source_ids"], "evidence_ids": request_evidence_ids, "caption_policy": caption_policy, "interaction_policy": interaction})

    section_ids = {section["kind"]: section["id"] for section in sections}
    placements = []
    for item in items:
        placements.append({"source_id": item["id"], "primary_section_id": section_ids[item["primary_section"]], "reuse_policy": "primary_only", "secondary_section_ids": []})

    facet_sections = {
        "research_question": "research_task", "core_approach": "method_overview",
        "key_finding": "main_results", "formula": "method_details",
        "experiment": "experimental_setup", "related_context": "existing_methods",
    }
    coverage = []
    for facet, kind in facet_sections.items():
        section = next(row for row in sections if row["kind"] == kind)
        sources = [source for item in items if item["id"] in section["item_ids"] for source in item["source_ids"]]
        coverage.append({"facet": facet, "status": "covered" if section["status"] == "complete" else ("fallback" if section["status"] == "fallback" else "missing"), "source_ids": list(dict.fromkeys(sources)), "reason": section["fallback_reason"]})

    source_ids = list(dict.fromkeys(source for item in items for source in item["source_ids"] if source in entities))
    source_index = [{"source_id": source_id, "source_type": types[source_id], "evidence_ids": _evidence_closure(source_id, entities, types)} for source_id in source_ids]
    coverage.append({"facet": "source_traceability", "status": "covered" if source_index else "missing", "source_ids": source_ids, "reason": None if source_index else "没有可追溯来源。"})
    critical_missing = any(row["facet"] in {"research_question", "core_approach", "source_traceability"} and row["status"] == "missing" for row in coverage)
    warnings = [entry["reason"] for entry in omissions]
    paper = ir["paper"]
    paper_metadata = {
        "title": paper["title"], "authors": list(paper.get("authors", [])), "affiliations": [],
        "venue": paper.get("venue"), "published_at": str(paper.get("year")) if paper.get("year") else None,
        "doi": paper.get("doi"), "arxiv_id": paper.get("arxiv_id"),
    }
    return {"schema_version": SCHEMA_VERSION, "stage_version": STAGE_VERSION, "paper_id": ir["paper"]["id"], "source_ir_sha256": _digest(ir), "paper_metadata": paper_metadata, "sections": sections, "items": items, "visual_requests": visual_requests, "placement_ledger": placements, "coverage": coverage, "omissions": omissions, "source_index": source_index, "review": {"status": "needs_review" if critical_missing else "passed", "warnings": warnings, "overlay_applied": False, "reviewer": None, "reason": None}}


def _apply_overlay(plan: dict[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    if set(overlay) - {"approval", "sections", "items"}:
        raise PlanningError("overlay may contain only approval, sections, and items")
    approval = overlay.get("approval", {})
    if approval.get("status") != "approved" or approval.get("base_ir_sha256") != plan["source_ir_sha256"] or approval.get("base_plan_sha256") != _digest(plan):
        raise PlanningError("overlay requires approved, hash-bound human review")
    if not approval.get("reviewer") or not approval.get("reason"):
        raise PlanningError("overlay approval requires reviewer and reason")
    result = copy.deepcopy(plan)
    sections = {row["id"]: row for row in result["sections"]}
    for patch in overlay.get("sections", []):
        section_id = patch.get("id")
        if section_id not in sections or set(patch) - {"id", "title", "item_ids"}:
            raise PlanningError("overlay section may only reorder/select known sections and items")
        sections[section_id].update(copy.deepcopy(dict(patch)))
    items = {row["id"]: row for row in result["items"]}
    for patch in overlay.get("items", []):
        item_id = str(patch.get("id", ""))
        existing = items.get(item_id)
        if not existing:
            raise PlanningError("overlay cannot add content without a baseline placement")
        allowed = {"id", "title", "body"} if existing["content_type"] == "narrative" else {"id", "title"}
        if set(patch) - allowed:
            raise PlanningError("overlay cannot alter source bindings or immutable IR-backed data")
        merged = copy.deepcopy(existing)
        merged.update(copy.deepcopy(dict(patch)))
        items[item_id] = merged
    result["items"] = list(items.values())
    result["review"] = {"status": "passed", "warnings": result["review"]["warnings"], "overlay_applied": True, "reviewer": approval["reviewer"], "reason": approval["reason"]}
    return result


def validate_content_plan(ir: Mapping[str, Any], plan: Mapping[str, Any], project_root: Path | None = None) -> list[str]:
    root = project_root or Path(__file__).resolve().parents[2]
    schema = json.loads((root / "schemas" / "content-plan.schema.json").read_text(encoding="utf-8"))
    errors = [f"{'/'.join(map(str, error.path))}: {error.message}" for error in Draft202012Validator(schema).iter_errors(plan)]
    entities, types = _indexes(ir)
    item_map = {item.get("id"): item for item in plan.get("items", [])}
    section_ids = {section.get("id") for section in plan.get("sections", [])}
    if plan.get("source_ir_sha256") != _digest(ir):
        errors.append("source_ir_sha256 does not match Paper IR")
    if len({section.get("kind") for section in plan.get("sections", [])}) != len(SECTION_ORDER):
        errors.append("content sections must contain each required kind exactly once")
    for section in plan.get("sections", []):
        for item_id in section.get("item_ids", []):
            if item_id not in item_map:
                errors.append(f"{section.get('id')}: unknown item {item_id}")
    placed_item_ids = [item_id for section in plan.get("sections", []) for item_id in section.get("item_ids", [])]
    if len(placed_item_ids) != len(set(placed_item_ids)):
        errors.append("content sections must not render the same item more than once")
    normalized: set[str] = set()
    for item in item_map.values():
        unknown = [source for source in item.get("source_ids", []) if source not in entities]
        if unknown:
            errors.append(f"{item.get('id')}: unknown source ids {unknown}")
        if any(types.get(evidence_id) != "evidence" for evidence_id in item.get("evidence_ids", [])):
            errors.append(f"{item.get('id')}: evidence_ids must reference Evidence")
        if item.get("content_type") == "narrative":
            if not item.get("body") or not item.get("evidence_ids"):
                errors.append(f"{item.get('id')}: narrative content must be evidence-bound")
            key = " ".join(str(item.get("body", "")).casefold().split())
            reusable_story = str(item.get("id", "")).startswith(("content:digest:", "content:section-brief:"))
            if key in normalized and not reusable_story:
                errors.append(f"{item.get('id')}: duplicate claim content")
            normalized.add(key)
            claim_id = item.get("claim_id")
            if claim_id and not set(item.get("evidence_ids", [])).issubset(set(_evidence_closure(claim_id, entities, types))):
                errors.append(f"{item.get('id')}: content evidence is outside its claim")
            if item.get("generation") == "llm":
                compact_body = re.sub(r"[^\w]+", "", re.sub(r"\[[0-9,\s-]+\]", "", str(item.get("body") or "").casefold()))
                compact_evidence = {
                    re.sub(r"[^\w]+", "", re.sub(r"\[[0-9,\s-]+\]", "", str(entities[evidence_id].get("verbatim_text") or "").casefold()))
                    for evidence_id in item.get("evidence_ids", [])
                    if evidence_id in entities
                }
                if compact_body in compact_evidence:
                    errors.append(f"{item.get('id')}: narrative duplicates source evidence")
        elif item.get("body"):
            errors.append(f"{item.get('id')}: only narrative items may carry body text")
    for request in plan.get("visual_requests", []):
        if request.get("section_id") not in section_ids:
            errors.append(f"{request.get('id')}: unknown section")
        if request.get("interaction_policy") == "body_evidence_only":
            for evidence_id in request.get("evidence_ids", []):
                evidence = entities.get(evidence_id, {})
                block_ids = evidence.get("locator", {}).get("block_ids", [])
                blocks = {block["id"]: block for page in ir.get("pages", []) for block in page.get("blocks", [])}
                if not block_ids or any(blocks.get(block_id, {}).get("role") != "body" for block_id in block_ids):
                    errors.append(f"{request.get('id')}: interactive visual uses non-body Evidence")
                    break
    primary_sources = [row.get("source_id") for row in plan.get("placement_ledger", [])]
    if len(primary_sources) != len(set(primary_sources)):
        errors.append("placement ledger contains duplicate primary items")
    return errors


def build_content_plan(ir: Mapping[str, Any], overlay: Mapping[str, Any] | None = None, *, project_root: Path | None = None) -> dict[str, Any]:
    root = project_root or Path(__file__).resolve().parents[2]
    ir_errors = validate_ir(dict(ir), root)
    if ir_errors:
        raise PlanningError("invalid Paper IR: " + "; ".join(ir_errors))
    plan = _build_base(ir)
    if overlay:
        plan = _apply_overlay(plan, overlay)
    errors = validate_content_plan(ir, plan, root)
    if errors:
        raise PlanningError("invalid content plan: " + "; ".join(errors))
    return plan


def plan_ir_file(ir_path: Path | str, *, output_path: Path | str, overlay_path: Path | str | None = None, project_root: Path | None = None) -> dict[str, Any]:
    ir = json.loads(Path(ir_path).read_text(encoding="utf-8"))
    overlay = json.loads(Path(overlay_path).read_text(encoding="utf-8")) if overlay_path else None
    plan = build_content_plan(ir, overlay, project_root=project_root)
    atomic_write_json(Path(output_path), plan)
    return plan
