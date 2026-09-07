from __future__ import annotations

import copy
import json
from pathlib import Path

from paper_visualizer.modeling import build_paper_ir
from paper_visualizer.parsing import ParseOptions, parse_source
from paper_visualizer.related import build_related_work_plan, validate_related_work_plan, verify_related_work
from paper_visualizer.related import planner as planner_module
from paper_visualizer.rendering.page_model import _related_component


ROOT = Path(__file__).resolve().parents[1]


def _block(page: int, order: int, text: str, role: str = "body") -> dict:
    return {"id": f"block:p{page:04d}:{order:04d}", "text": text, "bbox": [20, 20 + order * 20, 550, 38 + order * 20], "order": order, "role": role}


def _ir() -> dict:
    body = _block(1, 2, "Prior grounded systems use retrieval and page navigation [1, 2].")
    caption = _block(1, 3, "Figure 1: Baseline adapted from [3].", "caption")
    refs = [
        _block(2, 0, "[1] A. Author. Retrieval Systems. 2021. https://example.org/one", "reference"),
        _block(2, 1, "[2] B. Author. Page Navigation. 2022. https://example.org/two", "reference"),
        _block(2, 2, "[3] C. Author. Figure Source. 2020. https://example.org/three", "reference"),
    ]
    parsed = {
        "schema_version": "1.0.0", "stage_version": "1.0.0", "status": "passed", "input_hashes": ["a" * 64],
        "source": {"kind": "local", "sha256": "a" * 64, "page_count": 2, "local_pdf": "/tmp/general.pdf", "original_url": None},
        "paper": {"title": "A General Study", "authors": ["R. Writer"], "year": 2026, "language": "en", "external_url": "https://example.org/current"},
        "pages": [
            {"number": 1, "width": 600, "height": 800, "blocks": [_block(1, 0, "A General Study", "heading"), _block(1, 1, "Related Work", "heading"), body, caption], "render_path": None},
            {"number": 2, "width": 600, "height": 800, "blocks": refs, "render_path": None},
        ],
        "sections": [{"id": "section:related", "title": "Related Work", "level": 1, "page_start": 1, "page_end": 1, "block_ids": ["block:p0001:0001", body["id"], caption["id"]]}],
        "figures": [], "tables": [], "formula_candidates": [],
        "references": [
            {"id": "reference:1", "raw_reference": refs[0]["text"], "page": 2, "block_ids": [refs[0]["id"]]},
            {"id": "reference:2", "raw_reference": refs[1]["text"], "page": 2, "block_ids": [refs[1]["id"]]},
            {"id": "reference:3", "raw_reference": refs[2]["text"], "page": 2, "block_ids": [refs[2]["id"]]},
        ],
    }
    return build_paper_ir(parsed)


class FakeProvider:
    name = "aminer.paper_search"
    unit_cost_cny = 0.01

    def __init__(self) -> None:
        self.calls = 0

    def search(self, title: str) -> list[dict]:
        self.calls += 1
        return [{"id": f"record-{self.calls}", "title": title, "authors": [{"name": "Verified Author"}], "year": 2021 if "Retrieval" in title else 2022, "venue_name": "Test Venue"}]


def test_builds_schema_valid_grounded_graph_and_filters_caption_mentions():
    ir = _ir()
    plan = build_related_work_plan(ir, project_root=ROOT)
    assert validate_related_work_plan(ir, plan, ROOT) == []
    assert len(plan["edges"]) == 2
    assert all(edge["kind"] == "same_problem" and edge["basis_evidence_ids"] for edge in plan["edges"])
    assert all(edge["source_id"].startswith("related:") and edge["target_id"] == plan["paper_id"] for edge in plan["edges"])
    assert plan["policy"]["edge_direction"] == "source_to_current_paper"
    assert plan["policy"]["category_quota"] == "none"
    assert all(not node["raw_reference"].startswith("[3]") for node in plan["nodes"])
    assert all("verbatim_text" not in node["description"] for node in plan["nodes"])


def test_validator_rejects_external_or_cross_citation_description_evidence():
    ir = _ir()
    plan = build_related_work_plan(ir, project_root=ROOT)
    first = plan["nodes"][0]
    caption_evidence = next(item["id"] for item in ir["evidence"] if item["verbatim_text"].startswith("Figure 1"))
    first["description"]["evidence_ids"] = [caption_evidence]
    errors = validate_related_work_plan(ir, plan, ROOT)
    assert any("not bound to its citation" in error for error in errors)


def test_verification_updates_metadata_only_and_cache_avoids_repeat_calls(tmp_path: Path):
    plan = build_related_work_plan(_ir(), project_root=ROOT)
    provider = FakeProvider()
    before = [copy.deepcopy(node["description"]) for node in plan["nodes"]]
    verified, cost = verify_related_work(plan, provider, cache_dir=tmp_path, max_queries=3)
    assert [node["description"] for node in verified["nodes"]] == before
    assert cost == {"provider": "aminer.paper_search", "unit_cost_cny": 0.01, "api_calls": 3, "cache_hits": 0, "total_cost_cny": 0.03}
    again, cached_cost = verify_related_work(plan, provider, cache_dir=tmp_path, max_queries=3)
    assert cached_cost["api_calls"] == 0 and cached_cost["cache_hits"] == 3
    assert provider.calls == 3
    cache_text = "\n".join(path.read_text(encoding="utf-8") for path in tmp_path.rglob("*.json"))
    assert "token" not in cache_text.casefold()
    assert again["nodes"][0]["metadata"]["url"].startswith("https://www.aminer.cn/pub/")


def test_runtime_contains_no_sample_specific_logic():
    source = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "paper_visualizer" / "related").glob("*.py")).casefold()
    assert all(marker not in source for marker in ("awm", "2608.25618", "1706.03762", "attention is all you need"))


