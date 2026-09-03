from __future__ import annotations

import hashlib
import json
import copy
from pathlib import Path

import pytest

from paper_visualizer.modeling import ModelingError, build_paper_ir, model_parsed_file, reconstruct_evidence
from paper_visualizer.parsing import ParseOptions, parse_source
from paper_visualizer.validation import validate_ir


ROOT = Path(__file__).resolve().parents[1]
SOURCE_HASH = "a" * 64


def _block(page: int, order: int, text: str, role: str = "body") -> dict[str, object]:
    top = 40 + order * 40
    return {
        "id": f"block:p{page:04d}:{order:04d}",
        "text": text,
        "bbox": [40.0, float(top), 560.0, float(top + 30)],
        "order": order,
        "role": role,
        # Parse-only fields must not leak into strict Paper IR blocks.
        "font_size_median": 10.0,
        "coordinate_precision": "exact",
    }


def parsed_document(*, status: str = "passed") -> dict[str, object]:
    pages = [
        {
            "number": 1,
            "width": 600.0,
            "height": 800.0,
            "blocks": [
                _block(1, 0, "A General Paper Visualizer", "heading"),
                _block(1, 1, "Abstract", "heading"),
                _block(1, 2, "We introduce a grounded system and evaluate its behavior [1]."),
                _block(1, 3, "1 Method", "heading"),
                _block(1, 4, "We propose a deterministic evidence model for scientific documents."),
                _block(1, 5, "where x denotes the input vector."),
                _block(1, 6, "y = W x + b (1)", "formula"),
                _block(1, 7, "Figure 1: System overview", "caption"),
            ],
            "render_path": "/tmp/page-1.png",
        },
        {
            "number": 2,
            "width": 600.0,
            "height": 800.0,
            "blocks": [
                _block(2, 0, "2 Results", "heading"),
                _block(2, 1, "The reported score is 91.2 on the evaluation set."),
                _block(2, 2, "Table 1: Main results", "caption"),
                _block(2, 3, "References", "heading"),
                _block(2, 4, "[1] A. Author. Reliable Systems. 2024. https://example.org/paper", "reference"),
            ],
            "render_path": "/tmp/page-2.png",
        },
    ]
    return {
        "schema_version": "1.0.0",
        "stage_version": "1.0.0",
        "status": status,
        "input_hashes": [SOURCE_HASH],
        "source": {
            "kind": "local",
            "sha256": SOURCE_HASH,
            "page_count": 2,
            "local_pdf": "/tmp/paper.pdf",
            "original_url": None,
        },
        "paper": {
            "title": "A General Paper Visualizer",
            "authors": ["A. Researcher"],
            "year": 2026,
            "language": "en",
        },
        "pages": pages,
        "sections": [
            {
                "id": "section:abstract",
                "title": "Abstract",
                "level": 1,
                "page_start": 1,
                "page_end": 1,
                "block_ids": ["block:p0001:0001", "block:p0001:0002"],
                "heading_block_id": "block:p0001:0001",
            },
            {
                "id": "section:method",
                "title": "1 Method",
                "level": 1,
                "page_start": 1,
                "page_end": 1,
                "block_ids": [
                    "block:p0001:0003",
                    "block:p0001:0004",
                    "block:p0001:0005",
                    "block:p0001:0006",
                    "block:p0001:0007",
                ],
                "heading_block_id": "block:p0001:0003",
            },
            {
                "id": "section:results",
                "title": "2 Results",
                "level": 1,
                "page_start": 2,
                "page_end": 2,
                "block_ids": ["block:p0002:0000", "block:p0002:0001", "block:p0002:0002"],
                "heading_block_id": "block:p0002:0000",
            },
        ],
        "figures": [
            {
                "id": "figure:001",
                "label": "1",
                "caption": "Figure 1: System overview",
                "page": 1,
                "caption_block_ids": ["block:p0001:0007"],
                "bbox": [40.0, 320.0, 560.0, 350.0],
                "asset_id": "image:p0001:000",
                "asset_path": "/tmp/figure-1.png",
                "match_status": "page_unique",
            }
        ],
        "tables": [
            {
                "id": "table:001",
                "label": "1",
                "caption": "Table 1: Main results",
                "page": 2,
                "caption_block_ids": ["block:p0002:0002"],
                "bbox": [40.0, 120.0, 560.0, 150.0],
                "columns": ["Method", "Score"],
                "rows": [["Ours", 91.2]],
            }
        ],
        "formula_candidates": [
            {
                "id": "formula-candidate:001",
                "text": "y = W x + b (1)",
                "label": "1",
                "page": 1,
                "block_ids": ["block:p0001:0006"],
                "bbox": [40.0, 280.0, 560.0, 310.0],
            }
        ],
        "references": [
            {
                "id": "reference:0001",
                "raw_reference": "[1] A. Author. Reliable Systems. 2024. https://example.org/paper",
                "page": 2,
                "block_ids": ["block:p0002:0004"],
            }
        ],
    }


