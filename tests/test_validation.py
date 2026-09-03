from pathlib import Path

from paper_visualizer.validation import validate_ir


ROOT = Path(__file__).resolve().parents[1]


def test_schema_rejects_empty_ir():
    assert validate_ir({}, ROOT)

