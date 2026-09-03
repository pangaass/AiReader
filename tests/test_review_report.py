from __future__ import annotations

import copy
import json
from pathlib import Path

from paper_visualizer.review import reviewer


ROOT = Path(__file__).resolve().parents[1]


def _check(status: str = "passed", severity: str = "blocker") -> dict:
    return {
        "id": "schema.contract",
        "category": "schema",
        "severity": severity,
        "status": status,
        "message": "Schema contract check.",
        "evidence": [],
    }


def test_report_aggregation_is_schema_valid_and_read_only(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(reviewer, "run_static_checks", lambda *args, **kwargs: [_check()])
    ir = {"paper": {"id": "paper:fixture"}}
    model = {"paper": {"id": "paper:fixture"}}
    original_ir, original_model = copy.deepcopy(ir), copy.deepcopy(model)
    browser = {item["id"]: {"status": "passed", "details": "checked"} for item in reviewer.BROWSER_HOOKS}
    browser["base_html_sha256"] = reviewer._digest_text("<!doctype html>")

    report = reviewer.review_artifacts(ir, model, "<!doctype html>", browser_results=browser, project_root=ROOT)

    assert report["status"] == "failed"  # supplemental interaction/mobile gates inspect the actual HTML
    assert report["summary"]["blocker_count"] == 2
    assert report["summary"]["warning_count"] == 0
    assert report["summary"]["passed_count"] >= 6
    assert report["summary"]["skipped_count"] == 0
    assert reviewer.validate_review_report(report, ROOT) == []
    assert ir == original_ir and model == original_model
    destination = tmp_path / "review_report.json"
    reviewer.write_review_report(destination, report, project_root=ROOT)
    assert json.loads(destination.read_text(encoding="utf-8")) == report


def test_blocker_fails_report_and_pending_browser_is_warning(monkeypatch):
    monkeypatch.setattr(reviewer, "run_static_checks", lambda *args, **kwargs: [_check("failed")])
    report = reviewer.review_artifacts(
        {"paper": {"id": "paper:fixture"}},
        {"paper": {"id": "paper:fixture"}},
        "<html></html>",
        project_root=ROOT,
    )
    assert report["status"] == "failed"
    assert report["summary"]["blocker_count"] >= 1
    assert report["summary"]["warning_count"] == len(reviewer.BROWSER_HOOKS)
    assert report["summary"]["skipped_count"] == len(reviewer.BROWSER_HOOKS)


def test_required_browser_checks_become_blockers(monkeypatch):
    monkeypatch.setattr(reviewer, "run_static_checks", lambda *args, **kwargs: [_check()])
    report = reviewer.review_artifacts(
        {"paper": {"id": "paper:fixture"}},
        {"paper": {"id": "paper:fixture"}},
        "<html></html>",
        require_browser=True,
        project_root=ROOT,
    )
    assert report["status"] == "failed"
    assert report["summary"]["blocker_count"] >= len(reviewer.BROWSER_HOOKS)


def test_stale_browser_results_fail_strict_review(monkeypatch):
    monkeypatch.setattr(reviewer, "run_static_checks", lambda *args, **kwargs: [_check()])
    browser = {item["id"]: {"status": "passed", "details": "checked"} for item in reviewer.BROWSER_HOOKS}
    browser["base_html_sha256"] = "0" * 64
    report = reviewer.review_artifacts(
        {"paper": {"id": "paper:fixture"}},
        {"paper": {"id": "paper:fixture"}},
        "<!doctype html>",
        browser_results=browser,
        require_browser=True,
        project_root=ROOT,
    )
    binding = next(item for item in report["checks"] if item["id"] == "browser.artifact_binding")
    assert binding["status"] == "failed"
    assert report["status"] == "failed"


def test_supplemental_review_rejects_fragment_summaries_and_missing_numbered_formula():
    ir = {
        "claims": [{"id": "claim:0001", "summary": "mechanism. We propose an architecture"}],
        "formulas": [],
        "source": {"page_count": 1},
    }
    parsed = {
        "formula_candidates": [
            {"id": "formula-candidate:001", "label": "1", "block_ids": ["block:p0001:0002"]}
        ]
    }
    findings = reviewer._supplemental_checks(
        ir,
        {"sections": [], "evidence": {}},
        '<meta name="viewport"><script>data-evidence-id event.key===\'Enter\' event.key===\' \' event.key===\'Escape\' .focus()</script>',
        parsed_document=None,
        content_plan=None,
        visual_plan=None,
        related_work=None,
    )
    ids = {item["id"] for item in findings}
    assert "evidence:summary:claim:0001" in ids

    findings = reviewer._supplemental_checks(
        ir,
        {"sections": [], "evidence": {}},
        '<meta name="viewport"><script>data-evidence-id event.key===\'Enter\' event.key===\' \' event.key===\'Escape\' .focus()</script>',
        content_plan=None,
        visual_plan=None,
        related_work=None,
        parsed_document=parsed,
    )
    assert any(item["id"] == "formula:labeled-coverage:formula-candidate:001" for item in findings)


def test_supplemental_review_distinguishes_math_rich_rows_from_prose():
    ir = {
        "claims": [],
        "formulas": [
            {
                "id": "formula:math-row",
                "label": None,
                "latex": r"Useful-memory failure A_{01}−A_{00}=(\gamma−\omega)/(\sigma_g+\epsilon), (0,1)≻(0,0)",
            },
            {
                "id": "formula:prose",
                "label": None,
                "latex": r"We used the Adam optimizer with \beta_1=0.9, \beta_2=0.98 and \epsilon=10^{-9}. We varied the learning rate.",
            },
        ],
        "source": {"page_count": 1},
    }
    findings = reviewer._supplemental_checks(
        ir,
        {"sections": [], "evidence": {}},
        '<meta name="viewport"><script>data-evidence-id event.key===\'Enter\' event.key===\' \' event.key===\'Escape\' .focus()</script>',
        parsed_document=None,
        content_plan=None,
        visual_plan=None,
        related_work=None,
    )
    ids = {item["id"] for item in findings}
    assert "formula:prose:formula:math-row" not in ids
    assert "formula:prose:formula:prose" in ids
