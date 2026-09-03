"""Render a Page Model as a safe, self-contained HTML document."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any, Mapping

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from .page_model import PageModelError, build_page_model, validate_page_model


class RenderError(ValueError):
    """Raised when a single-file page cannot be rendered safely."""


def _json_for_script(value: object) -> str:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
        .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    )


def _embedded_pdf(ir: Mapping[str, Any]) -> str | None:
    source = ir.get("source", {})
    if source.get("original_url"):
        return None
    raw = str(source.get("local_pdf") or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_file() or path.suffix.casefold() != ".pdf" or path.stat().st_size > 100 * 1024 * 1024:
        return None
    data = path.read_bytes()
    if not data.startswith(b"%PDF-"):
        return None
    return base64.b64encode(data).decode("ascii")


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def render_html(
    ir: Mapping[str, Any], content_plan: Mapping[str, Any], visual_plan: Mapping[str, Any],
    related_work: Mapping[str, Any] | None = None, *, output_path: Path | str | None = None,
    project_root: Path | None = None, embed_local_pdf: bool = True,
    allow_unreviewed: bool = False,
) -> str:
    """Return self-contained HTML and optionally atomically write it to disk."""

    root = project_root or Path(__file__).resolve().parents[2]
    model = build_page_model(ir, content_plan, visual_plan, related_work, project_root=root, allow_unreviewed=allow_unreviewed)
    errors = validate_page_model(model, root)
    if errors:
        raise RenderError("invalid page model: " + "; ".join(errors[:8]))
    template_root = root / "templates"
    environment = Environment(
        loader=FileSystemLoader(str(template_root)),
        autoescape=select_autoescape(enabled_extensions=("html", "j2"), default_for_string=True),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = environment.get_template("paper_visualizer.html.j2")
    html = template.render(
        model=model, evidence_json=_json_for_script(model["evidence"]),
        embedded_pdf_base64=_embedded_pdf(ir) if embed_local_pdf else None,
        generator_version=STAGE_VERSION,
    )
    if output_path is not None:
        _atomic_write_text(Path(output_path), html)
    return html


STAGE_VERSION = "1.3.0"


def render_files(
    ir_path: Path | str, content_plan_path: Path | str, visual_plan_path: Path | str,
    *, output_path: Path | str, related_work_path: Path | str | None = None,
    project_root: Path | None = None, embed_local_pdf: bool = True,
    allow_unreviewed: bool = False,
) -> str:
    """File-oriented entry point used by orchestration and command-line layers."""

    try:
        ir = json.loads(Path(ir_path).read_text(encoding="utf-8"))
        content = json.loads(Path(content_plan_path).read_text(encoding="utf-8"))
        visuals = json.loads(Path(visual_plan_path).read_text(encoding="utf-8"))
        related = json.loads(Path(related_work_path).read_text(encoding="utf-8")) if related_work_path else None
    except (OSError, json.JSONDecodeError) as exc:
        raise RenderError(f"cannot read rendering input: {exc}") from exc
    try:
        return render_html(ir, content, visuals, related, output_path=output_path, project_root=project_root, embed_local_pdf=embed_local_pdf, allow_unreviewed=allow_unreviewed)
    except PageModelError as exc:
        raise RenderError(str(exc)) from exc