def test_builds_schema_valid_general_ir_with_strict_provenance():
    ir = build_paper_ir(parsed_document())

    assert validate_ir(ir, ROOT) == []
    assert ir["paper"]["title"] == "A General Paper Visualizer"
    assert "font_size_median" not in ir["pages"][0]["blocks"][0]
    assert "heading_block_id" not in ir["sections"][0]
    for item in ir["evidence"]:
        rebuilt, _ = reconstruct_evidence(ir["pages"], item["locator"])
        assert rebuilt == item["verbatim_text"]
        assert hashlib.sha256(rebuilt.encode()).hexdigest() == item["text_sha256"]
        assert item["provenance"]["kind"] == "paper_verbatim"
    assert all(claim["display_label"] == "原文摘录" for claim in ir["claims"])
    assert all(claim["provenance"]["kind"] == "paper_excerpt" for claim in ir["claims"])


def test_claims_use_complete_cross_block_sentences_with_continuous_evidence():
    document = copy.deepcopy(parsed_document())
    document["pages"][0]["blocks"][4]["text"] = "We propose a robust visual-"
    document["pages"][0]["blocks"][5]["text"] = "ization method that remains grounded in source evidence."

    ir = build_paper_ir(document)

    claim = next(item for item in ir["claims"] if "robust visualization" in item["summary"])
    assert claim["summary"].endswith(".")
    assert "…" not in claim["summary"]
    evidence = next(item for item in ir["evidence"] if item["id"] == claim["evidence_ids"][0])
    assert evidence["verbatim_text"] == "We propose a robust visual- ization method that remains grounded in source evidence."
    assert evidence["locator"]["block_ids"] == ["block:p0001:0004", "block:p0001:0005"]
    rebuilt, _ = reconstruct_evidence(ir["pages"], evidence["locator"])
    assert rebuilt == evidence["verbatim_text"]


def test_claim_candidates_reject_author_email_metadata_that_ends_in_initial():
    document = copy.deepcopy(parsed_document())
    document["pages"][0]["blocks"][2]["text"] = (
        "Alice Researcher University alice@example.org Bob Scientist Laboratory bob@example.org Aidan N."
    )

    ir = build_paper_ir(document)

    assert ir["claims"]
    assert all("@" not in item["summary"] for item in ir["claims"])
    assert all(not item["summary"].endswith("Aidan N.") for item in ir["claims"])


def test_claims_repair_collapsed_math_boundaries_and_reject_merged_footnotes():
    document = copy.deepcopy(parsed_document())
    document["pages"][0]["blocks"][4]["text"] = (
        "We show that the input has dimensiondk and valuesh when the mean exceedsr10. "
        "We improve the output in parallel, yieldingdv-dimensional 4To explain the footnote text."
    )

    ir = build_paper_ir(document)

    summaries = [item["summary"] for item in ir["claims"]]
    assert any("dimension dk" in item and "values h" in item and "exceeds r10" in item for item in summaries)
    assert all("4To" not in item for item in summaries)


def test_claims_preserve_compound_hyphens_and_reject_short_colon_tail():
    document = copy.deepcopy(parsed_document())
    document["pages"][0]["blocks"][4]["text"] = (
        "We show that agent-\nwritten memory avoids memory-missing-\ncorrect failures. "
        "We improve answer-\ning while keeping LONG-\nDOCURL stable. "
        "We induce three preferences: Memory refinement."
    )

    ir = build_paper_ir(document)
    summaries = [item["summary"] for item in ir["claims"]]

    assert any("agent-written" in item and "memory-missing-correct" in item for item in summaries)
    assert any("answering" in item and "LONGDOCURL" in item for item in summaries)
    assert all(not item.endswith("preferences: Memory refinement.") for item in summaries)


