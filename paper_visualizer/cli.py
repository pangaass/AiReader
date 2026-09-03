"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from paper_visualizer.orchestration import PipelineOptions, run_pipeline


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="paper-visualizer", description="Generate an evidence-grounded single-file paper visualizer.")
    parser.add_argument("source", help="Local PDF path or public HTTP(S) PDF URL")
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--paper-id", help="Stable artifact/output slug")
    parser.add_argument("--output", type=Path, help="Output HTML path")
    parser.add_argument("--force", action="store_true", help="Ignore parse cache")
    parser.add_argument("--verify-related-work", action="store_true", help="Verify citation metadata with AMiner using AMINER_API_KEY")
    parser.add_argument("--llm", action="store_true", help="Use the configured model to write the narrative Visualizer content and metadata")
    review_mode = parser.add_mutually_exclusive_group()
    review_mode.add_argument("--allow-unreviewed", dest="allow_unreviewed", action="store_true", help="Generate a visibly marked preview from pending review artifacts (default)")
    review_mode.add_argument("--require-reviewed", dest="allow_unreviewed", action="store_false", help="Stop unless all required human review overlays are present")
    parser.set_defaults(allow_unreviewed=True)
    parser.add_argument("--no-embed-pdf", action="store_true", help="Do not embed a local source PDF")
    parser.add_argument("--no-render-pages", action="store_true")
    parser.add_argument("--require-browser-review", action="store_true", help="Treat missing browser review results as blockers")
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--progress-json", action="store_true", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    def progress(event: dict[str, object]) -> None:
        print("PAPER_VISUALIZER_PROGRESS " + json.dumps(event, ensure_ascii=False, separators=(",", ":")), file=sys.stderr, flush=True)

    manifest = run_pipeline(
        args.source,
        project_root=args.project_root,
        output_path=args.output,
        options=PipelineOptions(
            paper_id=args.paper_id,
            force=args.force,
            render_pages=not args.no_render_pages,
            max_attempts=max(1, args.max_attempts),
            verify_related_work=args.verify_related_work,
            allow_unreviewed=args.allow_unreviewed,
            embed_local_pdf=not args.no_embed_pdf,
            require_browser_review=args.require_browser_review,
            use_llm=args.llm,
        ),
        progress=progress if args.progress_json else None,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
