from __future__ import annotations

from types import SimpleNamespace

from paper_visualizer.extraction import (
    HARNESS_VERSION,
    apply_semantic_extraction,
    build_parsed_context,
    extract_semantic_content,
    validate_semantic_extraction,
)


def _parsed() -> dict[str, object]:
    return {
        "source": {"sha256": "a" * 64},
        "paper": {"title": "Grounded Extraction"},
        "pages": [
            {
                "number": 1,
                "blocks": [
                    {
                        "id": "block:p0001:0001",
                        "role": "body",
                        "text": "We propose a deterministic evidence model for scientific documents.",
                    }
                ],
            },
            {
                "number": 2,
                "blocks": [
                    {
                        "id": "block:p0002:0001",
                        "role": "body",
                        "text": "The reported score is 91.2 on the evaluation set.",
                    }
                ],
            },
        ],
    }


def _ir() -> dict[str, object]:
    evidence = [
        {
            "id": "evidence:0001",
            "verbatim_text": "We propose a deterministic evidence model for scientific documents.",
            "locator": {"page": 1, "block_ids": ["block:p0001:0001"]},
        },
        {
            "id": "evidence:0002",
            "verbatim_text": "The reported score is 91.2 on the evaluation set.",
            "locator": {"page": 2, "block_ids": ["block:p0002:0001"]},
        },
    ]
    return {
        "evidence": evidence,
        "claims": [
            {
                "id": "claim:0001",
                "summary": "A deterministic fallback claim remains available.",
                "evidence_ids": ["evidence:0001"],
            }
        ],
        "relations": [],
        "review": {"status": "pending", "items": []},
    }


def test_parsed_context_preserves_page_and_block_provenance():
    context = build_parsed_context(_parsed())
    assert "## Page 1" in context
    assert "## Page 2" in context
    assert "[block:p0001:0001 role=body]" in context
    assert "The reported score is 91.2" in context


def test_semantic_extraction_runs_optimized_shape_without_presentation(tmp_path):
    class FakeRunner:
        model = "fake-glm"

        def run(self, prompt, cwd, **kwargs):
            assert "You cannot see the paired presentation" in prompt
            assert "source_sha256" in prompt
            return SimpleNamespace(
                data={
                    "paper_id": "paper-1",
                    "title": "Grounded Extraction",
                    "overview": [
                        {
                            "role": "method",
                            "summary": "The paper introduces a deterministic evidence model.",
                            "evidence": [
                                {
                                    "page": 1,
                                    "quote": "We propose a deterministic evidence model for scientific documents.",
                                }
                            ],
                        }
                    ],
                    "sections": [],
                    "visual_references": [],
                    "warnings": [],
                },
                cost_usd=0.01,
            )

    result = extract_semantic_content(
        _parsed(), paper_id="paper-1", runner=FakeRunner(), cwd=tmp_path
    )
    assert result["harness_version"] == HARNESS_VERSION
    assert result["status"] == "complete"
    assert result["coverage"] == {
        "page_count": 2,
        "attempted_units": 1,
        "succeeded_units": 1,
        "failed_units": 0,
        "retried_units": 0,
        "recovered_units": 0,
    }
    assert validate_semantic_extraction(result) == []


def test_only_same_page_verbatim_supported_summaries_enter_paper_ir():
    extraction = {
        "harness_version": HARNESS_VERSION,
        "instruction_sha256": "b" * 64,
        "overview": [
            {
                "summary": "The method uses a deterministic evidence model for scientific documents",
                "evidence": [
                    {
                        "page": 1,
                        "quote": "We propose a deterministic evidence model for scientific documents.",
                    }
                ],
            },
            {
                "summary": "This unsupported result must not be published",
                "evidence": [{"page": 1, "quote": "The score improves by one hundred points."}],
            },
            {
                "summary": "A real quote on the wrong page must also be rejected",
                "evidence": [{"page": 1, "quote": "The reported score is 91.2 on the evaluation set."}],
            },
        ],
        "sections": [],
    }
    result = apply_semantic_extraction(_ir(), extraction)
    harness_claims = [
        item for item in result["claims"] if item["id"].startswith("claim:0000-harness")
    ]
    assert len(harness_claims) == 1
    assert harness_claims[0]["evidence_ids"] == ["evidence:0001"]
    assert harness_claims[0]["provenance"]["verification"] == "pending"
    assert result["claims"][0] == harness_claims[0]
    audit = result["review"]["items"][-1]
    assert audit["matched_claims"] == 1
    assert audit["rejected_unverified_claims"] == 2
    assert len(result["relations"]) == 1


def test_failed_concurrent_unit_is_retried_without_repeating_success(tmp_path):
    parsed = _parsed()
    parsed["pages"][0]["blocks"][0]["text"] = "A" * 9000
    parsed["pages"][1]["blocks"][0]["text"] = "B" * 9000

    class FlakyRunner:
        model = "fake-glm"

        def __init__(self):
            self.calls: dict[str, int] = {}

        def run(self, prompt, cwd, **kwargs):
            label = "pages-1-1" if "Current unit: pages-1-1" in prompt else "pages-2-2"
            self.calls[label] = self.calls.get(label, 0) + 1
            if label == "pages-2-2" and self.calls[label] == 1:
                raise ConnectionError("stream closed")
            return SimpleNamespace(
                data={
                    "paper_id": "paper-1",
                    "title": None,
                    "overview": [],
                    "sections": [],
                    "visual_references": [],
                    "warnings": [],
                },
                cost_usd=0.0,
            )

    runner = FlakyRunner()
    result = extract_semantic_content(
        parsed, paper_id="paper-1", runner=runner, cwd=tmp_path
    )
    assert runner.calls == {"pages-1-1": 1, "pages-2-2": 2}
    assert result["coverage"]["succeeded_units"] == 2
    assert result["coverage"]["failed_units"] == 0
    assert result["coverage"]["retried_units"] == 1
    assert result["coverage"]["recovered_units"] == 1