def test_repeated_claim_sentences_are_deduplicated_across_sections():
    document = copy.deepcopy(parsed_document())
    repeated = "We propose a source-grounded method that improves reliability across tasks."
    document["pages"][0]["blocks"][4]["text"] = repeated
    duplicate = copy.deepcopy(document["pages"][0]["blocks"][4])
    duplicate.update(id="block:p0002:0005", order=5, bbox=[40.0, 350.0, 560.0, 380.0], text=repeated + " [1]")
    document["pages"][1]["blocks"].append(duplicate)
    document["sections"].append({
        "id": "section:conclusion", "title": "Conclusion", "level": 1,
        "page_start": 2, "page_end": 2, "block_ids": [duplicate["id"]],
    })

    ir = build_paper_ir(document)
    matching = [item for item in ir["claims"] if "source-grounded method" in item["summary"]]

    assert len(matching) == 1


def test_models_formula_variables_visuals_citations_and_relations():
    ir = build_paper_ir(parsed_document())

    formula = ir["formulas"][0]
    variable = next(item for item in ir["variables"] if item["symbol"] == "x")
    assert variable["id"] in formula["variable_ids"]
    assert formula["id"] in variable["formula_ids"]
    assert next(item for item in ir["evidence"] if item["id"] == variable["definition_evidence_id"])["locator"]["page"] == 1
    assert any(item["kind"] == "paper_original" for item in ir["visuals"])
    assert not any(item.get("visual_type") == "network" for item in ir["visuals"])
    assert ir["citations"][0]["cited_in_evidence_ids"]
    assert ir["citations"][0]["external_url"] == "https://example.org/paper"
    assert {item["type"] for item in ir["relations"]} >= {"supports", "defines", "mentions", "visualizes", "cites"}


def test_formula_variables_support_subscripts_greek_and_grouped_where_definitions():
    document = copy.deepcopy(parsed_document())
    document["pages"][0]["blocks"][5]["text"] = (
        "where d_k and d_v denote key and value dimensions, d_model is the model width, "
        "h represents the number of heads, and β_2 = 0.98."
    )
    formula_text = "d_k = d_v = d_model / h + β_2 (1)"
    document["pages"][0]["blocks"][6]["text"] = formula_text
    document["formula_candidates"][0]["text"] = formula_text

    ir = build_paper_ir(document)

    symbols = {item["symbol"] for item in ir["variables"]}
    assert {"d_{k}", "d_{v}", "d_{model}", "h", "β_2"}.issubset(symbols)
    formula = ir["formulas"][0]
    assert len(formula["variable_ids"]) == 5
    evidence = {item["id"]: item for item in ir["evidence"]}
    for variable in ir["variables"]:
        assert "where" in evidence[variable["definition_evidence_id"]]["verbatim_text"].casefold()


def test_formula_variables_restore_compact_indices_and_lhs_symbols():
    document = copy.deepcopy(parsed_document())
    document["pages"][0]["blocks"][5]["text"] = "where Ai is normalized advantage, σg is the group deviation, and τi denotes trajectory i."
    document["pages"][0]["blocks"][6]["text"] = "A11-A10 = Ai + r(τi)/(σg+ε) (1)"
    document["formula_candidates"][0]["text"] = document["pages"][0]["blocks"][6]["text"]

    ir = build_paper_ir(document)

    symbols = [item["symbol"] for item in ir["variables"]]
    assert {"A_i", "τ_i", "σ_g", "A_11", "A_10"}.issubset(symbols)
    assert len(symbols) == len(set(symbols))
    formula = ir["formulas"][0]
    assert "A_i" in formula["latex"] and "A_{11}" in formula["latex"] and "\\sigma_g" in formula["latex"]


def test_formula_variables_restore_subscript_after_misordered_exponent():
    document = copy.deepcopy(parsed_document())
    document["pages"][0]["blocks"][5]["text"] = "where dmodel is the model width and warmup_steps is the warmup duration."
    document["pages"][0]["blocks"][6]["text"] = "lrate = d−0.5 model · warmup_steps−1.5 (1)"
    document["formula_candidates"][0]["text"] = document["pages"][0]["blocks"][6]["text"]

    ir = build_paper_ir(document)

    symbols = {item["symbol"] for item in ir["variables"]}
    assert "d_{model}" in symbols
    assert "d" not in symbols and "model" not in symbols
    assert "d_{model}^-0.5" in ir["formulas"][0]["latex"]


