from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest import mock

import pytest

from paper_visualizer.orchestration import pipeline
from paper_visualizer.orchestration.pipeline import PipelineOptions, run_pipeline
from paper_visualizer.parsing import ParseError
from paper_visualizer.review import review_runtime_sources


ROOT = Path(__file__).resolve().parents[1]
PAPERS = (
    ("tmp/pdfs/2608.25618.pdf", "integration-paper-a"),
    ("tmp/pdfs/1706.03762.pdf", "integration-paper-b"),
)


def _isolated_project(tmp_path: Path) -> Path:
    root = tmp_path / "paper-visualizer"
    shutil.copytree(ROOT / "schemas", root / "schemas")
    shutil.copytree(ROOT / "templates", root / "templates")
    return root


@pytest.mark.parametrize(("relative_pdf", "paper_id"), PAPERS)
def test_real_papers_complete_the_same_pipeline_and_save_all_artifacts(
    tmp_path: Path, relative_pdf: str, paper_id: str,
):
    project = _isolated_project(tmp_path)
    output = project / "output" / f"{paper_id}.html"
    progress: list[dict[str, object]] = []
    manifest = run_pipeline(
        ROOT / relative_pdf,
        project_root=project,
        output_path=output,
        options=PipelineOptions(
            paper_id=paper_id,
            render_pages=False,
            extract_images=False,
            embed_local_pdf=False,
        ),
        progress=progress.append,
    )

    expected = {
        "parsed", "ir", "content_plan", "visual_plan", "related_work",
        "page_model", "html", "review_report",
    }
    assert set(manifest["artifacts"]) == expected
    assert all(Path(path).is_file() for path in manifest["artifacts"].values())
    assert output.read_text(encoding="utf-8").startswith("<!doctype html>")
    assert manifest["status"] == "review_failed"
    assert manifest["review"]["status"] in {"passed", "failed"}
    report = json.loads(Path(manifest["artifacts"]["review_report"]).read_text(encoding="utf-8"))
    assert not any("EVIDENCE_REGISTRY_INVALID" in item["evidence"] for item in report["checks"])
    assert not any("A11Y_IMAGE_ALT" in item["evidence"] for item in report["checks"])
    visual_plan = json.loads(Path(manifest["artifacts"]["visual_plan"]).read_text(encoding="utf-8"))
    assert not any(item.get("visual_type") == "relation" for item in visual_plan["visuals"])
    page_model = json.loads(Path(manifest["artifacts"]["page_model"]).read_text(encoding="utf-8"))
    related = next(item for item in page_model["sections"] if item["kind"] == "existing_methods")
    assert not any(item.get("component") in {"citation", "excerpt", "summary"} for item in related["components"])
    completed = [event["stage"] for event in progress if event["status"] == "completed"]
    assert completed == ["parse", "model", "content", "visual", "related", "page", "render", "review", "complete"]


def test_second_run_uses_content_addressed_parse_cache(tmp_path: Path):
    project = _isolated_project(tmp_path)
    options = PipelineOptions(
        paper_id="cache-check",
        render_pages=False,
        extract_images=False,
        allow_unreviewed=True,
        embed_local_pdf=False,
    )
    source = ROOT / PAPERS[1][0]
    run_pipeline(source, project_root=project, options=options)
    run_pipeline(source, project_root=project, options=options)
    parsed = json.loads((project / "artifacts/cache-check/parsed/parsed_document.json").read_text(encoding="utf-8"))
    assert parsed["cache_hit"] is True
    for stage in ("model", "content", "visual", "related", "page", "render", "review"):
        record = json.loads((project / f"artifacts/cache-check/orchestration/{stage}.json").read_text(encoding="utf-8"))
        assert record["cache_hit"] is True
        assert record["result"]["agent_role"].endswith("-lead")
        assert record["result"]["reviewed_by"].endswith("-verifier")
        assert record["task"]["input_artifacts"]

    strict = PipelineOptions(
        paper_id="cache-check",
        render_pages=False,
        extract_images=False,
        allow_unreviewed=True,
        embed_local_pdf=False,
        require_browser_review=True,
    )
    manifest = run_pipeline(source, project_root=project, options=strict)
    review_record = json.loads((project / "artifacts/cache-check/orchestration/review.json").read_text(encoding="utf-8"))
    assert review_record["cache_hit"] is False
    assert manifest["status"] == "review_failed"


def test_parse_stage_retries_retryable_failures(tmp_path: Path):
    parsed = {"status": "passed", "source": {"sha256": "a" * 64}}
    calls: list[int] = []

    def flaky(*args, **kwargs):
        calls.append(kwargs["options"].attempt)
        if len(calls) < 3:
            raise ParseError("temporary parser failure")
        return parsed

    with mock.patch.object(pipeline, "parse_source", side_effect=flaky), mock.patch.object(pipeline.time, "sleep") as sleep:
        result = pipeline._run_parse(
            "paper.pdf",
            tmp_path / "artifacts",
            tmp_path / "cache",
            PipelineOptions(max_attempts=3),
        )
    assert result is parsed
    assert calls == [1, 2, 3]
    assert sleep.call_count == 2


def test_templates_and_runtime_have_no_sample_specific_logic():
    report = review_runtime_sources(ROOT)
    assert report.passed, report.to_dict()
