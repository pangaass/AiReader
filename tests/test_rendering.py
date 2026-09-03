from __future__ import annotations

import base64
import json
import re
from pathlib import Path

import pytest

from paper_visualizer.modeling import build_paper_ir
from paper_visualizer.planning import build_content_plan
from paper_visualizer.rendering import PageModelError, build_page_model, render_html, validate_page_model
from paper_visualizer.rendering import page_model as page_model_module
from paper_visualizer.visuals import build_visual_plan


ROOT = Path(__file__).resolve().parents[1]
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")


def _block(page: int, order: int, text: str, role: str = "body") -> dict:
    return {"id": f"block:p{page:04d}:{order:04d}", "text": text, "bbox": [20, 30 + order * 30, 580, 55 + order * 30], "order": order, "role": role}


def _inputs(tmp_path: Path) -> tuple[dict, dict, dict]:
    image = tmp_path / "method.png"
    image.write_bytes(PNG)
    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(b"%PDF-1.4\n% renderer fixture\n")
    page1 = [
        _block(1, 0, "A <General> Rendering Study", "heading"),
        _block(1, 1, "Abstract", "heading"),
        _block(1, 2, "Existing systems have a reliability problem."),
        _block(1, 3, "Method", "heading"),
        _block(1, 4, "We propose a grounded method where x denotes the input vector."),
        _block(1, 5, "y = W x + b (1)", "formula"),
        _block(1, 6, "Figure 1: General method overview", "caption"),
    ]
    page2 = [
        _block(2, 0, "Results", "heading"),
        _block(2, 1, "The evaluation result improves the reported score to 91.2."),
        _block(2, 2, "Table 1: Main results", "caption"),
    ]
    parsed = {
        "schema_version": "1.0.0", "stage_version": "1.0.0", "status": "passed", "input_hashes": ["a" * 64],
        "source": {"kind": "local", "sha256": "a" * 64, "page_count": 2, "local_pdf": str(pdf), "original_url": None},
        "paper": {"title": "A <General> Rendering Study", "authors": ["R. Researcher"], "year": 2026, "language": "en", "abstract": "A compact abstract."},
        "pages": [{"number": 1, "width": 600, "height": 800, "blocks": page1, "render_path": None}, {"number": 2, "width": 600, "height": 800, "blocks": page2, "render_path": None}],
        "sections": [
            {"id": "section:abstract", "title": "Abstract", "level": 1, "page_start": 1, "page_end": 1, "block_ids": [page1[1]["id"], page1[2]["id"]]},
            {"id": "section:method", "title": "Method", "level": 1, "page_start": 1, "page_end": 1, "block_ids": [item["id"] for item in page1[3:]]},
            {"id": "section:results", "title": "Results", "level": 1, "page_start": 2, "page_end": 2, "block_ids": [item["id"] for item in page2]},
        ],
        "figures": [{"id": "figure:001", "label": "1", "caption": "Figure 1: General method overview", "page": 1, "caption_block_ids": [page1[6]["id"]], "bbox": [20, 210, 580, 420], "asset_id": "asset:1", "asset_path": str(image), "match_status": "page_unique"}],
        "tables": [{"id": "table:001", "label": "1", "caption": "Table 1: Main results", "page": 2, "caption_block_ids": [page2[2]["id"]], "bbox": [20, 120, 580, 240], "asset_path": str(image), "columns": ["Method", "Score"], "rows": [["Baseline", 85.0], ["Proposed", 91.2]]}],
        "formula_candidates": [{"id": "formula-candidate:001", "text": "y = W x + b (1)", "label": "1", "page": 1, "block_ids": [page1[5]["id"]], "bbox": [20, 160, 580, 190]}],
        "references": [],
    }
    base = build_paper_ir(parsed)
    result_evidence = next(item["id"] for item in base["evidence"] if "91.2" in item["verbatim_text"])
    ir = build_paper_ir(parsed, overlay={"tables": [{"id": "table:0001", "discussed_cells": [{"row": 1, "column": 1, "evidence_ids": [result_evidence]}]}]})
    content = build_content_plan(ir)
    visuals = build_visual_plan(ir, content)
    return ir, content, visuals


