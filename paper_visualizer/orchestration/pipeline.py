"""Restartable orchestration for the Paper Visualizer stages."""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from paper_visualizer.artifacts import atomic_write_json, sha256_file
from paper_visualizer.modeling import build_paper_ir
from paper_visualizer.parsing import IngestError, ParseError, ParseOptions, parse_source
from paper_visualizer.planning import build_content_plan, enrich_content_plan, validate_content_plan
from paper_visualizer.related import AMinerTitleSearch, apply_related_review_overlay, build_related_work_plan, validate_related_work_plan, verify_related_work
from paper_visualizer.rendering import build_page_model, render_html, validate_page_model
from paper_visualizer.review import review_artifacts, validate_review_report
from paper_visualizer.validation import validate_ir
from paper_visualizer.visuals import build_visual_plan, validate_visual_plan
from .runner import StageRunner, stable_digest


class PipelineError(RuntimeError):
    """Raised when an orchestrated stage cannot produce a usable artifact."""


@dataclass(frozen=True)
class PipelineOptions:
    paper_id: str | None = None
    force: bool = False
    render_pages: bool = True
    extract_images: bool = True
    render_dpi: int = 144
    max_attempts: int = 3
    verify_related_work: bool = False
    max_related_queries: int = 20
    allow_unreviewed: bool = True
    embed_local_pdf: bool = True
    require_browser_review: bool = False
    use_llm: bool = False


def _slug(value: str) -> str:
    result = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return result[:64] or "paper"


