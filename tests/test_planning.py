from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from paper_visualizer.modeling import build_paper_ir
from paper_visualizer.planning import PlanningError, build_content_plan, enrich_content_plan, validate_content_plan
from paper_visualizer.planning import planner as planner_module
from paper_visualizer.planning import narrative as narrative_module


ROOT = Path(__file__).resolve().parents[1]


def _block(page: int, order: int, text: str, role: str = "body") -> dict:
    return {"id": f"block:p{page:04d}:{order:04d}", "text": text, "bbox": [20, 20 + order * 20, 500, 38 + order * 20], "order": order, "role": role}


def _ir(*, with_formula: bool = True, with_reference_context: bool = True) -> dict:
    blocks = [
        _block(1, 0, "A General Study", "heading"),
        _block(1, 1, "Abstract", "heading"),
        _block(1, 2, "Existing systems have a reliability problem that motivates this study."),
        _block(1, 3, "Method", "heading"),
        _block(1, 4, "We propose a grounded method with deterministic source links."),
        _block(1, 5, "where x denotes the input vector."),
    ]
    formulas = []
    if with_formula:
        blocks.append(_block(1, 6, "y = W x + b (1)", "formula"))
        formulas = [{"id": "formula-candidate:001", "text": "y = W x + b (1)", "label": "1", "page": 1, "block_ids": ["block:p0001:0006"], "bbox": [20, 140, 500, 158]}]
    page2 = [
        _block(2, 0, "Results", "heading"),
        _block(2, 1, "The evaluation result improves the reported score [1]." if with_reference_context else "The evaluation result improves the reported score."),
        _block(2, 2, "Table 1: Main results", "caption"),
        _block(2, 3, "[1] A. Author. Prior Study. 2024. https://example.org/prior", "reference"),
    ]
    parsed = {
        "schema_version": "1.0.0", "stage_version": "1.0.0", "status": "passed", "input_hashes": ["a" * 64],
        "source": {"kind": "local", "sha256": "a" * 64, "page_count": 2, "local_pdf": "/tmp/general.pdf", "original_url": None},
        "paper": {"title": "A General Study", "authors": ["A. Researcher"], "year": 2026, "language": "en"},
        "pages": [{"number": 1, "width": 600, "height": 800, "blocks": blocks, "render_path": None}, {"number": 2, "width": 600, "height": 800, "blocks": page2, "render_path": None}],
        "sections": [
            {"id": "section:abstract", "title": "Abstract", "level": 1, "page_start": 1, "page_end": 1, "block_ids": ["block:p0001:0001", "block:p0001:0002"]},
            {"id": "section:method", "title": "Method", "level": 1, "page_start": 1, "page_end": 1, "block_ids": [block["id"] for block in blocks[3:]]},
            {"id": "section:results", "title": "Results", "level": 1, "page_start": 2, "page_end": 2, "block_ids": ["block:p0002:0000", "block:p0002:0001", "block:p0002:0002"]},
        ],
        "figures": [{"id": "figure:001", "label": "1", "caption": "Figure 1: Method overview", "page": 1, "caption_block_ids": [], "bbox": [20, 200, 500, 400], "asset_id": "asset:1", "asset_path": "/tmp/figure.png", "match_status": "page_unique"}],
        "tables": [{"id": "table:001", "label": "1", "caption": "Table 1: Main results", "page": 2, "caption_block_ids": ["block:p0002:0002"], "bbox": [20, 100, 500, 180], "columns": ["Method", "Score"], "rows": [["Proposed", 91.2]]}],
        "formula_candidates": formulas,
        "references": [{"id": "reference:0001", "raw_reference": "[1] A. Author. Prior Study. 2024. https://example.org/prior", "page": 2, "block_ids": ["block:p0002:0003"]}],
    }
    return build_paper_ir(parsed)


