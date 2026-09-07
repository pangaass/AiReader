"""Build a paper-agnostic Related Work graph from Paper IR.

The graph stores references to current-paper Evidence, not copied or generated
descriptions. External records may fill bibliographic fields but may never add
claims about what the current paper says.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator

from ..artifacts import atomic_write_json
from ..validation import validate_ir


SCHEMA_VERSION = "2.0.0"
STAGE_VERSION = "2.0.0"

_YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
_URL_RE = re.compile(r"https?://[^\s)>\]}]+", re.I)
_DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.I)
_ARXIV_RE = re.compile(r"(?<!\d)(\d{4}\.\d{4,5})(?:v\d+)?(?!\d)", re.I)
_LEADING_INDEX_RE = re.compile(r"^\s*(?:\[\d+\]|\d+[.)])\s*")

_RELATIONS: tuple[tuple[str, str, str, re.Pattern[str]], ...] = (
    (
        "same_problem",
        "研究问题来源",
        "这些前作界定了本文继续处理的任务、现象或研究问题。",
        re.compile(r"\b(?:previous|prior|earlier|existing|recent)(?:\s+[a-z-]+){0,3}\s+(?:work|approach|method|model|system)s?\b|\b(?:one|a|prior|previous) stud(?:y|ies)\b[^.]{0,100}\b(?:found|showed|reported)\b|\bstud(?:y|ies)\b[^.]{0,100}\b(?:found|showed|reported)\b|\b(?:another|an alternative) (?:approach|method)\b|\b(?:numerous|several|many) (?:efforts|approaches|methods|systems)\b|\bstate[- ]of[- ]the[- ]art\b|\b(?:has|have) (?:[a-z]+ )?been (?:proposed|used|applied|studied|developed)\b|\b(?:system|tool|technique|method|approach)s?\b[^.]{0,90}\b(?:introduced|used|provided|supported)\b|\b(?:address|solve|tackle)(?:es|ed|ing)? (?:the |this |a )?(?:same |similar |related )?(?:problem|task|challenge|goal)\b", re.I),
    ),
    (
        "improvement_comparison",
        "相关工作与差异",
        "这些替代路线、基线或已知局限解释了本文的设计选择。",
        re.compile(r"\b(unlike|in contrast|compared? (?:to|with)|comparison|baseline|outperform|surpass|better than|advantages? over|alternative (?:method|approach)|not easy|challenges? of|(?:achieved|obtained|delivered|showed)\s+(?:significant(?:ly)?\s+)?improvements?|limitation|limited by|drawback|shortcoming|whereas|rather than|instead(?: of)?|target(?:s|ed|ing)?[^.]{0,100}\bnot\b)\b|\b(?:we|our|this (?:work|paper|method)|[A-Z][A-Z0-9-]{1,9}\s+(?:keeps|uses|adopts|extends))\b[^.]{0,140}\bbut\b", re.I),
    ),
    (
        "foundation_inheritance",
        "方法来源",
        "这些工作提供了本文采用、扩展或重新组织的方法基础。",
        re.compile(r"\b(?:we|our (?:method|model|approach|system))\s+(?:adopt|employ|follow|extend|adapt|build)|\bwe\s+use\b[^.]{0,100}\b(?:method|model|architecture|algorithm|technique|encoding|embedding|connection|layer|objective|loss|procedure)s?\b|\b(?:was|were|is|are)\s+(?:encoded|implemented|initialized|trained)\s+(?:using|with|by)\b|\b(?:split|encode)d?\s+(?:tokens|sentences|inputs)\b[^.]{0,100}\b(?:using|into|with)\b|\b(?:based|built) (?:on|upon)\b|\binspir(?:ed|ation) (?:by|from)\b|\bextend(?:s|ed|ing)?\b[^.]{0,100}\bprior\b|\b(?:introduced|proposed) by\b|\b(?:as in|as described in|similar to)\b", re.I),
    ),
    (
        "data_evaluation",
        "数据与评测来源",
        "这些工作影响本文的数据、指标、基线或评测协议。",
        re.compile(r"\b(?:we|our (?:method|model|approach|system))\s+(?:use|evaluate|train|test|report)[^.]{0,120}\b(?:dataset|corpus|benchmark|metric|evaluation protocol|test set|training data|BLEU|ROUGE|perplexity|accuracy|F1)\b|\b(?:dataset|corpus|benchmark|metric|evaluation protocol|test set|training data)\b[^.]{0,100}\b(?:introduced|proposed|released|from)\b", re.I),
    ),
)


class RelatedWorkError(ValueError):
    """Raised when a related-work plan violates the source boundary."""


def _digest(value: Mapping[str, Any]) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _clean_url(value: object) -> str | None:
    url = str(value or "").strip().rstrip(".,;")
    parsed = urllib.parse.urlparse(url)
    return url if parsed.scheme in {"http", "https"} and "." in (parsed.hostname or "") else None


def _reference_metadata(citation: Mapping[str, Any]) -> dict[str, Any]:
    raw = str(citation.get("raw_reference") or "").strip()
    body = _LEADING_INDEX_RE.sub("", raw)
    body = _URL_RE.sub("", body)
    year_match = list(_YEAR_RE.finditer(body))
    year = citation.get("year")
    if year is None and year_match:
        year = int(year_match[-1].group(1))
    parts = [part.strip(" ,;.") for part in re.split(r"\.\s+", body) if part.strip(" ,;.")]
    title = citation.get("title")
    authors = [str(item) for item in citation.get("authors", [])]
    title_index: int | None = None
    if title is None and len(parts) >= 2:
        usable = [
            (index, part)
            for index, part in enumerate(parts)
            if not _YEAR_RE.fullmatch(part) and not re.fullmatch(r"[A-Z]", part)
        ]
        if usable:
            title_index, title = max(usable[1:] or usable, key=lambda item: (len(item[1].split()), len(item[1])))
    if not authors and parts:
        author_text = ". ".join(parts[:title_index] if title_index else parts[:1])
        authors = [item.strip() for item in re.split(r",|\band\b", author_text) if item.strip()]
    url = _clean_url(citation.get("external_url"))
    if url is None:
        match = _URL_RE.search(raw)
        if match:
            url = _clean_url(match.group(0))
        else:
            doi = _DOI_RE.search(raw)
            if doi:
                url = f"https://doi.org/{doi.group(0).rstrip('.,;')}"
            else:
                arxiv = _ARXIV_RE.search(raw)
                if arxiv:
                    url = f"https://arxiv.org/abs/{arxiv.group(1)}"
    return {"title": title, "authors": authors, "year": int(year) if year is not None else None, "url": url, "doi": None, "venue": None}


def _relation_for(text: str) -> tuple[str, str, str] | None:
    if _is_table_like(text):
        return None
    text = re.sub(r"(?<=[A-Za-z])-\s+(?=[A-Za-z])", "", text)
    priority = {"foundation_inheritance": 0, "data_evaluation": 1, "improvement_comparison": 2, "same_problem": 3}
    for key, label, description, pattern in sorted(_RELATIONS, key=lambda item: priority[item[0]]):
        if pattern.search(text):
            return key, label, description
    return None


def _is_table_like(text: str) -> bool:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 10:
        return False
    short_numeric = sum(bool(re.fullmatch(r"[-–—]?\d+(?:\.\d+)?%?", line)) for line in lines)
    median = sorted(len(line) for line in lines)[len(lines) // 2]
    return short_numeric >= 3 and median < 24


def _citation_context(text: str, citation: Mapping[str, Any]) -> str | None:
    """Return the smallest prose context that explicitly contains the citation."""

    normalized = re.sub(r"(?<=[A-Za-z])-\s+(?=[A-Za-z])", "", text)
    normalized = " ".join(normalized.split())
    sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z])", normalized)
    raw_reference = str(citation.get("raw_reference") or "")
    number_match = re.match(r"^\s*\[(\d{1,4})\]", raw_reference)
    marker_patterns: list[re.Pattern[str]] = []
    if number_match:
        wanted = int(number_match.group(1))
        for sentence in sentences:
            for marker in re.finditer(r"\[([\d,\s-]+)\]", sentence):
                values: set[int] = set()
                for part in re.split(r"\s*,\s*", marker.group(1)):
                    if "-" in part:
                        left, right = (int(value.strip()) for value in part.split("-", 1))
                        if 0 <= right - left <= 25:
                            values.update(range(left, right + 1))
                    elif part.strip().isdigit():
                        values.add(int(part))
                if wanted in values:
                    marker_patterns.append(re.compile(re.escape(marker.group(0))))
    else:
        year_match = re.search(r"\.\s*((?:19|20)\d{2})([a-z]?)\.\s+", raw_reference, re.I)
        author_text = raw_reference[: year_match.start()] if year_match else ""
        first_author = author_text.split(",", 1)[0].strip()
        tokens = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ'’-]+", first_author)
        if tokens and year_match:
            surname = tokens[-1]
            year = year_match.group(1) + year_match.group(2)
            suffix = re.escape(year[4:]) if len(year) > 4 else r"[a-z]?"
            marker_patterns.append(re.compile(rf"\b{re.escape(surname)}\b[^();]{{0,60}}?\b{year[:4]}{suffix}\b", re.I))
    selected_indices = [
        index for index, sentence in enumerate(sentences)
        if any(pattern.search(sentence) for pattern in marker_patterns)
    ]
    if not selected_indices:
        return None
    # A following/current-paper contrast can describe the preceding group of
    # citations collectively, as in "OurMethod instead ...". Only the
    # immediately following sentence is eligible; a contrast elsewhere in the
    # paragraph must not be attached to this citation.
    contrast = re.compile(r"\b(?:we|our|this (?:work|paper|method)|[A-Z][A-Z0-9-]{1,9})\s+(?:instead|keeps|differs|extends|uses|adopts)\b", re.I)
    selected: list[str] = []
    for index in selected_indices:
        selected.append(sentences[index])
        if index + 1 < len(sentences) and contrast.search(sentences[index + 1]):
            selected.append(sentences[index + 1])
    return " ".join(dict.fromkeys(selected))


def _relation_evidence(
    evidence_ids: list[str],
    evidence: Mapping[str, Mapping[str, Any]],
    section_titles: Mapping[str, str],
    citation: Mapping[str, Any],
) -> tuple[tuple[str, str, str], list[str]] | None:
    candidates: list[tuple[int, int, int, tuple[str, str, str], str]] = []
    for order, evidence_id in enumerate(evidence_ids):
        item = evidence[evidence_id]
        text = str(item.get("verbatim_text") or "")
        if _is_table_like(text):
            continue
        context = _citation_context(text, citation)
        if context is None:
            continue
        relation = _relation_for(context)
        if relation is None:
            continue
        title = section_titles.get(str(item.get("section_id") or ""), "")
        if re.search(r"\b(related work|background|introduction|prior work|literature)\b", title, re.I):
            section_rank = 3
        elif re.search(r"\b(method|approach|model|architecture)\b", title, re.I):
            section_rank = 2
        else:
            section_rank = 1
        candidates.append((section_rank, -len(context), -order, relation, evidence_id))
    # Do not join unrelated Evidence records to manufacture a relation. A
    # displayed edge needs one continuous Evidence item that contains both the
    # citation marker and the supported semantic relation.
    if not candidates:
        return None
    _, _, _, selected, selected_id = max(candidates, key=lambda item: (item[0], item[1], item[2]))
    return selected, [selected_id]


def _unverified() -> dict[str, Any]:
    return {"status": "unverified", "checked_fields": [], "conflicts": [], "sources": []}


def build_related_work_plan(ir: Mapping[str, Any], *, project_root: Path | None = None) -> dict[str, Any]:
    """Build clusters and edges using only citations and Evidence in ``ir``."""

    root = project_root or Path(__file__).resolve().parents[2]
    ir_value = dict(ir)
    ir_errors = validate_ir(ir_value, root)
    if ir_errors:
        raise RelatedWorkError("Paper IR failed validation: " + "; ".join(ir_errors[:5]))

    evidence = {item["id"]: item for item in ir_value.get("evidence", [])}
    section_titles = {str(item["id"]): str(item.get("title") or "") for item in ir_value.get("sections", [])}
    block_roles = {
        block["id"]: block.get("role")
        for page in ir_value.get("pages", [])
        for block in page.get("blocks", [])
    }
    paper = ir_value["paper"]
    current_id = str(paper["id"])
    nodes: list[dict[str, Any]] = []
    clusters_by_id: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []
    warnings: list[str] = []
    related_block_ids = {
        block_id
        for section in ir_value.get("sections", [])
        if re.search(r"\b(related work|background|prior work|literature)\b", str(section.get("title") or ""), re.I)
        for block_id in section.get("block_ids", [])
    }
    related_body_evidence = {
        item_id for item_id, item in evidence.items()
        if any(block_id in related_block_ids for block_id in item.get("locator", {}).get("block_ids", []))
        and all(block_roles.get(block_id) == "body" for block_id in item.get("locator", {}).get("block_ids", []))
    }

    for citation in ir_value.get("citations", []):
        evidence_ids = [
            item for item in citation.get("cited_in_evidence_ids", [])
            if item in evidence
            and evidence[item].get("provenance", {}).get("kind") == "paper_verbatim"
            and all(block_roles.get(block_id) == "body" for block_id in evidence[item].get("locator", {}).get("block_ids", []))
        ]
        metadata = _reference_metadata(citation)
        if not metadata["title"]:
            warnings.append(f"{citation['id']}: omitted from display because no reliable paper title was extracted")
            continue
        if not evidence_ids:
            warnings.append(f"{citation['id']}: omitted because the current paper does not discuss it in body Evidence")
            continue
        relation_match = _relation_evidence(evidence_ids, evidence, section_titles, citation)
        if relation_match is None:
            warnings.append(f"{citation['id']}: omitted because no explicit Related Work relation was expressed")
            continue
        relation, relation_evidence_ids = relation_match
        relation_key, relation_label, relation_description = relation
        node_id = f"related:{citation['id']}"
        cluster_id = f"cluster:{relation_key}"
        nodes.append(
            {
                "id": node_id,
                "citation_id": citation["id"],
                "raw_reference": citation["raw_reference"],
                "metadata": metadata,
                "description": {
                    "kind": "current_paper_verbatim",
                    "evidence_ids": relation_evidence_ids,
                    "display_policy": "dereference_verbatim_text",
                },
                "cluster_ids": [cluster_id],
                "verification": _unverified(),
            }
        )
        cluster = clusters_by_id.setdefault(
            cluster_id,
            {"id": cluster_id, "label": relation_label, "description": relation_description, "member_node_ids": [], "basis_evidence_ids": [], "basis": "current_paper_context"},
        )
        cluster["member_node_ids"].append(node_id)
        cluster["basis_evidence_ids"] = list(dict.fromkeys([*cluster["basis_evidence_ids"], *relation_evidence_ids]))
        edges.append(
            {
                "id": f"edge:relation:{len(edges) + 1:04d}",
                "source_id": node_id,
                "target_id": current_id,
                "kind": relation_key,
                "cluster_id": cluster_id,
                "basis_evidence_ids": relation_evidence_ids,
                "verification": "verified",
            }
        )
        if metadata["url"] is None:
            warnings.append(f"{citation['id']}: displayed without an external link; use the current-paper evidence entry")

    current_metadata = {
        "title": paper.get("title"),
        "authors": list(paper.get("authors", [])),
        "year": paper.get("year"),
        "url": _clean_url(paper.get("external_url")),
        "doi": paper.get("doi"),
        "venue": paper.get("venue"),
    }
    displayable = [node for node in nodes if node["metadata"]["title"] and node["description"]["evidence_ids"]]
    review_items: list[dict[str, Any]] = []
    if related_body_evidence and not displayable:
        warning = "BLOCKER: Related Work正文存在，但没有同时具备可靠标题与正文 Evidence 的节点"
        warnings.append(warning)
        review_items.append({"kind": "related_work_empty", "severity": "blocker", "evidence_ids": sorted(related_body_evidence), "reason": warning})
    result = {
        "schema_version": SCHEMA_VERSION,
        "stage_version": STAGE_VERSION,
        "paper_id": current_id,
        "source_ir_sha256": _digest(ir_value),
        "policy": {
            "description_source": "current_paper_verbatim_evidence_only",
            "external_data_scope": "metadata_and_relationship_verification_only",
            "edge_meaning": "four_evidence_grounded_relation_families",
            "node_size_meaning": "uniform_no_citation_count_encoding",
            "category_quota": "none",
            "graph_purpose": "evidence_grounded_research_provenance",
            "edge_direction": "source_to_current_paper",
            "layout_direction": "left_to_right_terminal",
        },
        "current_paper": {"id": current_id, "metadata": current_metadata, "verification": _unverified()},
        "nodes": nodes,
        "clusters": [clusters_by_id[f"cluster:{key}"] for key, _, _, _ in _RELATIONS if f"cluster:{key}" in clusters_by_id],
        "edges": edges,
        "review": {"status": "needs_review" if warnings or any(node["verification"]["status"] != "verified" for node in nodes) else "passed", "warnings": warnings, "needs_human_review": review_items},
    }
    errors = validate_related_work_plan(ir_value, result, root)
    if errors:
        raise RelatedWorkError("Related Work plan failed validation: " + "; ".join(errors[:5]))
    return result


def validate_related_work_plan(ir: Mapping[str, Any], plan: Mapping[str, Any], project_root: Path | None = None) -> list[str]:
    root = project_root or Path(__file__).resolve().parents[2]
    schema = json.loads((root / "schemas" / "related-work.schema.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    errors = [f"{'/'.join(str(part) for part in error.path)}: {error.message}" for error in sorted(validator.iter_errors(plan), key=lambda error: list(error.path))]
    evidence = {item["id"]: item for item in ir.get("evidence", [])}
    citations = {item["id"]: item for item in ir.get("citations", [])}
    node_ids = {item["id"] for item in plan.get("nodes", [])}
    cluster_ids = {item["id"] for item in plan.get("clusters", [])}
    block_roles = {block["id"]: block.get("role") for page in ir.get("pages", []) for block in page.get("blocks", [])}
    allowed_targets = {plan.get("paper_id")}
    relation_kinds = {key for key, _, _, _ in _RELATIONS}
    edges_by_source: dict[str, list[Mapping[str, Any]]] = {}
    for edge in plan.get("edges", []):
        edges_by_source.setdefault(str(edge.get("source_id")), []).append(edge)
    for node in plan.get("nodes", []):
        citation = citations.get(node.get("citation_id"))
        if citation is None:
            errors.append(f"{node.get('id')}: unknown citation")
        description_ids = list(node.get("description", {}).get("evidence_ids", []))
        supported_kinds: dict[str, str] = {}
        for evidence_id in description_ids:
            item = evidence.get(evidence_id)
            if item is None or item.get("provenance", {}).get("kind") != "paper_verbatim":
                errors.append(f"{node.get('id')}: description is not current-paper verbatim Evidence")
            elif any(block_roles.get(block_id) != "body" for block_id in item.get("locator", {}).get("block_ids", [])):
                errors.append(f"{node.get('id')}: description Evidence is not body text")
            if citation and evidence_id not in citation.get("cited_in_evidence_ids", []):
                errors.append(f"{node.get('id')}: description Evidence is not bound to its citation")
            if citation and item is not None:
                context = _citation_context(str(item.get("verbatim_text") or ""), citation)
                relation = _relation_for(context) if context else None
                if relation:
                    supported_kinds[evidence_id] = relation[0]
        node_edges = edges_by_source.get(str(node.get("id")), [])
        if len(node_edges) != 1:
            errors.append(f"{node.get('id')}: Related Work node must have exactly one semantic edge")
        elif not any(kind == node_edges[0].get("kind") for kind in supported_kinds.values()):
            errors.append(f"{node.get('id')}: edge kind is not explicitly supported by its description Evidence")
        elif not set(node_edges[0].get("basis_evidence_ids", [])).issubset(set(description_ids)):
            errors.append(f"{node.get('id')}: edge basis is outside its description Evidence")
    for edge in plan.get("edges", []):
        if edge.get("source_id") not in node_ids or edge.get("target_id") not in allowed_targets:
            errors.append(f"{edge.get('id')}: edge endpoint is unknown")
        if edge.get("kind") not in relation_kinds:
            errors.append(f"{edge.get('id')}: unsupported Related Work relation")
        for evidence_id in edge.get("basis_evidence_ids", []):
            if evidence_id not in evidence:
                errors.append(f"{edge.get('id')}: unknown Evidence {evidence_id}")
        if edge.get("cluster_id") not in cluster_ids:
            errors.append(f"{edge.get('id')}: unknown cluster")
    for cluster in plan.get("clusters", []):
        if any(item not in node_ids for item in cluster.get("member_node_ids", [])):
            errors.append(f"{cluster.get('id')}: cluster contains unknown node")
    if plan.get("source_ir_sha256") != _digest(ir):
        errors.append("source_ir_sha256 does not match Paper IR")
    related_blocks = {
        block_id
        for section in ir.get("sections", [])
        if re.search(r"\b(related work|background|prior work|literature)\b", str(section.get("title") or ""), re.I)
        for block_id in section.get("block_ids", [])
    }
    has_related_body = any(
        any(block_id in related_blocks for block_id in item.get("locator", {}).get("block_ids", []))
        and all(block_roles.get(block_id) == "body" for block_id in item.get("locator", {}).get("block_ids", []))
        for item in evidence.values()
    )
    displayable = [node for node in plan.get("nodes", []) if node.get("metadata", {}).get("title") and node.get("description", {}).get("evidence_ids")]
    if has_related_body and not displayable:
        review = plan.get("review", {})
        blocker = any(item.get("severity") == "blocker" and item.get("kind") == "related_work_empty" for item in review.get("needs_human_review", []))
        if review.get("status") != "needs_review" or not blocker:
            errors.append("Related Work body exists but zero displayable nodes is not marked as a blocker")
    return errors


def write_related_work_plan(ir: Mapping[str, Any], output_path: Path, *, project_root: Path | None = None) -> dict[str, Any]:
    plan = build_related_work_plan(ir, project_root=project_root)
    atomic_write_json(output_path, plan)
    return plan
