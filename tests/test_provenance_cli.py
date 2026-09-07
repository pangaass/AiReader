from __future__ import annotations

import json
from pathlib import Path

from paper_visualizer.related.cli import main
from test_related_work import _ir


ROOT = Path(__file__).resolve().parents[1]


def test_cli_builds_source_to_current_graph(tmp_path: Path):
    ir_path = tmp_path / "paper_ir.json"
    output_path = tmp_path / "provenance_graph.json"
    ir_path.write_text(json.dumps(_ir()), encoding="utf-8")

    assert main([str(ir_path), "--output", str(output_path), "--project-root", str(ROOT)]) == 0

    result = json.loads(output_path.read_text(encoding="utf-8"))
    assert result["policy"]["graph_purpose"] == "evidence_grounded_research_provenance"
    assert all(edge["target_id"] == result["paper_id"] for edge in result["edges"])