def test_variable_symbol_does_not_match_inside_a_prose_word():
    document = copy.deepcopy(parsed_document())
    document["pages"][0]["blocks"][5]["text"] = "All values enter the softmax before illegal connections are masked."
    document["pages"][0]["blocks"][6]["text"] = "FFN(x) = max(0, x W) (1)"
    document["formula_candidates"][0]["text"] = "FFN(x) = max(0, x W) (1)"

    ir = build_paper_ir(document)

    formula = ir["formulas"][0]
    linked = {item["symbol"] for item in ir["variables"] if item["id"] in formula["variable_ids"]}
    assert "x" not in linked


def test_where_definition_does_not_match_single_letter_inside_a_prose_word():
    document = copy.deepcopy(parsed_document())
    document["pages"][0]["blocks"][5]["text"] = (
        "where the weight assigned to each value is computed by a compatibility function."
    )
    document["pages"][0]["blocks"][6]["text"] = "z = O + 1 (1)"
    document["formula_candidates"][0]["text"] = "z = O + 1 (1)"

    ir = build_paper_ir(document)

    formula = ir["formulas"][0]
    linked = {item["symbol"] for item in ir["variables"] if item["id"] in formula["variable_ids"]}
    assert "O" not in linked


def test_membership_expression_can_define_a_formula_variable():
    document = copy.deepcopy(parsed_document())
    document["pages"][0]["blocks"][5]["text"] = "Projection O ∈ Rhdv×dmodel."
    document["pages"][0]["blocks"][6]["text"] = "z = O + 1 (1)"
    document["formula_candidates"][0]["text"] = "z = O + 1 (1)"

    ir = build_paper_ir(document)

    variable = next(item for item in ir["variables"] if item["symbol"] == "O")
    evidence = next(item for item in ir["evidence"] if item["id"] == variable["definition_evidence_id"])
    assert "O ∈" in evidence["verbatim_text"]


def test_unresolved_formula_symbols_are_not_fabricated_and_create_review_items():
    ir = build_paper_ir(parsed_document())

    symbols = {item["symbol"] for item in ir["variables"]}
    assert "W" not in symbols and "b" not in symbols
    unresolved = {item["symbol"] for item in ir["review"]["items"] if item["kind"] == "variable_definition_missing"}
    assert {"W", "b"}.issubset(unresolved)
    assert ir["review"]["status"] == "needs_review"


def test_attention_real_parse_produces_interactive_formula_variables(tmp_path: Path):
    document = parse_source(
        ROOT / "tmp" / "pdfs" / "1706.03762.pdf",
        artifact_dir=tmp_path / "attention",
        cache_dir=tmp_path / "cache",
        options=ParseOptions(render_pages=False, extract_images=False, force=True),
    )
    overlay = {
        "approval": {
            "status": "approved",
            "base_source_sha256": document["source"]["sha256"],
            "reviewer": "automated-regression-fixture",
            "reason": "Use the checked-in parse artifact for a deterministic modeling regression.",
        }
    }

    ir = build_paper_ir(document, overlay=overlay)

    symbols = {item["symbol"] for item in ir["variables"]}
    assert {"Q", "K", "V", "d_k", "d_v", "d_{model}", "h", "pos"}.issubset(symbols)
    assert len(ir["formulas"]) >= 7
    assert sum(bool(formula["variable_ids"]) for formula in ir["formulas"]) >= 5
    assert any(formula["latex"].startswith("Attention(Q,K,V") and len(formula["locator"]["block_ids"]) == 3 for formula in ir["formulas"])
    dimension_formula = next(formula for formula in ir["formulas"] if formula["latex"] == "d_k = d_v = d_{model}/h = 64")
    dimension_text, _ = reconstruct_evidence(ir["pages"], dimension_formula["locator"])
    assert dimension_text == "dk = dv = dmodel/h = 64"
    assert dimension_formula["locator"]["char_start"] > 0
    assert not any("Adam optimizer" in formula["latex"] for formula in ir["formulas"])
    assert len(symbols) == len(ir["variables"])
    learning_rate = next(formula for formula in ir["formulas"] if formula["latex"].startswith("lrate ="))
    linked = {item["symbol"] for item in ir["variables"] if item["id"] in learning_rate["variable_ids"]}
    assert "d_{model}" in linked and "d" not in linked and "model" not in linked
    assert validate_ir(ir, ROOT) == []


def test_table_cells_remain_unbound_until_explicit_body_evidence_overlay():
    base = build_paper_ir(parsed_document())
    assert base["tables"][0]["discussed_cells"] == []
    result_evidence = next(item["id"] for item in base["evidence"] if "91.2" in item["verbatim_text"])

    ir = build_paper_ir(
        parsed_document(),
        overlay={
            "tables": [
                {
                    "id": "table:0001",
                    "discussed_cells": [{"row": 0, "column": 1, "evidence_ids": [result_evidence]}],
                }
            ]
        },
    )
    assert ir["tables"][0]["discussed_cells"][0]["evidence_ids"] == [result_evidence]