def test_page_model_resolves_ir_and_never_serializes_local_paths(tmp_path: Path):
    ir, content, visuals = _inputs(tmp_path)
    model = build_page_model(ir, content, visuals)
    assert validate_page_model(model, ROOT) == []
    assert len(model["sections"]) == 11
    assert model["sections"][0]["kind"] == "one_minute_read"
    assert "hero" not in model
    assert model["paper"]["pdf_url"] is None
    assert str(tmp_path) not in str(model)
    formulas = [item for section in model["sections"] for item in section["components"] if item["component"] == "formula"]
    assert formulas and any(token["evidence_id"] for token in formulas[0]["tokens"] if token["variable_id"])


def test_renderer_is_single_file_escaped_and_accessible(tmp_path: Path):
    ir, content, visuals = _inputs(tmp_path)
    html = render_html(ir, content, visuals)
    assert "A &lt;General&gt; Rendering Study" in html and "A <General> Rendering Study" not in html
    assert "data:image/png;base64," in html
    assert "data-image-src=" not in html
    assert "<math display=\"block\"" in html and "class=\"math-var\"" in html
    assert "id=\"evidenceCard\" role=\"dialog\"" in html
    assert "aria-label=\"跳到正文\"" not in html  # the visible text, not a redundant aria-label
    assert "<a class=\"skip\" href=\"#main\">跳到正文</a>" in html
    assert "#page=${item.page}" in html
    assert str(tmp_path) not in html
    assert "https://cdn" not in html and "<script src=" not in html
    assert "证据地图" not in html and "论文出处" not in html
    assert "原文摘录" not in html and "生成式总结" not in html
    assert "Paper Metadata" in html and "一分钟速读" in html
    assert 'class="panel quick-read-grid"' in html
    assert html.count('<article class="quick-read-cell"') == 3
    assert "@media(max-width:420px){.quick-read-grid{grid-template-columns:1fr}" in html
    assert 'class="panel story-flow story-flow-' in html
    story_steps = re.findall(r'<article class="story-step".*?</article>', html, re.S)
    assert story_steps
    assert all("source-chip" not in step and "查看原文" not in step for step in story_steps)
    assert 'data-source-page="1"' in html
    assert "content-section-evidence-map" not in html and "content-section-provenance" not in html
    registry_match = re.search(r'<script type="application/json" id="evidenceRegistry">(.*?)</script>', html, re.S)
    assert registry_match is not None
    assert json.loads(registry_match.group(1)) == build_page_model(ir, content, visuals)["evidence"]
    assert "&#34;" not in registry_match.group(1)
    assert '<img id="imageLarge" alt="论文原图放大预览">' in html


def test_supported_formula_notation_does_not_force_preview(tmp_path: Path):
    ir, _, _ = _inputs(tmp_path)
    ir["formulas"][0]["latex"] = r"y_{i}=\tau_i+\sqrt d_k+step_num"
    content = build_content_plan(ir)
    visuals = build_visual_plan(ir, content)
    model = build_page_model(ir, content, visuals)
    formula = next(
        item
        for section in model["sections"]
        for item in section["components"]
        if item["component"] == "formula"
    )
    assert formula["render_note"] is None
    assert model["preview"] is False


def test_formula_tokens_match_canonical_and_pdf_collapsed_subscripts():
    variables = [
        {"id": "variable:k", "symbol": "d_k", "definition_evidence_id": "evidence:k"},
        {"id": "variable:v", "symbol": "d_{v}", "definition_evidence_id": "evidence:v"},
        {"id": "variable:model", "symbol": "d_{model}", "definition_evidence_id": "evidence:model"},
        {"id": "variable:h", "symbol": "h", "definition_evidence_id": "evidence:h"},
    ]

    tokens = page_model_module._formula_tokens("dk = d_{v} = dmodel/h", variables)
    interactive = {item["text"]: item["variable_id"] for item in tokens if item["variable_id"]}

    assert interactive == {
        "dk": "variable:k",
        "d_{v}": "variable:v",
        "dmodel": "variable:model",
        "h": "variable:h",
    }


def test_formula_tokens_restore_collapsed_multiplication_boundary():
    variables = [
        {"id": "variable:x", "symbol": "x", "definition_evidence_id": "evidence:ffn"},
        {"id": "variable:w1", "symbol": "W1", "definition_evidence_id": "evidence:ffn"},
    ]

    tokens = page_model_module._formula_tokens("FFN(x) = max(0,xW1 + b1)", variables)
    interactive = {item["text"]: item["variable_id"] for item in tokens if item["variable_id"]}

    assert interactive == {"x": "variable:x", "W1": "variable:w1"}


