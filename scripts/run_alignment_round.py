#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from alignment_harness.llm import ClaudeRunner
from alignment_harness.pipeline import AlignmentPipeline, load_manifest
from alignment_harness.prompts import BASELINE_PAPER_INSTRUCTION

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, default=Path("artifacts/alignment_harness/latest"))
    parser.add_argument("--model", default="glm-5.3-flash")
    parser.add_argument("--timeout", type=int, default=360, help="per-agent timeout in seconds")
    parser.add_argument("--effort", choices=["low", "medium", "high"], default="low")
    parser.add_argument("--iterations", type=int, default=2)
    parser.add_argument("--pair-id", action="append", help="run only the selected pair (repeatable)")
    parser.add_argument("--resume", action="store_true", help="reuse completed aggregate extractions")
    parser.add_argument("--instruction-file", type=Path, help="seed paper-harness instruction")
    args = parser.parse_args()
    root = args.root.resolve()
    output = args.output if args.output.is_absolute() else root / args.output
    pairs = load_manifest(args.manifest.resolve())
    if args.pair_id:
        selected = set(args.pair_id)
        pairs = [pair for pair in pairs if pair["id"] in selected]
    instruction = (
        args.instruction_file.read_text(encoding="utf-8")
        if args.instruction_file
        else BASELINE_PAPER_INSTRUCTION
    )
    summary = AlignmentPipeline(
        root, output, ClaudeRunner(args.model, args.timeout, args.effort), resume=args.resume
    ).run(pairs, args.iterations, instruction)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