def _plan_hash(plan: dict) -> str:
    raw = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def test_builds_grounded_deduplicated_plan_and_handoff_requests():
    ir = _ir()
    plan = build_content_plan(ir)

    assert validate_content_plan(ir, plan, ROOT) == []
    assert [section["kind"] for section in plan["sections"]] == [kind for kind, _ in __import__("paper_visualizer.planning.planner", fromlist=["SECTION_ORDER"]).SECTION_ORDER]
    assert len(plan["sections"]) == 11
    assert not {"evidence_map", "provenance"} & {section["kind"] for section in plan["sections"]}
    assert not {"evidence_map", "source_index"} & {item["content_type"] for item in plan["items"]}
    narratives = [item for item in plan["items"] if item["content_type"] == "narrative"]
    assert narratives and all(item["evidence_ids"] for item in narratives)
    assert all("display_label" not in item for item in narratives)
    assert not any(item["content_type"] in {"paper_excerpt", "generated_summary"} for item in plan["items"])
    formula_items = [item for item in plan["items"] if item["content_type"] == "formula"]
    assert formula_items and all(item["title"].startswith("公式 ") and "\\" not in item["title"] for item in formula_items)
    placed_ids = [item_id for section in plan["sections"] for item_id in section["item_ids"]]
    assert len(placed_ids) == len(set(placed_ids))
    by_kind = {section["kind"]: section for section in plan["sections"]}
    assert all(by_kind[kind]["item_ids"] for kind in ("one_minute_read", "research_task", "method_details", "main_results"))
    assert all("verbatim_text" not in item and "latex" not in item and "caption" not in item for item in plan["items"])
    assert all(request["derived_from"] for request in plan["visual_requests"])
    table_request = next(request for request in plan["visual_requests"] if request["semantic_intent"] == "original_table")
    assert table_request["interaction_policy"] == "static"


def test_missing_optional_sections_are_explicit_not_fabricated():
    plan = build_content_plan(_ir(with_formula=False, with_reference_context=False))
    by_kind = {section["kind"]: section for section in plan["sections"]}

    assert by_kind["training_inference"]["status"] == "unavailable"
    assert by_kind["existing_methods"]["status"] == "unavailable"
    assert {item["section_kind"] for item in plan["omissions"]} >= {"training_inference", "existing_methods"}


def test_claim_bucket_uses_section_semantics_before_loose_result_words():
    entities = {
        "section:method": {"id": "section:method", "title": "Method and Training"},
        "section:prior": {"id": "section:prior", "title": "Related Work"},
        "section:results": {"id": "section:results", "title": "Experimental Results"},
        "evidence:method": {
            "id": "evidence:method", "section_id": "section:method",
            "verbatim_text": "Our method improves memory construction before evaluation.",
        },
        "evidence:prior": {
            "id": "evidence:prior", "section_id": "section:prior",
            "verbatim_text": "Prior methods improved retrieval accuracy [3].",
        },
        "evidence:results": {
            "id": "evidence:results", "section_id": "section:results",
            "verbatim_text": "The evaluation reports an accuracy improvement of five points.",
        },
    }
    assert planner_module._claim_bucket({"summary": "Method summary.", "evidence_ids": ["evidence:method"]}, entities) == ("training_inference", False)
    assert planner_module._claim_bucket({"summary": "Prior-work summary.", "evidence_ids": ["evidence:prior"]}, entities) == ("existing_methods", False)
    assert planner_module._claim_bucket({"summary": "Result summary.", "evidence_ids": ["evidence:results"]}, entities) == ("main_results", False)


