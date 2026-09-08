from alignment_harness.llm import AgentResult, parse_json_payload
from alignment_harness.pipeline import AlignmentPipeline, PairRun, deterministic_score


def test_parse_json_payload_accepts_claude_envelope_and_fence():
    payload = {"type": "result", "result": "```json\n{\"ok\": true}\n```"}
    assert parse_json_payload(payload) == {"ok": True}


def test_parse_json_payload_repairs_minor_model_damage():
    assert parse_json_payload('{"ok": true, "items": [1, 2,],}') == {
        "ok": True,
        "items": [1, 2],
    }


def test_parse_json_payload_prefers_structured_output():
    assert parse_json_payload(
        {"type": "result", "result": "not json", "structured_output": {"ok": True}}
    ) == {"ok": True}


def test_deterministic_score_reports_structure_and_evidence():
    presentation = {"overview": [{}, {}], "sections": [{}, {}]}
    paper = {
        "overview": [
            {"evidence": [{"quote": "supported"}]},
            {"evidence": []},
        ],
        "sections": [{}],
    }
    assert deterministic_score(presentation, paper) == {
        "overview_count_recall": 1.0,
        "section_count_recall": 0.5,
        "overview_evidence_rate": 0.5,
    }


def test_meta_loop_keeps_best_instruction_when_candidate_regresses(tmp_path):
    class FakeRunner:
        model = "fake"

        def run(self, *args, **kwargs):
            return AgentResult(
                {
                    "revised_instruction": "candidate",
                    "rationale": [],
                    "expected_improvements": [],
                    "risks": [],
                },
                {"total_cost_usd": 0, "duration_ms": 1, "session_id": "fake"},
            )

    class FakePipeline(AlignmentPipeline):
        def run_pair(self, pair, instruction, iteration, presentation_cache=None):
            score = 0.8 if iteration == 0 else 0.4
            return PairRun(
                pair_id=pair["id"],
                presentation={},
                paper={},
                audit={"overall_score": score},
                deterministic={},
                cost_usd=0,
            )

    pipeline = FakePipeline(tmp_path, tmp_path / "output", FakeRunner())
    summary = pipeline.run([{"id": "pair"}], iterations=2, initial_instruction="baseline")
    assert summary["selected_instruction"] == "baseline"
    assert (tmp_path / "output" / "selected_instruction.md").read_text().strip() == "baseline"
