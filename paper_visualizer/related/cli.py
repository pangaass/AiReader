"""Build the evidence-grounded provenance graph from an existing Paper IR."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .planner import write_related_work_plan


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="paper-provenance-graph",
        description="从 Paper IR 构建指向当前论文的研究溯源图数据。",
    )
    parser.add_argument("paper_ir", type=Path, help="paper_ir.json 路径")
    parser.add_argument("--output", type=Path, required=True, help="输出的 provenance graph JSON 路径")
    parser.add_argument("--project-root", type=Path, default=Path.cwd(), help="Paper Visualizer 项目根目录")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    ir = json.loads(args.paper_ir.read_text(encoding="utf-8"))
    plan = write_related_work_plan(ir, args.output, project_root=args.project_root.resolve())
    print(f"已生成 {len(plan['nodes'])} 个来源节点：{args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
