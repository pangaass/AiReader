from pathlib import Path

import pytest

from alignment_harness.preprocess import prepare_paper_context, split_paper_context


def test_prepare_paper_context_keeps_page_provenance(tmp_path: Path):
    fitz = pytest.importorskip("pymupdf")
    source = tmp_path / "paper.pdf"
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "Evidence on page one")
    document.save(source)
    destination = tmp_path / "context.json"

    prepare_paper_context(source, destination)

    payload = destination.read_text()
    assert "page_count: 1" in payload
    assert "## Page 1" in payload
    assert "Evidence on page one" in payload


def test_split_paper_context_preserves_every_page():
    context = "header\n" + "\n".join(
        f"## Page {number}\n" + (str(number) * 20) for number in range(1, 5)
    )
    units = split_paper_context(context, max_chars=60)
    combined = "\n".join(text for _, text in units)
    assert len(units) >= 2
    for number in range(1, 5):
        assert f"## Page {number}" in combined