def test_formula_mathml_restores_scripts_radical_and_projection_notation():
    variables = [
        {"id": "variable:q", "symbol": "Q", "definition_evidence_id": "evidence:q"},
        {"id": "variable:k", "symbol": "K", "definition_evidence_id": "evidence:k"},
        {"id": "variable:dk", "symbol": "d_k", "definition_evidence_id": "evidence:dk"},
        {"id": "variable:w", "symbol": "W", "definition_evidence_id": "evidence:w"},
    ]

    mathml = page_model_module._formula_mathml(
        r"softmax(QKT \sqrtdk) + QW Q i", variables
    )

    assert "<msqrt>" in mathml
    assert "<msub>" in mathml
    assert "<msup>" in mathml
    assert "<mfrac>" in mathml
    assert 'data-evidence-id="evidence:dk"' in mathml
    assert 'data-variable-id="variable:dk"' in mathml


def test_formula_mathml_binds_base_and_superscript_variables_independently():
    variables = [
        {"id": "variable:w", "symbol": "W", "definition_evidence_id": "evidence:w"},
        {"id": "variable:o", "symbol": "O", "definition_evidence_id": "evidence:o"},
    ]

    mathml = page_model_module._formula_mathml(r"W^O", variables)

    assert '<msup>' in mathml
    assert mathml.count('data-variable-id="variable:w"') == 1
    assert mathml.count('data-variable-id="variable:o"') == 1
    assert mathml.index('data-variable-id="variable:w"') < mathml.index('data-variable-id="variable:o"')


def test_formula_mathml_keeps_subscript_variable_bound_when_it_has_an_exponent():
    variables = [
        {"id": "variable:model", "symbol": "d_{model}", "definition_evidence_id": "evidence:model"},
        {"id": "variable:warmup", "symbol": "warmup_{steps}", "definition_evidence_id": "evidence:warmup"},
    ]

    mathml = page_model_module._formula_mathml(
        r"d_{model}^-0.5 \cdot warmup_{steps}^{-1.5}", variables
    )

    assert mathml.count('data-variable-id="variable:model"') == 1
    assert mathml.count('data-variable-id="variable:warmup"') == 1
    assert "<msup>" in mathml and "<msub>" in mathml
    assert "<mo>−</mo><mn>0.5</mn>" in mathml


def test_formula_mathml_escapes_untrusted_formula_text():
    mathml = page_model_module._formula_mathml("x <script>alert(1)</script>", [])

    assert "<script>" not in mathml
    assert "&lt;" in mathml and "&gt;" in mathml


def test_formula_mathml_renders_fraction_without_external_runtime():
    mathml = page_model_module._formula_mathml(r"z=\frac{x_1}{\sqrt{d_k}}", [])

    assert "<mfrac>" in mathml
    assert "<msqrt>" in mathml
    assert "<msub>" in mathml


def test_table_crop_is_embedded_when_structured_cells_are_unavailable(tmp_path: Path):
    ir, content, visuals = _inputs(tmp_path)
    ir["tables"][0]["columns"] = []
    ir["tables"][0]["rows"] = []
    content = build_content_plan(ir)
    visuals = build_visual_plan(ir, content)

    model = build_page_model(ir, content, visuals)
    table = next(
        item
        for section in model["sections"]
        for item in section["components"]
        if item["component"] == "table"
    )

    assert table["asset_data_uri"].startswith("data:image/png;base64,")
    assert table["rows"] == []


def test_unreviewed_plans_require_explicit_preview_opt_in(tmp_path: Path):
    ir, content, visuals = _inputs(tmp_path)
    visuals["review"] = {"status": "needs_review", "warnings": ["manual check"]}
    with pytest.raises(PageModelError, match="review.status=passed"):
        build_page_model(ir, content, visuals)
    preview = build_page_model(ir, content, visuals, allow_unreviewed=True)
    assert preview["preview"] is True


def test_runtime_and_templates_have_no_sample_specific_branching():
    sources = list((ROOT / "paper_visualizer" / "rendering").glob("*.py")) + list((ROOT / "templates").glob("*.j2"))
    text = "\n".join(path.read_text(encoding="utf-8") for path in sources).casefold()
    forbidden = ("awm", "2608.25618", "1706.03762", "attention is all you need")
    assert all(marker not in text for marker in forbidden)
