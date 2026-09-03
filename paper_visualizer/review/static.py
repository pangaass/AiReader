"""Independent, read-only static checks for reviewer quality gates.

These checks intentionally do not repair upstream artifacts.  They report
cross-document invariants that JSON Schema alone cannot express, plus a small
set of properties of the final single-file HTML.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from enum import StrEnum
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..validation import validate_ir


class Severity(StrEnum):
    """Reviewer severity ordered by release impact."""

    BLOCKER = "blocker"
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True)
class ReviewIssue:
    code: str
    severity: Severity
    message: str
    location: str | None = None


@dataclass(frozen=True)
class ReviewReport:
    issues: tuple[ReviewIssue, ...]

    @property
    def passed(self) -> bool:
        return not any(item.severity in {Severity.BLOCKER, Severity.ERROR} for item in self.issues)

    def to_dict(self) -> dict[str, Any]:
        counts = {severity.value: 0 for severity in Severity}
        for item in self.issues:
            counts[item.severity.value] += 1
        return {
            "status": "passed" if self.passed else "failed",
            "counts": counts,
            "issues": [{**asdict(item), "severity": item.severity.value} for item in self.issues],
        }


def _issue(code: str, severity: Severity, message: str, location: str | None = None) -> ReviewIssue:
    return ReviewIssue(code=code, severity=severity, message=message, location=location)


def _entity_index(ir: Mapping[str, Any]) -> tuple[dict[str, Mapping[str, Any]], dict[str, str]]:
    entities: dict[str, Mapping[str, Any]] = {}
    types: dict[str, str] = {}
    for group in ("sections", "evidence", "claims", "formulas", "variables", "visuals", "tables", "citations", "relations"):
        for item in ir.get(group, []):
            entity_id = str(item.get("id") or "")
            if entity_id:
                entities[entity_id] = item
                types[entity_id] = group[:-1] if group.endswith("s") else group
    return entities, types


def _references(
    issues: list[ReviewIssue],
    owner: str,
    values: Iterable[object],
    types: Mapping[str, str],
    expected: set[str],
    code: str,
) -> None:
    for value in values:
        target = str(value)
        actual = types.get(target)
        if actual not in expected:
            issues.append(_issue(code, Severity.BLOCKER, f"{owner} references {target!r} as {sorted(expected)}, found {actual!r}", owner))


def review_ir(ir: Mapping[str, Any], project_root: Path) -> ReviewReport:
    """Review Paper IR schema plus strict provenance and cross-reference rules."""

    issues = [
        _issue("IR_SCHEMA_OR_BASE_SEMANTICS", Severity.BLOCKER, message)
        for message in validate_ir(dict(ir), project_root)
    ]
    entities, types = _entity_index(ir)
    pages = {page.get("number"): page for page in ir.get("pages", [])}
    page_count = ir.get("source", {}).get("page_count")
    if page_count != len(pages) or set(pages) != set(range(1, len(pages) + 1)):
        issues.append(_issue("IR_PAGE_SET_MISMATCH", Severity.BLOCKER, "source.page_count and page numbers must describe exactly 1..page_count", "source.page_count"))

    block_page: dict[str, int] = {}
    block_order: dict[str, int] = {}
    for page_number, page in pages.items():
        for block in page.get("blocks", []):
            block_id = str(block.get("id") or "")
            block_page[block_id] = int(page_number)
            block_order[block_id] = int(block.get("order", -1))

    for evidence in ir.get("evidence", []):
        evidence_id = str(evidence.get("id") or "evidence")
        locator = evidence.get("locator", {})
        page = locator.get("page")
        block_ids = [str(item) for item in locator.get("block_ids", [])]
        if any(block_page.get(block_id) != page for block_id in block_ids):
            issues.append(_issue("EVIDENCE_CROSSES_PAGE", Severity.BLOCKER, "Evidence block_ids must all belong to locator.page", evidence_id))
        orders = [block_order[block_id] for block_id in block_ids if block_id in block_order]
        if len(orders) == len(block_ids) and (orders != sorted(orders) or any(right != left + 1 for left, right in zip(orders, orders[1:]))):
            issues.append(_issue("EVIDENCE_NONCONTIGUOUS", Severity.BLOCKER, "Evidence block_ids must be consecutive in reading order", evidence_id))
        if "char_start" not in locator or "char_end" not in locator:
            issues.append(_issue("EVIDENCE_IMPRECISE_RANGE", Severity.ERROR, "Evidence must persist an explicit half-open character range", evidence_id))
        section_id = evidence.get("section_id")
        if section_id is not None and types.get(str(section_id)) != "section":
            issues.append(_issue("EVIDENCE_UNKNOWN_SECTION", Severity.ERROR, "Evidence section_id does not resolve to a section", evidence_id))

    for claim in ir.get("claims", []):
        claim_id = str(claim.get("id") or "claim")
        _references(issues, claim_id, claim.get("evidence_ids", []), types, {"evidence"}, "CLAIM_INVALID_EVIDENCE")
        provenance = claim.get("provenance", {})
        provenance_kind = provenance.get("kind")
        expected_label = {"paper_excerpt": "原文摘录", "generated_summary": "生成式总结"}.get(provenance_kind)
        if expected_label is None or claim.get("display_label") != expected_label:
            issues.append(_issue("CLAIM_PROVENANCE_LABEL_MISMATCH", Severity.BLOCKER, "Claim label must distinguish source excerpts from generated summaries", claim_id))
        compact_claim = re.sub(r"[^\w]+", "", re.sub(r"\[[0-9,\s-]+\]", "", str(claim.get("summary") or "").casefold()))
        compact_evidence = [
            re.sub(r"[^\w]+", "", re.sub(r"\[[0-9,\s-]+\]", "", str(entities.get(str(item), {}).get("verbatim_text") or "").casefold()))
            for item in claim.get("evidence_ids", [])
        ]
        if provenance_kind == "paper_excerpt" and len(compact_evidence) == 1:
            if not compact_claim or compact_claim not in compact_evidence[0]:
                issues.append(_issue("CLAIM_EXCERPT_DRIFT", Severity.BLOCKER, "Paper excerpt differs from its source Evidence", claim_id))
        elif provenance_kind == "generated_summary" and compact_claim in compact_evidence:
            issues.append(_issue("CLAIM_GENERATED_DUPLICATES_SOURCE", Severity.BLOCKER, "Generated summary duplicates source Evidence", claim_id))

    formula_exceptions: dict[str, Mapping[str, Any]] = {}
    for item in ir.get("review", {}).get("items", []):
        if not isinstance(item, Mapping) or item.get("kind") != "formula_variable_exception":
            continue
        formula_id = str(item.get("formula_id") or "")
        if formula_id:
            formula_exceptions[formula_id] = item

    formula_variables: dict[str, set[str]] = {}
    for formula in ir.get("formulas", []):
        formula_id = str(formula.get("id") or "formula")
        variable_ids = {str(item) for item in formula.get("variable_ids", [])}
        formula_variables[formula_id] = variable_ids
        _references(issues, formula_id, variable_ids, types, {"variable"}, "FORMULA_INVALID_VARIABLE")
        if not variable_ids:
            exception = formula_exceptions.get(formula_id, {})
            reason = str(exception.get("reason") or "").strip()
            rationale = str(exception.get("rationale") or "").strip()
            approved = exception.get("status") == "approved"
            if not (approved and reason == "pure_constant" and rationale):
                issues.append(_issue(
                    "FORMULA_VARIABLE_COVERAGE",
                    Severity.BLOCKER,
                    "Formula has no variable_ids; a pure-constant exception requires an approved, explicit rationale",
                    formula_id,
                ))
    for variable in ir.get("variables", []):
        variable_id = str(variable.get("id") or "variable")
        definition = variable.get("definition_evidence_id")
        _references(issues, variable_id, [definition], types, {"evidence"}, "VARIABLE_INVALID_DEFINITION")
        definition_item = entities.get(str(definition), {})
        provenance = definition_item.get("provenance", {}) if isinstance(definition_item, Mapping) else {}
        if provenance.get("kind") != "paper_verbatim" or provenance.get("verification") != "verified":
            issues.append(_issue(
                "VARIABLE_DEFINITION_UNVERIFIED",
                Severity.BLOCKER,
                "Formula variable definition must resolve to verified paper-verbatim Evidence",
                variable_id,
            ))
        for formula_id in variable.get("formula_ids", []):
            _references(issues, variable_id, [formula_id], types, {"formula"}, "VARIABLE_INVALID_FORMULA")
            if variable_id not in formula_variables.get(str(formula_id), set()):
                issues.append(_issue("VARIABLE_BACKLINK_MISMATCH", Severity.ERROR, f"{formula_id!r} does not link back to this variable", variable_id))

    for table in ir.get("tables", []):
        table_id = str(table.get("id") or "table")
        rows = table.get("rows", [])
        for cell in table.get("discussed_cells", []):
            row, column = cell.get("row"), cell.get("column")
            valid = isinstance(row, int) and isinstance(column, int) and 0 <= row < len(rows) and 0 <= column < len(rows[row])
            if not valid:
                issues.append(_issue("TABLE_CELL_OUT_OF_RANGE", Severity.BLOCKER, "Discussed cell lies outside table rows", table_id))
            _references(issues, table_id, cell.get("evidence_ids", []), types, {"evidence"}, "TABLE_CELL_INVALID_EVIDENCE")

    for visual in ir.get("visuals", []):
        visual_id = str(visual.get("id") or "visual")
        if visual.get("kind") == "derived":
            sources = visual.get("derived_from", [])
            if not sources:
                issues.append(_issue("DERIVED_VISUAL_UNGROUNDED", Severity.BLOCKER, "Derived visual has no source entities", visual_id))
            _references(issues, visual_id, sources, types, {"claim", "evidence", "formula", "variable", "table", "visual", "citation"}, "DERIVED_VISUAL_INVALID_SOURCE")
        for target in visual.get("interactive_targets", []):
            evidence_ids = target.get("evidence_ids", [])
            _references(issues, str(target.get("id") or visual_id), evidence_ids, types, {"evidence"}, "VISUAL_TARGET_INVALID_EVIDENCE")
            if target.get("interactive") and not evidence_ids:
                issues.append(_issue("VISUAL_TARGET_UNGROUNDED", Severity.BLOCKER, "Interactive target has no body Evidence", str(target.get("id") or visual_id)))

    for citation in ir.get("citations", []):
        citation_id = str(citation.get("id") or "citation")
        _references(issues, citation_id, citation.get("cited_in_evidence_ids", []), types, {"evidence"}, "CITATION_INVALID_CONTEXT")
        if citation.get("verification") == "verified" and not citation.get("external_url"):
            issues.append(_issue("CITATION_VERIFIED_WITHOUT_ENTRY", Severity.ERROR, "Verified citation has no external paper entry", citation_id))

    allowed_relations = {
        "supports": {("evidence", "claim"), ("claim", "evidence")},
        "defines": {("evidence", "variable"), ("variable", "evidence"), ("formula", "variable")},
        "mentions": {("formula", "variable"), ("evidence", "citation"), ("citation", "evidence")},
        "visualizes": {("visual", "claim"), ("visual", "evidence"), ("visual", "formula"), ("visual", "table")},
        "cites": {("claim", "citation"), ("evidence", "citation")},
        "same_lineage": {("citation", "citation")},
        "derived_from": {("visual", "claim"), ("visual", "evidence"), ("visual", "formula"), ("visual", "table")},
    }
    for relation in ir.get("relations", []):
        relation_id = str(relation.get("id") or "relation")
        source_type = types.get(str(relation.get("source_id")))
        target_type = types.get(str(relation.get("target_id")))
        if source_type is None or target_type is None:
            issues.append(_issue("RELATION_DANGLING", Severity.BLOCKER, "Relation endpoint does not resolve", relation_id))
        elif (source_type, target_type) not in allowed_relations.get(str(relation.get("type")), set()):
            issues.append(_issue("RELATION_TYPE_MISMATCH", Severity.ERROR, f"Invalid endpoint types {source_type}->{target_type}", relation_id))

    return ReviewReport(tuple(issues))


class _HTMLAuditParser(HTMLParser):
    _VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str | None], tuple[str, ...]]] = []
        self.stack: list[str] = []
        self.script_id: str | None = None
        self.script_parts: dict[str, list[str]] = {}
        self.text_parts: list[tuple[str, tuple[str, ...]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        self.tags.append((tag, values, tuple(self.stack)))
        if tag not in self._VOID_TAGS:
            self.stack.append(values.get("id") or tag)
        if tag == "script" and values.get("id"):
            self.script_id = values["id"]
            self.script_parts.setdefault(self.script_id, [])

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs), tuple(self.stack)))

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self.script_id = None
        if self.stack:
            self.stack.pop()

    def handle_data(self, data: str) -> None:
        self.text_parts.append((data, tuple(self.stack)))
        if self.script_id:
            self.script_parts[self.script_id].append(data)


def review_html(html: str, *, expected_evidence: Mapping[str, Mapping[str, Any]] | None = None) -> ReviewReport:
    """Review the rendered HTML without opening a browser or mutating files."""

    issues: list[ReviewIssue] = []
    parser = _HTMLAuditParser()
    try:
        parser.feed(html)
    except Exception as exc:  # HTMLParser normally recovers; retain a deterministic gate.
        return ReviewReport((_issue("HTML_UNPARSABLE", Severity.BLOCKER, str(exc)),))

    tags = parser.tags
    attrs_by_id = {attrs.get("id"): (tag, attrs) for tag, attrs, _ in tags if attrs.get("id")}
    if "main" not in attrs_by_id or not any(tag == "a" and attrs.get("href") == "#main" for tag, attrs, _ in tags):
        issues.append(_issue("A11Y_SKIP_LINK", Severity.ERROR, "Document needs a skip link targeting #main"))
    html_tag = next((attrs for tag, attrs, _ in tags if tag == "html"), {})
    if not html_tag.get("lang"):
        issues.append(_issue("A11Y_DOCUMENT_LANGUAGE", Severity.ERROR, "html element has no language"))

    for tag, attrs, ancestors in tags:
        if tag == "script" and attrs.get("src"):
            issues.append(_issue("HTML_EXTERNAL_RUNTIME", Severity.BLOCKER, "External script dependency is forbidden", attrs.get("src")))
        if tag == "link" and "stylesheet" in str(attrs.get("rel") or "") and attrs.get("href"):
            issues.append(_issue("HTML_EXTERNAL_RUNTIME", Severity.BLOCKER, "External stylesheet dependency is forbidden", attrs.get("href")))
        if tag in {"img", "source"} and str(attrs.get("src") or "").lower().startswith(("http://", "https://")):
            issues.append(_issue("HTML_EXTERNAL_ASSET", Severity.BLOCKER, "Image assets must be embedded", attrs.get("src")))
        if tag == "img" and not str(attrs.get("alt") or "").strip():
            issues.append(_issue("A11Y_IMAGE_ALT", Severity.ERROR, "Image has no non-empty alt text"))
        if tag == "svg" and attrs.get("role") != "img":
            issues.append(_issue("A11Y_SVG_ROLE", Severity.ERROR, "Informational SVG must have role=img"))
        if tag == "a" and str(attrs.get("href") or "").strip().casefold().startswith("javascript:"):
            issues.append(_issue("LINK_UNSAFE_PROTOCOL", Severity.BLOCKER, "Anchor uses javascript: URL", attrs.get("href")))
        if tag == "a" and attrs.get("target") == "_blank":
            rel = set(str(attrs.get("rel") or "").split())
            if not {"noopener", "noreferrer"}.issubset(rel):
                issues.append(_issue("LINK_OPENER_EXPOSURE", Severity.ERROR, "target=_blank link must use noopener noreferrer", attrs.get("href")))
        evidence_id = attrs.get("data-evidence-id")
        if evidence_id is not None:
            interactive = tag in {"button", "a"} or (attrs.get("role") == "button" and attrs.get("tabindex") == "0")
            if not interactive:
                issues.append(_issue("A11Y_EVIDENCE_TRIGGER", Severity.ERROR, "Evidence trigger is not keyboard operable", evidence_id))
            if not evidence_id.strip():
                issues.append(_issue("EVIDENCE_TRIGGER_EMPTY", Severity.BLOCKER, "Interactive element has an empty Evidence id"))

    raw_registry = "".join(parser.script_parts.get("evidenceRegistry", [])).strip()
    registry: Mapping[str, Any] = {}
    if not raw_registry:
        issues.append(_issue("EVIDENCE_REGISTRY_MISSING", Severity.BLOCKER, "Rendered page has no Evidence registry"))
    else:
        try:
            loaded = json.loads(raw_registry)
            if not isinstance(loaded, dict):
                raise ValueError("registry is not an object")
            registry = loaded
        except (json.JSONDecodeError, ValueError) as exc:
            issues.append(_issue("EVIDENCE_REGISTRY_INVALID", Severity.BLOCKER, str(exc)))
    trigger_ids = {str(attrs.get("data-evidence-id")) for _, attrs, _ in tags if attrs.get("data-evidence-id")}
    for evidence_id in sorted(trigger_ids - set(registry)):
        issues.append(_issue("EVIDENCE_TRIGGER_DANGLING", Severity.BLOCKER, "Trigger does not resolve in Evidence registry", evidence_id))
    if expected_evidence is not None and registry != expected_evidence:
        issues.append(_issue("EVIDENCE_REGISTRY_DRIFT", Severity.BLOCKER, "Rendered Evidence registry differs from reviewed Page Model"))
    for evidence_id, item in registry.items():
        page = item.get("page") if isinstance(item, dict) else None
        href = item.get("href") if isinstance(item, dict) else None
        if not isinstance(page, int) or page < 1:
            issues.append(_issue("EVIDENCE_PAGE_INVALID", Severity.BLOCKER, "Evidence has no valid PDF page", str(evidence_id)))
        if href and f"#page={page}" not in str(href):
            issues.append(_issue("EVIDENCE_PAGE_LINK_MISMATCH", Severity.BLOCKER, "Evidence href does not target its declared page", str(evidence_id)))
    if registry and any(isinstance(item, dict) and not item.get("href") for item in registry.values()) and "embeddedPdf" not in parser.script_parts:
        issues.append(_issue("PDF_JUMP_UNAVAILABLE", Severity.BLOCKER, "Evidence without a remote href requires an embedded local PDF"))

    card_actions = [
        (tag, attrs) for tag, attrs, ancestors in tags
        if "evidenceCard" in ancestors and (tag in {"button", "a"})
    ]
    if len(card_actions) != 2:
        issues.append(_issue("EVIDENCE_CARD_ACTIONS", Severity.ERROR, "Evidence card must contain exactly close and jump actions"))
    if "evidenceClose" not in attrs_by_id or "evidenceJump" not in attrs_by_id:
        issues.append(_issue("EVIDENCE_CARD_CONTROLS", Severity.ERROR, "Evidence card close/jump controls are incomplete"))

    lowered = html.casefold()
    if "@media(max-width:" not in lowered:
        issues.append(_issue("MOBILE_BREAKPOINT_MISSING", Severity.ERROR, "No mobile layout breakpoint found"))
    if "prefers-reduced-motion:reduce" not in lowered:
        issues.append(_issue("A11Y_REDUCED_MOTION", Severity.WARNING, "No reduced-motion accommodation found"))
    if re.search(r"(?:/users/|[a-z]:\\users\\)[^\"'<>\s]+", html, re.IGNORECASE):
        issues.append(_issue("LOCAL_PATH_LEAK", Severity.BLOCKER, "Rendered HTML contains a machine-local absolute path"))
    return ReviewReport(tuple(issues))


_SAMPLE_MARKERS = ("awm", "2608.25618", "1706.03762", "attention is all you need")
_SECRET_LITERAL = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|secret|password)\s*(?:=|:)\s*[\"'](?!\s*(?:os\.)?environ|\$\{)[^\"']{8,}[\"']"
)


def review_runtime_sources(project_root: Path) -> ReviewReport:
    """Detect sample-specific logic and literal credentials in reusable code."""

    issues: list[ReviewIssue] = []
    candidates = [
        path for path in (project_root / "paper_visualizer").rglob("*.py")
        if "review" not in path.relative_to(project_root / "paper_visualizer").parts
    ] + list((project_root / "templates").rglob("*.j2"))
    for path in sorted(candidates):
        text = path.read_text(encoding="utf-8")
        lowered = text.casefold()
        for marker in _SAMPLE_MARKERS:
            if marker in lowered:
                issues.append(_issue("SAMPLE_HARDCODING", Severity.BLOCKER, f"Reusable source contains sample marker {marker!r}", str(path.relative_to(project_root))))
        if _SECRET_LITERAL.search(text):
            issues.append(_issue("SECRET_LITERAL", Severity.BLOCKER, "Potential credential literal in reusable source", str(path.relative_to(project_root))))
    return ReviewReport(tuple(issues))


def _review_source_paths(source_paths: Iterable[Path | str]) -> list[ReviewIssue]:
    issues: list[ReviewIssue] = []
    for raw_path in source_paths:
        path = Path(raw_path)
        paths = sorted(path.rglob("*.py")) + sorted(path.rglob("*.j2")) if path.is_dir() else [path]
        for candidate in paths:
            if candidate.name == "static.py" and candidate.parent.name == "review" and candidate.parent.parent.name == "paper_visualizer":
                continue
            try:
                text = candidate.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                issues.append(_issue("SOURCE_UNREADABLE", Severity.ERROR, str(exc), str(candidate)))
                continue
            lowered = text.casefold()
            for marker in _SAMPLE_MARKERS:
                if marker in lowered:
                    issues.append(_issue("SAMPLE_HARDCODING", Severity.BLOCKER, f"Reusable source contains sample marker {marker!r}", str(candidate)))
            if _SECRET_LITERAL.search(text):
                issues.append(_issue("SECRET_LITERAL", Severity.BLOCKER, "Potential credential literal in reusable source", str(candidate)))
    return issues


def _category(code: str) -> str:
    if code.startswith("A11Y_"):
        return "accessibility"
    if code.startswith("MOBILE_"):
        return "mobile"
    if code.startswith("SAMPLE_"):
        return "hardcoding"
    if code.startswith(("SECRET_", "LOCAL_PATH_")):
        return "security"
    if code.startswith("HTML_EXTERNAL"):
        return "links"
    if code.startswith(("HTML_", "EVIDENCE_CARD_", "VISUAL_TARGET_", "TABLE_CELL_")):
        return "interaction"
    if code.startswith(("CONTRACT_", "PAGE_MODEL_", "PARSED_")):
        return "upstream_status" if code.startswith("PARSED_") else "schema"
    if code.startswith(("FORMULA_", "VARIABLE_")):
        return "formula"
    if code.startswith(("CITATION_", "RELATION_")):
        return "links"
    if code.startswith("IR_SCHEMA_"):
        return "schema"
    return "evidence"


def run_static_checks(
    ir: Mapping[str, Any],
    page_model: Mapping[str, Any],
    html: str,
    *,
    content_plan: Mapping[str, Any] | None = None,
    visual_plan: Mapping[str, Any] | None = None,
    related_work: Mapping[str, Any] | None = None,
    parsed_document: Mapping[str, Any] | None = None,
    human_review_overlay: Mapping[str, Any] | None = None,
    source_paths: Iterable[Path | str] = (),
    project_root: Path | None = None,
) -> list[dict[str, Any]]:
    """Return normalized static findings for the higher-level reviewer.

    The function only returns failed checks.  The reviewer orchestrator owns
    persistence, final status, approvals, and any repair request.
    """

    root = project_root or Path(__file__).resolve().parents[2]
    issues = list(review_ir(ir, root).issues)

    # Import here to keep this leaf checker independent from orchestration.
    from ..rendering import validate_page_model

    for message in validate_page_model(page_model, root):
        issues.append(_issue("PAGE_MODEL_CONTRACT", Severity.BLOCKER, message))
    if content_plan is not None:
        from ..planning import validate_content_plan

        for message in validate_content_plan(ir, content_plan, root):
            issues.append(_issue("CONTRACT_CONTENT_PLAN", Severity.BLOCKER, message))
    if visual_plan is not None and content_plan is not None:
        from ..visuals import validate_visual_plan

        for message in validate_visual_plan(ir, content_plan, visual_plan, root):
            issues.append(_issue("CONTRACT_VISUAL_PLAN", Severity.BLOCKER, message))
    if related_work is not None:
        from ..related import validate_related_work_plan

        for message in validate_related_work_plan(ir, related_work, root):
            issues.append(_issue("CONTRACT_RELATED_WORK", Severity.BLOCKER, message))

    ir_evidence = {str(item.get("id")): item for item in ir.get("evidence", [])}
    page_evidence = page_model.get("evidence", {})
    for evidence_id, rendered in page_evidence.items():
        source = ir_evidence.get(str(evidence_id))
        if source is None:
            issues.append(_issue("PAGE_MODEL_UNKNOWN_EVIDENCE", Severity.BLOCKER, "Page Model Evidence is absent from Paper IR", str(evidence_id)))
            continue
        if rendered.get("verbatim_text") != source.get("verbatim_text") or rendered.get("page") != source.get("locator", {}).get("page"):
            issues.append(_issue("PAGE_MODEL_EVIDENCE_DRIFT", Severity.BLOCKER, "Page Model changed Evidence text or page", str(evidence_id)))
    if set(page_evidence) != set(ir_evidence):
        issues.append(_issue("PAGE_MODEL_EVIDENCE_COVERAGE", Severity.ERROR, "Page Model Evidence registry does not cover Paper IR exactly"))

    if parsed_document is not None:
        parsed_source = parsed_document.get("source", {})
        ir_source = ir.get("source", {})
        if parsed_source.get("sha256") != ir_source.get("sha256"):
            issues.append(_issue("PARSED_SOURCE_HASH_DRIFT", Severity.BLOCKER, "Parsed document and Paper IR refer to different PDFs"))
        if parsed_source.get("page_count") != ir_source.get("page_count"):
            issues.append(_issue("PARSED_PAGE_COUNT_DRIFT", Severity.BLOCKER, "Parsed document and Paper IR page counts differ"))
        approval = (human_review_overlay or {}).get("approval", {})
        approved_parse = (
            approval.get("kind") == "human_review"
            and approval.get("status") == "approved"
            and approval.get("base_source_sha256") == parsed_source.get("sha256")
            and bool(str(approval.get("reviewer") or "").strip())
            and bool(str(approval.get("reason") or "").strip())
        )
        if parsed_document.get("status") != "passed" and not approved_parse:
            issues.append(_issue("PARSED_NOT_PASSED", Severity.ERROR, "Parsed artifact is not passed; a hash-bound human approval is required"))

    issues.extend(review_html(html, expected_evidence=page_evidence).issues)
    if source_paths:
        issues.extend(_review_source_paths(source_paths))
    else:
        issues.extend(review_runtime_sources(root).issues)

    unique: list[ReviewIssue] = []
    seen: set[tuple[str, str | None, str]] = set()
    for issue in issues:
        key = (issue.code, issue.location, issue.message)
        if key not in seen:
            unique.append(issue)
            seen.add(key)
    return [
        {
            "id": f"static:{index:04d}",
            "category": _category(issue.code),
            # The independent report contract deliberately has only two
            # release levels: anything actionable blocks; warnings do not.
            "severity": "warning" if issue.severity == Severity.WARNING else "blocker",
            "status": "failed",
            "message": issue.message,
            "evidence": [issue.code, *([issue.location] if issue.location else [])],
        }
        for index, issue in enumerate(unique, 1)
    ]
