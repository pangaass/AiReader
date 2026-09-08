from __future__ import annotations

import argparse
import json
from pathlib import Path

from .download import download_manifest
from .llm import ClaudeRunner
from .pipeline import AlignmentPipeline, load_manifest
from .prompts import BASELINE_PAPER_INSTRUCTION


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Paper/presentation alignment research harness")
    subparsers = result.add_subparsers(dest="command", required=True)
    download = subparsers.add_parser("download", help="download public pair files")
    download.add_argument("manifest", type=Path)
    download.add_argument("--root", type=Path, default=Path.cwd())
    run = subparsers.add_parser("run", help="run extraction, alignment, and meta-optimization")
    run.add_argument("manifest", type=Path)
    run.add_argument("--root", type=Path, default=Path.cwd())
    run.add_argument("--output", type=Path, default=Path("artifacts/alignment_harness/latest"))
    run.add_argument("--model", default="glm-5.3-flash")
    run.add_argument("--timeout", type=int, default=360)
    run.add_argument("--effort", choices=["low", "medium", "high"], default="low")
    run.add_argument("--iterations", type=int, default=2)
    run.add_argument("--pair-id", action="append")
    run.add_argument("--resume", action="store_true")
    run.add_argument("--instruction-file", type=Path)
    return result


def main() -> int:
    args = parser().parse_args()
    root = args.root.resolve()
    if args.command == "download":
        receipts = download_manifest(args.manifest.resolve(), root)
        print(json.dumps({"downloaded": len(receipts)}, indent=2))
        return 0
    pairs = load_manifest(args.manifest.resolve())
    if args.pair_id:
        selected = set(args.pair_id)
        pairs = [pair for pair in pairs if pair["id"] in selected]
    output = args.output if args.output.is_absolute() else root / args.output
    instruction = (
        args.instruction_file.read_text(encoding="utf-8")
        if args.instruction_file
        else BASELINE_PAPER_INSTRUCTION
    )
    summary = AlignmentPipeline(
        root,
        output,
        ClaudeRunner(args.model, args.timeout, args.effort),
        resume=args.resume,
    ).run(
        pairs, args.iterations, instruction
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