def test_relation_classification_uses_current_paper_wording():
    assert planner_module._relation_for("We build on the encoder architecture proposed by Smith et al.")[0] == "foundation_inheritance"
    assert planner_module._relation_for("Prior approaches address the same problem with recurrent networks.")[0] == "same_problem"
    assert planner_module._relation_for("Unlike the baseline, our method removes recurrence.")[0] == "improvement_comparison"
    assert planner_module._relation_for("We evaluate on the benchmark dataset introduced in prior work.")[0] == "data_evaluation"
    assert planner_module._relation_for("We discuss the advantages over models such as [9].")[0] == "improvement_comparison"
    assert planner_module._relation_for("In the following sections, we describe our model.") is None
    assert planner_module._relation_for("This fact is widely known [1].") is None


def test_relation_evidence_does_not_join_separate_passages_to_invent_an_edge():
    evidence = {
        "evidence:1": {"verbatim_text": "Smith's study is listed here [1].", "section_id": "section:related"},
        "evidence:2": {"verbatim_text": "Unlike a different baseline, our method removes recurrence.", "section_id": "section:related"},
    }
    citation = {"raw_reference": "[1] A. Smith. A Listed Study. 2020."}

    assert planner_module._relation_evidence(
        ["evidence:1", "evidence:2"], evidence, {"section:related": "Related Work"}, citation
    ) is None


def test_validator_rejects_edge_kind_not_supported_by_bound_evidence():
    ir = _ir()
    plan = build_related_work_plan(ir, project_root=ROOT)
    plan["edges"][0]["kind"] = "data_evaluation"

    errors = validate_related_work_plan(ir, plan, ROOT)

    assert any("not explicitly supported" in error for error in errors)


def test_validator_rejects_old_outward_edge_direction():
    ir = _ir()
    plan = build_related_work_plan(ir, project_root=ROOT)
    edge = plan["edges"][0]
    edge["source_id"], edge["target_id"] = edge["target_id"], edge["source_id"]

    errors = validate_related_work_plan(ir, plan, ROOT)

    assert any("edge endpoint is unknown" in error for error in errors)


def test_page_component_builds_left_to_right_terminal_network():
    ir = _ir()
    plan = build_related_work_plan(ir, project_root=ROOT)
    evidence = {item["id"]: item for item in ir["evidence"]}

    component = _related_component(plan, evidence)

    assert component["direction"] == "source_to_current_paper"
    assert component["hubs"]
    assert component["current_node"]["position"]["x"] > max(node["position"]["x"] for node in component["nodes"])
    semantic = [edge for edge in component["graph_edges"] if edge["semantic"]]
    trunks = [edge for edge in component["graph_edges"] if not edge["semantic"]]
    assert all(edge["source_id"].startswith("related:") and edge["target_id"].startswith("hub:") for edge in semantic)
    assert all(edge["source_id"].startswith("hub:") and edge["target_id"] == plan["paper_id"] for edge in trunks)


def test_result_table_is_not_misread_as_dataset_or_evaluation_usage():
    text = "\n".join(["Parser", "Training", "WSJ 23 F1", "Prior system [2]", "88.3", "Other [3]", "90.4", "Model", "91.3", "Baseline", "92.1"])
    assert planner_module._is_table_like(text)
    assert planner_module._relation_for(text) is None


def test_grounded_related_nodes_remain_displayable_without_external_links():
    ir = _ir()
    for citation in ir["citations"]:
        citation["external_url"] = None
        citation["raw_reference"] = citation["raw_reference"].split(" https://", 1)[0]
    plan = build_related_work_plan(ir, project_root=ROOT)
    assert len(plan["nodes"]) == 2
    assert all(node["metadata"]["url"] is None for node in plan["nodes"])
    assert plan["review"]["status"] == "needs_review"
    assert not any(item.get("severity") == "blocker" for item in plan["review"]["needs_human_review"])


def test_awm_author_year_regression_has_grounded_entry_nodes(tmp_path: Path):
    source_path = ROOT / "tmp" / "pdfs" / "2608.25618.pdf"
    if not source_path.exists():
        return
    parsed = parse_source(
        source_path,
        artifact_dir=tmp_path / "awm",
        cache_dir=tmp_path / "cache",
        options=ParseOptions(render_pages=False, extract_images=False, force=True),
    )
    overlay = {
        "approval": {
            "status": "approved",
            "base_source_sha256": parsed["source"]["sha256"],
            "reviewer": "automated-regression-fixture",
            "reason": "Use the checked-in source PDF for a deterministic Related Work regression.",
        }
    }
    ir = build_paper_ir(parsed, overlay=overlay)
    plan = build_related_work_plan(ir, project_root=ROOT)
    grounded = [node for node in plan["nodes"] if node["description"]["evidence_ids"] and node["metadata"]["url"]]
    # The generic parser must recover at least one grounded entry without
    # inventing URLs for references that lack a DOI/arXiv identifier.
    assert len(grounded) >= 1
    m3 = next(node for node in plan["nodes"] if "m3docrag" in node["raw_reference"].casefold())
    assert m3["citation_id"] == "citation:0004"
    assert m3["description"]["evidence_ids"]
    evidence = {item["id"]: item["verbatim_text"] for item in ir["evidence"]}
    audit = next(item for item in ir["citations"] if "auditing data membership" in item["raw_reference"].casefold())
    audit_text = " ".join(evidence[item] for item in audit["cited_in_evidence_ids"])
    assert "Other diagnostics" in audit_text
    assert "reader-based diagnostic" not in audit_text
    temperature = next(item for item in ir["citations"] if "learning temperature policy" in item["raw_reference"].casefold())
    assert all(node["citation_id"] != temperature["id"] for node in plan["nodes"])