def test_overlay_evidence_is_rebuilt_and_rejects_forged_or_discontinuous_text():
    base = build_paper_ir(parsed_document())
    existing = base["evidence"][0]
    with pytest.raises(ModelingError, match="does not match locator"):
        build_paper_ir(
            parsed_document(),
            overlay={"evidence": [{"id": existing["id"], "verbatim_text": "fabricated text"}]},
        )
    with pytest.raises(ModelingError, match="consecutive"):
        build_paper_ir(
            parsed_document(),
            overlay={
                "evidence": [
                    {
                        "id": "evidence:manual",
                        "locator": {
                            "page": 1,
                            "block_ids": ["block:p0001:0002", "block:p0001:0004"],
                            "char_start": 0,
                            "char_end": 20,
                        },
                    }
                ]
            },
        )


def test_nonpassed_parse_requires_source_bound_human_approval():
    with pytest.raises(ModelingError, match="approved human overlay"):
        build_paper_ir(parsed_document(status="needs_review"))
    ir = build_paper_ir(
        parsed_document(status="needs_review"),
        overlay={
            "approval": {
                "status": "approved",
                "base_source_sha256": SOURCE_HASH,
                "reviewer": "human-reviewer",
                "reason": "Confirmed the parser's estimated layout against the PDF.",
            }
        },
    )
    assert ir["source"]["sha256"] == SOURCE_HASH


def test_invalid_entity_types_and_table_coordinates_are_rejected():
    with pytest.raises(ModelingError, match="claim must reference Evidence only"):
        build_paper_ir(
            parsed_document(),
            overlay={
                "claims": [
                    {
                        "id": "claim:manual",
                        "summary": "Invalid claim",
                        "evidence_ids": ["formula:0001"],
                    }
                ]
            },
        )
    with pytest.raises(ModelingError, match="outside table"):
        build_paper_ir(
            parsed_document(),
            overlay={
                "tables": [
                    {
                        "id": "table:0001",
                        "discussed_cells": [{"row": 99, "column": 1, "evidence_ids": ["evidence:00005"]}],
                    }
                ]
            },
        )


def test_claim_semantic_gate_rejects_truncated_generated_summary():
    with pytest.raises(ModelingError, match="complete sentence"):
        build_paper_ir(
            parsed_document(),
            overlay={"claims": [{"id": "claim:0001", "summary": "This generated claim is truncated-"}]},
        )


def test_human_rewrite_is_reclassified_as_generated_summary():
    ir = build_paper_ir(
        parsed_document(),
        overlay={"claims": [{"id": "claim:0001", "summary": "A human-written summary remains explicitly grounded in the original Evidence."}]},
    )
    claim = ir["claims"][0]
    assert claim["display_label"] == "生成式总结"
    assert claim["provenance"]["kind"] == "generated_summary"


def test_generated_summary_must_not_duplicate_source_excerpt():
    base = build_paper_ir(parsed_document())
    source_claim = base["claims"][0]
    with pytest.raises(ModelingError, match="generated summary must differ"):
        build_paper_ir(
            parsed_document(),
            overlay={
                "claims": [
                    {
                        "id": source_claim["id"],
                        "display_label": "生成式总结",
                        "provenance": {
                            "kind": "generated_summary",
                            "agent": "knowledge-modeling:human-overlay",
                            "source_ids": source_claim["evidence_ids"],
                            "verification": "verified",
                        },
                    }
                ]
            },
        )


def test_claim_cannot_spoof_a_paper_excerpt_label():
    with pytest.raises(ModelingError, match="paper excerpt must match"):
        build_paper_ir(
            parsed_document(),
            overlay={
                "claims": [
                    {
                        "id": "claim:manual",
                        "summary": "This text was not copied from the referenced Evidence sentence.",
                        "display_label": "原文摘录",
                        "evidence_ids": ["evidence:00001"],
                        "importance": "secondary",
                        "provenance": {
                            "kind": "paper_excerpt",
                            "agent": "knowledge-modeling:human-overlay",
                            "source_ids": ["evidence:00001"],
                            "verification": "verified",
                        },
                    }
                ]
            },
        )


