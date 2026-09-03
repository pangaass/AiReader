from __future__ import annotations

import copy
from pathlib import Path

from paper_visualizer.modeling import build_paper_ir
from paper_visualizer.review import Severity, review_html, review_ir, review_runtime_sources


ROOT = Path(__file__).resolve().parents[1]


def _block(page: int, order: int, text: str, role: str = "body") -> dict:
    return {
        "id": f"block:p{page:04d}:{order:04d}",
        "text": text,
        "bbox": [20, 20 + order * 20, 500, 38 + order * 20],
        "order": order,
        "role": role,
    }


def _ir() -> dict:
    blocks = [
        _block(1, 0, "Review Fixture", "heading"),
        _block(1, 1, "Method", "heading"),
        _block(1, 2, "We propose a grounded method where x denotes the input."),
        _block(1, 3, "y = x (1)", "formula"),
    ]
    parsed = {
        "schema_version": "1.0.0",
        "stage_version": "1.0.0",
        "status": "passed",
        "input_hashes": ["a" * 64],
        "source": {"kind": "local", "sha256": "a" * 64, "page_count": 1, "local_pdf": "/tmp/review.pdf", "original_url": None},
        "paper": {"title": "Review Fixture", "authors": ["R. Reviewer"], "year": 2026, "language": "en"},
        "pages": [{"number": 1, "width": 600, "height": 800, "blocks": blocks, "render_path": None}],
        "sections": [{"id": "section:method", "title": "Method", "level": 1, "page_start": 1, "page_end": 1, "block_ids": [item["id"] for item in blocks[1:]]}],
        "figures": [],
        "tables": [],
        "formula_candidates": [{"id": "formula-candidate:001", "text": "y = x (1)", "label": "1", "page": 1, "block_ids": [blocks[3]["id"]], "bbox": blocks[3]["bbox"]}],
        "references": [],
    }
    return build_paper_ir(parsed)


def _html(registry: str, trigger: str = "evidence:00001") -> str:
    return f'''<!doctype html><html lang="zh-CN"><head><style>@media(max-width:620px){{body{{margin:0}}}} @media(prefers-reduced-motion:reduce){{*{{animation:none}}}}</style></head><body>
    <a href="#main">跳到正文</a><main id="main"><button data-evidence-id="{trigger}">查看原文</button></main>
    <aside id="evidenceCard"><button id="evidenceClose">×</button><a id="evidenceJump">↗</a></aside>
    <script type="application/json" id="evidenceRegistry">{registry}</script></body></html>'''


def test_review_ir_accepts_builder_output_and_rejects_cross_page_evidence():
    ir = _ir()
    assert review_ir(ir, ROOT).passed
    broken = copy.deepcopy(ir)
    broken["evidence"][0]["locator"]["page"] = 2
    report = review_ir(broken, ROOT)
    assert not report.passed
    assert any(item.code == "EVIDENCE_CROSSES_PAGE" and item.severity == Severity.BLOCKER for item in report.issues)


def test_review_ir_rejects_excerpt_drift_and_wrong_label():
    broken = copy.deepcopy(_ir())
    broken["claims"][0]["summary"] = "This is not an extract from the linked paper Evidence."
    broken["claims"][0]["display_label"] = "生成式总结"

    report = review_ir(broken, ROOT)
    codes = {item.code for item in report.issues}
    assert {"CLAIM_PROVENANCE_LABEL_MISMATCH", "CLAIM_EXCERPT_DRIFT"} <= codes


def test_review_ir_rejects_invalid_variable_backlink_and_relation_types():
    broken = copy.deepcopy(_ir())
    variable = broken["variables"][0]
    formula = broken["formulas"][0]
    formula["variable_ids"] = []
    broken["relations"].append({"id": "relation:bad", "type": "same_lineage", "source_id": variable["id"], "target_id": formula["id"]})
    codes = {item.code for item in review_ir(broken, ROOT).issues}
    assert {"VARIABLE_BACKLINK_MISMATCH", "RELATION_TYPE_MISMATCH"} <= codes


def test_formula_without_variables_is_a_blocker():
    broken = copy.deepcopy(_ir())
    formula = broken["formulas"][0]
    variable_ids = set(formula["variable_ids"])
    formula["variable_ids"] = []
    broken["variables"] = [item for item in broken["variables"] if item["id"] not in variable_ids]
    broken["relations"] = [
        item for item in broken["relations"]
        if item["source_id"] not in variable_ids and item["target_id"] not in variable_ids
    ]

    report = review_ir(broken, ROOT)

    assert any(
        item.code == "FORMULA_VARIABLE_COVERAGE" and item.severity == Severity.BLOCKER
        for item in report.issues
    )


def test_pure_constant_formula_requires_an_explicit_approved_reason():
    reviewed = copy.deepcopy(_ir())
    formula = reviewed["formulas"][0]
    variable_ids = set(formula["variable_ids"])
    formula["latex"] = "1 + 1 = 2"
    formula["variable_ids"] = []
    reviewed["variables"] = [item for item in reviewed["variables"] if item["id"] not in variable_ids]
    reviewed["relations"] = [
        item for item in reviewed["relations"]
        if item["source_id"] not in variable_ids and item["target_id"] not in variable_ids
    ]
    reviewed["review"]["items"].append({
        "kind": "formula_variable_exception",
        "formula_id": formula["id"],
        "reason": "pure_constant",
        "rationale": "The expression contains numeric constants and operators only.",
        "status": "approved",
    })

    assert not any(item.code == "FORMULA_VARIABLE_COVERAGE" for item in review_ir(reviewed, ROOT).issues)


def test_formula_variable_definition_must_be_verified_verbatim_evidence():
    broken = copy.deepcopy(_ir())
    definition_id = broken["variables"][0]["definition_evidence_id"]
    definition = next(item for item in broken["evidence"] if item["id"] == definition_id)
    definition["provenance"]["verification"] = "pending"

    report = review_ir(broken, ROOT)

    assert any(item.code == "VARIABLE_DEFINITION_UNVERIFIED" for item in report.issues)


def test_html_review_accepts_grounded_shell_and_detects_release_blockers():
    evidence = {"evidence:00001": {"id": "evidence:00001", "verbatim_text": "paper text", "page": 3, "href": "https://example.org/paper.pdf#page=3", "section_id": None}}
    import json

    assert review_html(_html(json.dumps(evidence)), expected_evidence=evidence).passed
    broken = _html(json.dumps(evidence), trigger="evidence:missing").replace("</head>", '<script src="https://cdn.example/app.js"></script></head>')
    report = review_html(broken, expected_evidence=evidence)
    codes = {item.code for item in report.issues}
    assert {"HTML_EXTERNAL_RUNTIME", "EVIDENCE_TRIGGER_DANGLING"} <= codes


def test_runtime_source_review_is_clean_and_finds_sample_and_secret(tmp_path: Path):
    assert review_runtime_sources(ROOT).passed
    (tmp_path / "paper_visualizer").mkdir()
    (tmp_path / "templates").mkdir()
    (tmp_path / "paper_visualizer" / "bad.py").write_text('PAPER = "2608.25618"\nAPI_KEY = "literal-secret-value"', encoding="utf-8")
    codes = {item.code for item in review_runtime_sources(tmp_path).issues}
    assert {"SAMPLE_HARDCODING", "SECRET_LITERAL"} <= codes