def test_human_overlay_is_hash_bound_and_cannot_change_source_bindings():
    ir = _ir()
    base = build_content_plan(ir)
    narrative = next(item for item in base["items"] if item["content_type"] == "narrative")
    approval = {"status": "approved", "base_ir_sha256": base["source_ir_sha256"], "base_plan_sha256": _plan_hash(base), "reviewer": "human", "reason": "Improve readability."}

    result = build_content_plan(ir, {"approval": approval, "items": [{"id": narrative["id"], "title": "A clearer title", "body": "A clearer evidence-grounded explanation."}]})
    assert result["review"]["overlay_applied"] is True
    updated = next(item for item in result["items"] if item["id"] == narrative["id"])
    assert updated["body"] == "A clearer evidence-grounded explanation."
    with pytest.raises(PlanningError, match="source bindings"):
        build_content_plan(ir, {"approval": approval, "items": [{"id": narrative["id"], "source_ids": ["evidence:99999"]}]})
    bad = dict(approval, base_plan_sha256="0" * 64)
    with pytest.raises(PlanningError, match="hash-bound"):
        build_content_plan(ir, {"approval": bad})


def test_generated_claim_becomes_an_unlabeled_editable_narrative():
    ir = copy.deepcopy(_ir())
    ir["claims"][0]["summary"] = "A generated explanation of the method remains grounded in source evidence."
    ir["claims"][0]["display_label"] = "生成式总结"
    ir["claims"][0]["provenance"]["kind"] = "generated_summary"
    base = build_content_plan(ir)
    summary = next(item for item in base["items"] if item["content_type"] == "narrative")
    approval = {"status": "approved", "base_ir_sha256": base["source_ir_sha256"], "base_plan_sha256": _plan_hash(base), "reviewer": "human", "reason": "Improve readability."}

    result = build_content_plan(ir, {"approval": approval, "items": [{"id": summary["id"], "body": "A shorter grounded summary."}]})
    updated = next(item for item in result["items"] if item["id"] == summary["id"])
    assert "display_label" not in updated
    assert updated["body"] == "A shorter grounded summary."


def test_overlay_cannot_duplicate_an_item_across_sections():
    ir = _ir()
    base = build_content_plan(ir)
    item_id = base["sections"][1]["item_ids"][0]
    approval = {"status": "approved", "base_ir_sha256": base["source_ir_sha256"], "base_plan_sha256": _plan_hash(base), "reviewer": "human", "reason": "Check duplicate placement."}
    duplicate_sections = [
        {"id": base["sections"][0]["id"], "item_ids": [*base["sections"][0]["item_ids"], item_id]},
    ]
    with pytest.raises(PlanningError, match="must not render the same item"):
        build_content_plan(ir, {"approval": approval, "sections": duplicate_sections})


def test_llm_enrichment_rewrites_every_narrative_and_enriches_metadata(monkeypatch):
    ir = _ir()
    plan = build_content_plan(ir)
    narrative_ids = [item["id"] for item in plan["items"] if item["content_type"] == "narrative"]
    monkeypatch.setattr(narrative_module, "_call_structured", lambda **kwargs: {
        "metadata": {
            "title": "A General Study", "authors": ["A. Researcher"],
            "affiliations": ["Example University"], "venue": "ExampleConf 2026",
            "published_at": "2026", "doi": "10.1000/example", "arxiv_id": None,
        },
        "items": [
            {"id": item_id, "title": f"解释 {index}", "body": f"这是基于该条证据形成的中文解释 {index}。"}
            for index, item_id in enumerate(narrative_ids, 1)
        ],
    })

    enriched = enrich_content_plan(ir, plan, api_key="secret", model="test-model")

    assert validate_content_plan(ir, enriched, ROOT) == []
    assert enriched["paper_metadata"]["affiliations"] == ["Example University"]
    assert all(
        item["generation"] == "llm" and "摘录" not in item["body"]
        for item in enriched["items"] if item["content_type"] == "narrative"
    )


def test_runtime_has_no_sample_specific_branching():
    source = (ROOT / "paper_visualizer" / "planning" / "planner.py").read_text(encoding="utf-8").casefold()
    assert all(marker not in source for marker in ("awm", "2608.25618", "1706.03762", "attention is all you need"))
