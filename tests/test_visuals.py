from __future__ import annotations

import copy
from pathlib import Path

from paper_visualizer.modeling import build_paper_ir
from paper_visualizer.planning import build_content_plan
from paper_visualizer.visuals import build_visual_plan, validate_visual_plan


ROOT = Path(__file__).resolve().parents[1]


def _block(page: int, order: int, text: str, role: str = "body") -> dict:
    return {"id": f"block:p{page:04d}:{order:04d}", "text": text, "bbox": [20, 30 + order * 30, 580, 55 + order * 30], "order": order, "role": role}


def _parsed() -> dict:
    page1 = [
        _block(1, 0, "A General Visual Study", "heading"),
        _block(1, 1, "Abstract", "heading"),
        _block(1, 2, "Existing tools have a reliability problem."),
        _block(1, 3, "Method", "heading"),
        _block(1, 4, "We propose a source-grounded method."),
        _block(1, 5, "Figure 1: Method overview", "caption"),
    ]
    page2 = [
        _block(2, 0, "Results", "heading"),
        _block(2, 1, "The evaluation result improves the score to 91.2."),
        _block(2, 2, "Table 1: Main results", "caption"),
    ]
    return {
        "schema_version": "1.0.0", "stage_version": "1.0.0", "status": "passed", "input_hashes": ["a" * 64],
        "source": {"kind": "local", "sha256": "a" * 64, "page_count": 2, "local_pdf": "/tmp/general.pdf", "original_url": None},
        "paper": {"title": "A General Visual Study", "authors": ["A. Researcher"], "year": 2026, "language": "en"},
        "pages": [{"number": 1, "width": 600, "height": 800, "blocks": page1, "render_path": None}, {"number": 2, "width": 600, "height": 800, "blocks": page2, "render_path": None}],
        "sections": [
            {"id": "section:abstract", "title": "Abstract", "level": 1, "page_start": 1, "page_end": 1, "block_ids": [page1[1]["id"], page1[2]["id"]]},
            {"id": "section:method", "title": "Method", "level": 1, "page_start": 1, "page_end": 1, "block_ids": [item["id"] for item in page1[3:]]},
            {"id": "section:results", "title": "Results", "level": 1, "page_start": 2, "page_end": 2, "block_ids": [item["id"] for item in page2]},
        ],
        "figures": [{"id": "figure:001", "label": "1", "caption": "Figure 1: Method overview", "page": 1, "caption_block_ids": [page1[5]["id"]], "bbox": [20, 210, 580, 420], "asset_id": "asset:figure:1", "asset_path": "/tmp/figure.png", "match_status": "page_unique"}],
        "tables": [{"id": "table:001", "label": "1", "caption": "Table 1: Main results", "page": 2, "caption_block_ids": [page2[2]["id"]], "bbox": [20, 120, 580, 240], "columns": ["Method", "Score"], "rows": [["Baseline", 85.0], ["Proposed", 91.2]]}],
        "formula_candidates": [], "references": [],
    }


def _inputs() -> tuple[dict, dict]:
    base = build_paper_ir(_parsed())
    result_evidence = next(item["id"] for item in base["evidence"] if "91.2" in item["verbatim_text"])
    ir = build_paper_ir(_parsed(), overlay={"tables": [{"id": "table:0001", "discussed_cells": [{"row": 1, "column": 1, "evidence_ids": [result_evidence]}]}]})
    return ir, build_content_plan(ir)


def test_originals_are_primary_and_derived_visuals_are_source_bound():
    ir, content = _inputs()
    result = build_visual_plan(ir, content)
    assert validate_visual_plan(ir, content, result, ROOT) == []
    figure = next(item for item in result["visuals"] if item["visual_type"] == "figure")
    table = next(item for item in result["visuals"] if item["visual_type"] == "table")
    comparison = next(item for item in result["visuals"] if item["visual_type"] == "comparison")
    assert figure["kind"] == table["kind"] == "paper_original"
    assert figure["placement_role"] == table["placement_role"] == "primary"
    assert comparison["placement_role"] == "supplementary" and comparison["derived_from"]
    assert all(item["description"]["display_label"] == "派生图" for item in result["visuals"] if item["kind"] == "derived")


def test_only_body_evidence_makes_targets_interactive():
    ir, content = _inputs()
    result = build_visual_plan(ir, content)
    figure = next(item for item in result["visuals"] if item["visual_type"] == "figure")
    comparison = next(item for item in result["visuals"] if item["visual_type"] == "comparison")
    assert figure["targets"] and not figure["targets"][0]["interactive"]
    assert sum(target["interactive"] for target in comparison["targets"]) == 1
    assert sum(mark["target_id"] is not None for mark in comparison["layout"]["marks"]) == 1


def test_validator_rejects_interactive_caption_and_missing_derivation():
    ir, content = _inputs()
    result = build_visual_plan(ir, content)
    figure = next(item for item in result["visuals"] if item["visual_type"] == "figure")
    caption_evidence = next(item["id"] for item in ir["evidence"] if "Figure 1" in item["verbatim_text"])
    figure["targets"][0].update(interactive=True, evidence_ids=[caption_evidence])
    derived = next(item for item in result["visuals"] if item["kind"] == "derived")
    derived["derived_from"] = []
    errors = validate_visual_plan(ir, content, result, ROOT)
    assert any("body Evidence" in error for error in errors)
    assert any("derived visual lacks" in error or "non-empty" in error for error in errors)


def test_validator_rejects_originals_without_displayable_sources():
    ir, content = _inputs()
    result = build_visual_plan(ir, content)
    broken = copy.deepcopy(ir)
    broken["visuals"][0]["asset_path"] = None
    broken["tables"][0]["asset_path"] = None
    broken["tables"][0]["columns"] = []
    broken["tables"][0]["rows"] = []
    errors = validate_visual_plan(broken, content, result, ROOT)
    assert any("paper original figure lacks a validated asset" in error for error in errors)
    assert any("paper original table lacks an asset or structured data" in error for error in errors)


def test_runtime_has_no_sample_specific_branching():
    source = (ROOT / "paper_visualizer" / "visuals" / "planner.py").read_text(encoding="utf-8").casefold()
    assert all(marker not in source for marker in ("awm", "2608.25618", "1706.03762", "attention is all you need"))