def test_stale_formula_overlay_is_quarantined_after_formula_order_changes():
    ir = build_paper_ir(
        parsed_document(),
        overlay={
            "formulas": [{"id": "formula:0001", "variable_ids": ["variable:manual"]}],
            "variables": [
                {
                    "id": "variable:manual",
                    "symbol": "unrelated_symbol",
                    "definition_evidence_id": "evidence:00001",
                    "formula_ids": ["formula:0001"],
                }
            ],
        },
    )

    assert "variable:manual" not in {item["id"] for item in ir["variables"]}
    assert any(item["kind"] == "stale_formula_overlay" for item in ir["review"]["items"])
    assert ir["review"]["status"] == "needs_review"


def test_model_parsed_file_writes_reusable_json(tmp_path: Path):
    parsed_path = tmp_path / "parsed_document.json"
    output_path = tmp_path / "ir" / "paper_ir.json"
    parsed_path.write_text(json.dumps(parsed_document()), encoding="utf-8")

    ir = model_parsed_file(parsed_path, output_path=output_path)

    assert output_path.is_file()
    assert json.loads(output_path.read_text(encoding="utf-8")) == ir


def test_author_year_citations_bind_across_adjacent_body_blocks():
    parsed = parsed_document()
    parsed["pages"][1]["blocks"][1]["text"] = "Prior systems follow Smith et"
    parsed["pages"][1]["blocks"].append(
        {
            "id": "block:p0002:0010",
            "text": "al., 2020; Jones et al., 2021).",
            "bbox": [40.0, 340.0, 560.0, 360.0],
            "order": 10,
            "role": "body",
        }
    )
    parsed["sections"][2]["block_ids"].append("block:p0002:0010")
    parsed["references"] = [
        {"id": "reference:smith", "raw_reference": "Alice Smith, Bob Doe, and Cora Ray. 2020. Grounded Retrieval. TestConf.", "page": 2, "block_ids": ["block:p0002:0004"]},
        {"id": "reference:jones", "raw_reference": "David Jones and Erin Poe. 2021. Reliable Navigation. TestConf.", "page": 2, "block_ids": ["block:p0002:0004"]},
    ]
    ir = build_paper_ir(parsed)
    assert len(ir["citations"]) == 2
    assert ir["citations"][0]["raw_reference"].startswith("Alice Smith")
    assert ir["citations"][1]["raw_reference"].startswith("David Jones")
    first_id = next(item["id"] for item in ir["evidence"] if item["verbatim_text"] == "Prior systems follow Smith et")
    second_id = next(item["id"] for item in ir["evidence"] if item["verbatim_text"] == "al., 2020; Jones et al., 2021).")
    assert all({first_id, second_id} <= set(item["cited_in_evidence_ids"]) for item in ir["citations"])


def test_numbered_reference_records_are_not_remerged_when_year_ends_entry():
    parsed = parsed_document()
    parsed["pages"][0]["blocks"][2]["text"] = "Prior systems use normalization and attention [1, 2]."
    parsed["references"] = [
        {
            "id": "reference:1",
            "raw_reference": "[1] A. Author. Layer Normalization. arXiv:1607.06450, 2016.",
            "page": 2,
            "block_ids": ["block:p0002:0004"],
        },
        {
            "id": "reference:2",
            "raw_reference": "[2] B. Author. Attention Study. arXiv:1409.0473, 2014.",
            "page": 2,
            "block_ids": ["block:p0002:0004"],
        },
    ]

    ir = build_paper_ir(parsed)

    assert len(ir["citations"]) == 2
    assert ir["citations"][0]["raw_reference"].startswith("[1]")
    assert ir["citations"][1]["raw_reference"].startswith("[2]")
    assert all("[1]" not in item["raw_reference"] or "[2]" not in item["raw_reference"] for item in ir["citations"])


def test_numbered_references_are_split_when_parser_merges_many_entries():
    parsed = parsed_document()
    parsed["pages"][0]["blocks"][2]["text"] = "Prior systems address this task [1, 2]."
    parsed["references"] = [{
        "id": "reference:merged",
        "raw_reference": (
            "[1] Alice Author. 2020. First Grounded System. Test Venue. "
            "[2] Bob Builder. 2021. Second Grounded System. Other Venue."
        ),
        "page": 2,
        "block_ids": ["block:p0002:0004"],
    }]

    ir = build_paper_ir(parsed)

    assert [item["title"] for item in ir["citations"]] == [
        "First Grounded System",
        "Second Grounded System",
    ]
    assert all(item["cited_in_evidence_ids"] for item in ir["citations"])