def _load_overlay(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise PipelineError(f"Overlay must be a JSON object: {path}")
    return value


def _tree_digest(paths: tuple[Path, ...]) -> str:
    entries = []
    for root in paths:
        if root.is_file():
            entries.append((root.name, sha256_file(root)))
        elif root.is_dir():
            entries.extend(
                (str(path.relative_to(root)), sha256_file(path))
                for path in sorted(root.rglob("*"))
                if path.is_file() and path.suffix in {".py", ".j2"}
            )
    return stable_digest(entries)


def _validate_rendered_html(value: object) -> list[str]:
    html = str(value)
    if not html.lstrip().casefold().startswith("<!doctype html>"):
        return ["rendered HTML shell is invalid"]
    match = re.search(
        r'<script type="application/json" id="evidenceRegistry">(.*?)</script>',
        html,
        re.DOTALL,
    )
    if match is None or "&#34;" in match.group(1):
        return ["rendered Evidence Registry is invalid"]
    try:
        registry = json.loads(match.group(1))
    except json.JSONDecodeError:
        return ["rendered Evidence Registry is invalid"]
    return [] if isinstance(registry, dict) else ["rendered Evidence Registry is invalid"]


def _run_parse(source: str | Path, paper_dir: Path, cache_dir: Path, options: PipelineOptions) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(1, max(1, options.max_attempts) + 1):
        try:
            return parse_source(
                source,
                artifact_dir=paper_dir,
                cache_dir=cache_dir,
                options=ParseOptions(
                    render_pages=options.render_pages,
                    extract_images=options.extract_images,
                    render_dpi=options.render_dpi,
                    attempt=attempt,
                    force=options.force,
                ),
            )
        except (IngestError, ParseError, OSError) as exc:
            last_error = exc
            if attempt < options.max_attempts:
                time.sleep(min(0.25 * 2 ** (attempt - 1), 2.0))
    raise PipelineError(f"PDF parsing failed after {options.max_attempts} attempts: {last_error}")


def run_pipeline(
    source: str | Path,
    *,
    project_root: Path,
    output_path: Path | None = None,
    options: PipelineOptions | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Run all production stages and persist every intermediate artifact."""

    options = options or PipelineOptions()
    def emit(stage: str, status: str, **details: Any) -> None:
        if progress is not None:
            progress({"stage": stage, "status": status, **details})

    root = project_root.resolve()
    source_name = Path(str(source).split("?", 1)[0]).stem
    paper_id = _slug(options.paper_id or source_name)
    paper_dir = root / "artifacts" / paper_id
    cache_dir = root / "cache"
    runner = StageRunner(paper_id=paper_id, paper_dir=paper_dir, cache_dir=cache_dir / "orchestrated", max_attempts=options.max_attempts, force=options.force)
    source_fingerprint = sha256_file(Path(source).expanduser().resolve()) if not str(source).startswith(("http://", "https://")) else stable_digest(str(source).split("?", 1)[0])
    parsed_path = paper_dir / "parsed" / "parsed_document.json"
    emit("parse", "running")
    parsed, parse_cache_hit = runner.run(
        stage="parse", stage_version="1.2.7", role="pdf-parse-lead", reviewed_by="pdf-parse-verifier",
        inputs={"source": source_fingerprint, "options": {"render_pages": options.render_pages, "extract_images": options.extract_images, "render_dpi": options.render_dpi}},
        output_path=parsed_path,
        producer=lambda task: parse_source(
            source, artifact_dir=paper_dir, cache_dir=cache_dir,
            options=ParseOptions(render_pages=options.render_pages, extract_images=options.extract_images, render_dpi=options.render_dpi, attempt=task.attempt, force=options.force),
        ),
        use_stage_cache=False,
        input_paths={"source": Path(source).expanduser().resolve()} if not str(source).startswith(("http://", "https://")) else {},
        retryable_exceptions=(IngestError, ParseError, OSError),
        validator=lambda value: [] if isinstance(value, dict) and value.get("source", {}).get("page_count") == len(value.get("pages", [])) and value.get("pages") else ["parsed document has invalid page coverage"],
        status_resolver=lambda value: "passed" if value.get("status") == "passed" else "needs_review",  # type: ignore[union-attr]
    )
    emit("parse", "completed", cache_hit=parse_cache_hit)
    assert isinstance(parsed, dict)

    overlay = _load_overlay(paper_dir / "review" / "human_review.json")
    if parsed.get("status") != "passed" and overlay is None and options.allow_unreviewed:
        overlay = {
            "approval": {
                "status": "approved",
                "base_source_sha256": parsed["source"]["sha256"],
                "reviewer": "preview-mode",
                "reason": "Explicit allow_unreviewed preview; final publication still requires review.",
            }
        }
        atomic_write_json(paper_dir / "review" / "preview_overlay.json", overlay)
    ir_path = paper_dir / "ir" / "paper_ir.json"
    emit("model", "running")
    ir, model_cache_hit = runner.run(
        stage="model", stage_version="1.3.2", role="knowledge-modeling-lead", reviewed_by="knowledge-modeling-verifier",
        inputs={"parsed": parsed, "overlay": overlay}, output_path=ir_path,
        input_paths={"parsed": parsed_path},
        producer=lambda task: build_paper_ir(parsed, overlay=overlay),
        validator=lambda value: validate_ir(dict(value), root),  # type: ignore[arg-type]
    )
    emit("model", "completed", cache_hit=model_cache_hit)
    assert isinstance(ir, dict)

    content_overlay = _load_overlay(paper_dir / "review" / "content_review.json")
    content_path = paper_dir / "plans" / "content_plan.json"
    llm_model = os.environ.get("OPENAI_MODEL") or os.environ.get("PAPER_READER_LLM_MODEL") or ""
    llm_endpoint = os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1"
    def produce_content(task: object) -> object:
        value = build_content_plan(ir, overlay=content_overlay, project_root=root)
        if not options.use_llm:
            return value
        api_key = os.environ.get("OPENAI_API_KEY") or ""
        if not api_key or not llm_model:
            raise PipelineError("构建叙事版 Visualizer 前请先配置 Endpoint、Token 和 Model Name")
        return enrich_content_plan(
            ir, value, api_key=api_key, model=llm_model, base_url=llm_endpoint,
            max_attempts=options.max_attempts,
        )
    emit("content", "running")
    content, content_cache_hit = runner.run(
        stage="content", stage_version="2.0.5", role="content-design-lead", reviewed_by="content-design-verifier",
        inputs={"ir": ir, "overlay": content_overlay, "llm": {"enabled": options.use_llm, "model": llm_model, "endpoint": llm_endpoint if options.use_llm else None}}, output_path=content_path,
        input_paths={"ir": ir_path},
        producer=produce_content,
        validator=lambda value: validate_content_plan(ir, value, root),  # type: ignore[arg-type]
        status_resolver=lambda value: "passed" if value.get("review", {}).get("status") == "passed" else "needs_review",  # type: ignore[union-attr]
    )
    emit("content", "completed", cache_hit=content_cache_hit)
    assert isinstance(content, dict)

    visual_path = paper_dir / "plans" / "visual_plan.json"
    emit("visual", "running")
    visuals, visual_cache_hit = runner.run(
        stage="visual", stage_version="1.0.1", role="visualization-lead", reviewed_by="visualization-verifier",
        inputs={"ir": ir, "content": content}, output_path=visual_path,
        input_paths={"ir": ir_path, "content": content_path},
        producer=lambda task: build_visual_plan(ir, content, project_root=root),
        validator=lambda value: validate_visual_plan(ir, content, value, root),  # type: ignore[arg-type]
        status_resolver=lambda value: "passed" if value.get("review", {}).get("status") == "passed" else "needs_review",  # type: ignore[union-attr]
    )
    emit("visual", "completed", cache_hit=visual_cache_hit)
    assert isinstance(visuals, dict)

    cost = {"provider": None, "api_calls": 0, "cache_hits": 0, "total_cost_cny": 0.0}
    related_overlay = _load_overlay(paper_dir / "review" / "related_review.json")
    related_path = paper_dir / "plans" / "related_work.json"
    def produce_related(task: object) -> object:
        nonlocal cost
        value = build_related_work_plan(ir, project_root=root)
        if options.verify_related_work:
            value, cost = verify_related_work(value, AMinerTitleSearch(), cache_dir=cache_dir / "related-metadata", max_attempts=options.max_attempts, max_queries=options.max_related_queries)
        if related_overlay is not None:
            value = apply_related_review_overlay(value, related_overlay, project_root=root, ir=ir)
        return value
    emit("related", "running")
    related, related_cache_hit = runner.run(
        stage="related", stage_version="1.3.2", role="related-work-lead", reviewed_by="related-work-verifier",
        inputs={"ir": ir, "overlay": related_overlay, "verify": options.verify_related_work, "max_queries": options.max_related_queries},
        input_paths={"ir": ir_path, "content": content_path}, output_path=related_path, producer=produce_related,
        validator=lambda value: validate_related_work_plan(ir, value, root),  # type: ignore[arg-type]
        status_resolver=lambda value: "passed" if value.get("review", {}).get("status") == "passed" else "needs_review",  # type: ignore[union-attr]
    )
    emit("related", "completed", cache_hit=related_cache_hit)
    assert isinstance(related, dict)

    page_model_path = paper_dir / "plans" / "page_model.json"
    emit("page", "running")
    page_model, page_cache_hit = runner.run(
        stage="page", stage_version="2.0.0", role="page-generation-lead", reviewed_by="page-generation-verifier",
        inputs={"ir": ir, "content": content, "visual": visuals, "related": related, "allow_unreviewed": options.allow_unreviewed},
        input_paths={"ir": ir_path, "content": content_path, "visual": visual_path, "related": related_path},
        output_path=page_model_path,
        producer=lambda task: build_page_model(ir, content, visuals, related, project_root=root, allow_unreviewed=options.allow_unreviewed),
        validator=lambda value: validate_page_model(value, root),  # type: ignore[arg-type]
        status_resolver=lambda value: "needs_review" if value.get("preview") else "passed",  # type: ignore[union-attr]
    )
    emit("page", "completed", cache_hit=page_cache_hit)
    assert isinstance(page_model, dict)
    html_path = output_path or root / "output" / f"{paper_id}-visualizer.html"
    emit("render", "running")
    html, render_cache_hit = runner.run(
        stage="render", stage_version="1.2.0", role="html-generation-lead", reviewed_by="html-generation-verifier",
        inputs={"page_model": page_model, "templates": _tree_digest((root / "templates",)), "embed_local_pdf": options.embed_local_pdf},
        input_paths={"page_model": page_model_path},
        output_path=html_path, kind="html", schema_version="html5",
        producer=lambda task: render_html(
            ir, content, visuals, related, project_root=root,
            embed_local_pdf=options.embed_local_pdf, allow_unreviewed=options.allow_unreviewed,
        ),
        validator=_validate_rendered_html,
    )
    emit("render", "completed", cache_hit=render_cache_hit)
    assert isinstance(html, str)

    browser_results = _load_overlay(paper_dir / "review" / "browser_results.json")
    reusable_sources = tuple(
        path for path in (root / "paper_visualizer", root / "templates") if path.exists()
    )
    review_path = paper_dir / "review" / "review_report.json"
    emit("review", "running")
    review, review_cache_hit = runner.run(
        stage="review", stage_version="1.1.1", role="independent-review-lead", reviewed_by="independent-review-verifier",
        inputs={"parsed": parsed, "overlay": overlay, "ir": ir, "content": content, "visual": visuals, "related": related, "page": page_model, "html": stable_digest(html), "browser": browser_results, "require_browser_review": options.require_browser_review, "runtime_sources": _tree_digest(reusable_sources)},
        input_paths={"parsed": parsed_path, "ir": ir_path, "content": content_path, "visual": visual_path, "related": related_path, "page": page_model_path, "html": html_path},
        output_path=review_path,
        producer=lambda task: review_artifacts(
            ir, page_model, html, parsed_document=parsed, human_review_overlay=overlay,
            content_plan=content, visual_plan=visuals, related_work=related,
            browser_results=browser_results, source_paths=reusable_sources,
            require_browser=options.require_browser_review, project_root=root,
        ),
        validator=lambda value: validate_review_report(value, root),  # type: ignore[arg-type]
        status_resolver=lambda value: "passed" if value.get("status") == "passed" else "needs_review",  # type: ignore[union-attr]
    )
    emit("review", "completed", cache_hit=review_cache_hit)
    assert isinstance(review, dict)

    manifest = {
        "schema_version": "1.0.0",
        "paper_id": paper_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_sha256": parsed["source"]["sha256"],
        "status": "review_failed" if review["status"] != "passed" else ("preview" if page_model.get("preview") else "built"),
        "artifacts": {
            "parsed": str(paper_dir / "parsed" / "parsed_document.json"),
            "ir": str(ir_path),
            "content_plan": str(content_path),
            "visual_plan": str(visual_path),
            "related_work": str(related_path),
            "page_model": str(page_model_path),
            "html": str(html_path),
            "review_report": str(review_path),
        },
        "hashes": {"html": sha256_file(html_path)},
        "related_work_cost": cost,
        "review": {"status": review["status"], **review["summary"]},
        "html_bytes": len(html.encode("utf-8")),
    }
    atomic_write_json(paper_dir / "manifest.json", manifest)
    emit("complete", "completed", paper_id=paper_id, manifest_status=manifest["status"])
    return manifest
