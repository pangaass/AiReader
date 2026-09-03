from __future__ import annotations

import hashlib
import io
import json
import struct
import tempfile
import unittest
import urllib.error
import zlib
from pathlib import Path
from unittest import mock

from paper_visualizer.parsing import IngestError, ParseError, ParseOptions, ingest_pdf, parse_source
from paper_visualizer.parsing import ingest as ingest_module
from paper_visualizer.parsing import parser as parser_module


PDF_BYTES = b"%PDF-1.4\n% parser-contract-fixture\n%%EOF\n"


class _Response(io.BytesIO):
    def __init__(self, payload: bytes, *, declared_size: int | None = None) -> None:
        super().__init__(payload)
        self.headers = {"Content-Length": str(declared_size if declared_size is not None else len(payload))}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()


class _Opener:
    def __init__(self, response: _Response) -> None:
        self.response = response

    def open(self, request, timeout):
        return self.response


class _FailingOpener:
    def open(self, request, timeout):
        raise urllib.error.URLError("https://example.test/paper.pdf?token=TOPSECRET")


def _block(page: int, order: int, text: str, bbox: list[float], role: str = "body") -> dict[str, object]:
    return {
        "id": f"block:p{page:04d}:{order:04d}",
        "text": text,
        "bbox": bbox,
        "order": order,
        "role": role,
        "font_size_median": 10.0,
        "coordinate_precision": "exact",
    }


def _write_test_png(path: Path, width: int, height: int) -> None:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)

    # Alternating rows make the fixture visibly non-uniform without Pillow.
    rows = b"".join(b"\x00" + bytes([0 if row % 2 else 255, 255, 255]) * width for row in range(height))
    payload = b"\x89PNG\r\n\x1a\n"
    payload += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    payload += chunk(b"IDAT", zlib.compress(rows))
    payload += chunk(b"IEND", b"")
    path.write_bytes(payload)


def _write_uniform_png(path: Path, width: int, height: int) -> None:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)

    rows = b"".join(b"\x00" + b"\xff\xff\xff" * width for _ in range(height))
    payload = b"\x89PNG\r\n\x1a\n"
    payload += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    payload += chunk(b"IDAT", zlib.compress(rows))
    payload += chunk(b"IEND", b"")
    path.write_bytes(payload)


def _structured_result():
    pages = [
        {
            "number": 1,
            "width": 600.0,
            "height": 800.0,
            "blocks": [
                _block(1, 0, "1 Introduction", [40, 60, 200, 80], "heading"),
                _block(1, 1, "A grounded method is introduced.", [40, 100, 560, 140]),
                _block(1, 2, "Figure 1: General architecture", [40, 500, 560, 530], "caption"),
                _block(1, 3, "y = W x + b (1)", [120, 570, 480, 600], "formula"),
            ],
            "render_path": None,
        },
        {
            "number": 2,
            "width": 600.0,
            "height": 800.0,
            "blocks": [
                _block(2, 0, "References", [40, 60, 180, 80], "heading"),
                _block(2, 1, "[1] A. Author. A useful paper. 2024.", [40, 100, 560, 140], "reference"),
            ],
            "render_path": None,
        },
    ]
    images = [{
        "id": "image:p0001:000",
        "page": 1,
        "extension": "png",
        "bytes": b"not-a-real-png-but-extraction-is-byte-preserving",
        "width": 10,
        "height": 10,
        "source_bbox": [50, 200, 550, 480],
        "extraction_method": "test",
    }]
    return pages, images, []


class IngestTests(unittest.TestCase):
    def test_local_ingest_is_content_addressed_and_reused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "input.pdf"
            source.write_bytes(PDF_BYTES)
            first = ingest_pdf(source, artifact_dir=root / "a", cache_dir=root / "cache")
            second = ingest_pdf(source, artifact_dir=root / "b", cache_dir=root / "cache")
            self.assertEqual(first.sha256, hashlib.sha256(PDF_BYTES).hexdigest())
            self.assertFalse(first.cache_hit)
            self.assertTrue(second.cache_hit)
            self.assertEqual(second.local_pdf.read_bytes(), PDF_BYTES)

    def test_rejects_non_http_scheme_and_non_pdf(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bad = root / "bad.pdf"
            bad.write_text("not pdf", encoding="utf-8")
            with self.assertRaises(IngestError):
                ingest_pdf("ftp://example.test/paper.pdf", artifact_dir=root / "a", cache_dir=root / "cache")
            with self.assertRaises(IngestError):
                ingest_pdf(bad, artifact_dir=root / "a", cache_dir=root / "cache")

    def test_url_ingest_limits_size_and_redacts_secrets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with mock.patch.object(ingest_module.urllib.request, "build_opener", return_value=_Opener(_Response(PDF_BYTES))):
                result = ingest_pdf(
                    "https://user:pass@example.test/paper.pdf?token=secret&download=1#private",
                    artifact_dir=root / "a",
                    cache_dir=root / "cache",
                )
            self.assertEqual(result.original_url, "https://example.test/paper.pdf?token=REDACTED&download=1")
            with mock.patch.object(ingest_module.urllib.request, "build_opener", return_value=_Opener(_Response(PDF_BYTES, declared_size=999))):
                with self.assertRaises(IngestError):
                    ingest_pdf("https://example.test/large.pdf", artifact_dir=root / "b", cache_dir=root / "cache", max_download_mb=0)

    def test_download_error_traceback_does_not_expose_signed_url(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with mock.patch.object(ingest_module.urllib.request, "build_opener", return_value=_FailingOpener()):
                try:
                    signed_url = "https://example.test/paper.pdf?token=" + "TOPSECRET"
                    ingest_pdf(signed_url, artifact_dir=root / "a", cache_dir=root / "cache")
                except IngestError as exc:
                    rendered = str(exc)
                    self.assertIsNone(exc.__cause__)
                else:
                    self.fail("expected IngestError")
            self.assertNotIn("TOPSECRET", rendered)


class ParseTests(unittest.TestCase):
    def test_structured_parse_extracts_contract_fields_and_cache_rebases(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "input.pdf"
            source.write_bytes(PDF_BYTES)
            with mock.patch.object(parser_module, "_extract_with_pymupdf", return_value=_structured_result()), mock.patch.object(
                parser_module, "_render_pages", return_value=("test_renderer", [])
            ):
                first = parse_source(source, artifact_dir=root / "paper-a", cache_dir=root / "cache")
            self.assertEqual(first["status"], "passed")
            self.assertEqual(first["source"]["page_count"], 2)
            self.assertEqual(first["sections"][0]["page_start"], 1)
            self.assertEqual(first["figures"][0]["caption_block_ids"], ["block:p0001:0002"])
            self.assertEqual(first["figures"][0]["match_status"], "position_matched_embedded")
            self.assertEqual(first["formula_candidates"][0]["page"], 1)
            self.assertEqual(first["references"][0]["page"], 2)
            self.assertTrue(Path(first["image_assets"][0]["asset_path"]).is_file())
            second = parse_source(source, artifact_dir=root / "paper-b", cache_dir=root / "cache")
            self.assertTrue(second["cache_hit"])
            self.assertIn("paper-b", second["source"]["local_pdf"])
            self.assertIn("paper-b", second["image_assets"][0]["asset_path"])

    def test_layout_fallback_requires_review(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "input.pdf"
            source.write_bytes(PDF_BYTES)
            pages, _, _ = _structured_result()
            for page in pages:
                for block in page["blocks"]:
                    block["coordinate_precision"] = "estimated"
            with mock.patch.object(parser_module, "_extract_with_pymupdf", side_effect=ParseError("missing")), mock.patch.object(
                parser_module, "_extract_with_pypdf", return_value=(pages, [], ["fallback"])
            ):
                result = parse_source(
                    source,
                    artifact_dir=root / "paper",
                    cache_dir=root / "cache",
                    options=ParseOptions(render_pages=False, force=True),
                )
            self.assertEqual(result["parser"]["text_backend"], "pypdf_fallback")
            self.assertEqual(result["status"], "needs_review")
            self.assertTrue(any(item["kind"] == "coordinate_precision" for item in result["review_items"]))

    def test_vector_figure_gets_traceable_caption_crop_candidate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "input.pdf"
            source.write_bytes(PDF_BYTES)
            pages, _, _ = _structured_result()

            def fake_render(pdf_path, rendered_pages, output_dir, dpi):
                output_dir.mkdir(parents=True, exist_ok=True)
                for page in rendered_pages:
                    page["render_path"] = str(output_dir / f"page-{page['number']}.png")
                return "test_renderer", []

            def fake_crop(pdf_path, *, page_number, bbox, dpi, target, rendered_page_path):
                target.parent.mkdir(parents=True, exist_ok=True)
                _write_test_png(target, 290, 380)
                return [10, 20, 300, 400], "test_caption_crop"

            with mock.patch.object(parser_module, "_extract_with_pymupdf", return_value=(pages, [], [])), mock.patch.object(
                parser_module, "_render_pages", side_effect=fake_render
            ), mock.patch.object(parser_module, "_render_crop", side_effect=fake_crop):
                result = parse_source(source, artifact_dir=root / "paper", cache_dir=root / "cache", options=ParseOptions(force=True))
            figure = result["figures"][0]
            asset = next(item for item in result["image_assets"] if item["id"] == figure["asset_id"])
            self.assertEqual(figure["match_status"], "caption_crop_candidate")
            self.assertEqual(asset["source_page"], 1)
            self.assertEqual(asset["crop_bbox_pdf"], [27.0, 152.0, 573.0, 496.0])
            self.assertEqual(asset["extraction_method"], "test_caption_crop")
            self.assertEqual(asset["confidence"], "medium")
            self.assertEqual(result["status"], "needs_review")
            self.assertTrue(any(item["kind"] == "figure_crop_review" for item in result["review_items"]))

    def test_invalid_caption_crop_is_rejected_and_falls_back_to_source_page(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "input.pdf"
            source.write_bytes(PDF_BYTES)
            pages, _, _ = _structured_result()

            def fake_render(pdf_path, rendered_pages, output_dir, dpi):
                output_dir.mkdir(parents=True, exist_ok=True)
                for page in rendered_pages:
                    page["render_path"] = str(output_dir / f"page-{page['number']}.png")
                return "test_renderer", []

            def corrupt_crop(pdf_path, *, page_number, bbox, dpi, target, rendered_page_path):
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"not-a-valid-png")
                return [10, 20, 300, 400], "test_caption_crop"

            with mock.patch.object(parser_module, "_extract_with_pymupdf", return_value=(pages, [], [])), mock.patch.object(
                parser_module, "_render_pages", side_effect=fake_render
            ), mock.patch.object(parser_module, "_render_crop", side_effect=corrupt_crop):
                result = parse_source(source, artifact_dir=root / "paper", cache_dir=root / "cache", options=ParseOptions(force=True))

            figure = result["figures"][0]
            self.assertIsNone(figure.get("asset_path"))
            self.assertEqual(figure["match_status"], "caption_crop_rejected")
            rejected = next(item for item in result["review_items"] if item["kind"] == "figure_crop_rejected")
            self.assertEqual(rejected["fallback"], "caption_and_pdf_page_only")
            self.assertFalse(any(item["id"] == "crop:figure:001" for item in result["image_assets"]))

    def test_duplicate_figure_label_in_prose_is_not_a_second_caption(self):
        pages = [{
            "number": 1,
            "width": 600.0,
            "height": 800.0,
            "blocks": [
                _block(1, 0, "Figure 3: Main result", [320, 300, 540, 320], "caption"),
                _block(1, 1, "Figure 3. For each condition, we repeat the experiment.", [40, 500, 280, 530], "caption"),
            ],
            "render_path": None,
        }]

        figures, _ = parser_module._captions(pages)

        self.assertEqual(len(figures), 1)
        self.assertEqual(figures[0]["caption"], "Figure 3: Main result")
        self.assertEqual(pages[0]["blocks"][1]["role"], "body")

    def test_caption_continuation_block_is_merged_when_first_block_ends_in_hyphen(self):
        pages = [{
            "number": 1,
            "width": 600.0,
            "height": 800.0,
            "blocks": [
                _block(1, 0, "Table 6: Results on LONGDOCBENCH-", [40, 500, 280, 520], "caption"),
                _block(1, 1, "DOC in the controlled setting.", [40, 522, 240, 540], "body"),
                _block(1, 2, "The result improves the baseline.", [40, 570, 280, 600], "body"),
            ],
            "render_path": None,
        }]

        _, tables = parser_module._captions(pages)

        self.assertEqual(len(tables), 1)
        self.assertEqual(tables[0]["caption_block_ids"], ["block:p0001:0000", "block:p0001:0001"])
        self.assertIn("controlled setting.", tables[0]["caption"])
        self.assertEqual(pages[0]["blocks"][1]["role"], "caption")
        self.assertEqual(pages[0]["blocks"][2]["role"], "body")

    def test_caption_crop_ignores_axis_text_misclassified_as_heading(self):
        caption = _block(1, 2, "Figure 3: Main result", [320, 280, 540, 295], "caption")
        caption["coordinate_precision"] = "text_matrix"
        heading = _block(1, 1, "3.3 Experiment", [0, 225, 600, 235], "heading")
        heading["coordinate_precision"] = "exact"
        page = {"number": 1, "width": 600.0, "height": 800.0, "blocks": [heading, caption], "render_path": "/tmp/page.png"}
        figure = {"bbox": caption["bbox"], "caption_block_ids": [caption["id"]]}

        bbox, confidence = parser_module._caption_crop_bbox(figure, page, "figure")

        self.assertIsNotNone(bbox)
        self.assertEqual(confidence, "medium")
        self.assertLess(bbox[1], 100)

    def test_table_caption_crop_uses_adjacent_table_above_caption(self):
        header = _block(1, 0, "Method Score", [40, 300, 280, 315])
        rows = _block(1, 1, "Baseline 80 Proposed 90", [40, 320, 280, 355])
        caption = _block(1, 2, "Table 1: Main results", [40, 370, 280, 390], "caption")
        prose = _block(1, 3, "The proposed method improves the score.", [40, 420, 280, 455])
        page = {"number": 1, "width": 600.0, "height": 800.0, "blocks": [header, rows, caption, prose], "render_path": "/tmp/page.png"}
        table = {"bbox": caption["bbox"], "caption_block_ids": [caption["id"]]}

        bbox, confidence = parser_module._caption_crop_bbox(table, page, "table")

        self.assertEqual(confidence, "medium")
        self.assertLessEqual(bbox[1], header["bbox"][1])
        self.assertLess(bbox[3], caption["bbox"][1])
        self.assertGreater(bbox[3], rows["bbox"][3])

    def test_table_caption_crop_uses_adjacent_table_below_caption(self):
        caption = _block(1, 0, "Table 1: Main results", [40, 70, 540, 90], "caption")
        header = _block(1, 1, "Method Score", [40, 105, 540, 118])
        rows = _block(1, 2, "Baseline 80 Proposed 90", [40, 120, 540, 165])
        prose = _block(1, 3, "The proposed method improves the score.", [40, 220, 540, 255])
        page = {"number": 1, "width": 600.0, "height": 800.0, "blocks": [caption, header, rows, prose], "render_path": "/tmp/page.png"}
        table = {"bbox": caption["bbox"], "caption_block_ids": [caption["id"]]}

        bbox, confidence = parser_module._caption_crop_bbox(table, page, "table")

        self.assertEqual(confidence, "medium")
        self.assertGreater(bbox[1], caption["bbox"][3])
        self.assertLessEqual(bbox[1], header["bbox"][1])
        self.assertGreater(bbox[3], rows["bbox"][3])
        self.assertLess(bbox[3], prose["bbox"][1])

    def test_uniform_white_crop_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "blank.png"
            _write_uniform_png(target, 200, 40)
            item = {"bbox": [10.0, 70.0, 210.0, 90.0]}
            page = {"width": 300.0, "height": 400.0}

            error = parser_module._crop_validation_error(
                target, [10, 20, 210, 60], [10.0, 20.0, 210.0, 60.0], item, page, "table"
            )

            self.assertEqual(error, "table crop is visually blank or uniform")

    def test_nonblank_table_crop_below_caption_is_accepted(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "table.png"
            _write_test_png(target, 200, 40)
            item = {"bbox": [10.0, 70.0, 210.0, 90.0]}
            page = {"width": 300.0, "height": 400.0}

            error = parser_module._crop_validation_error(
                target, [10, 100, 210, 140], [10.0, 100.0, 210.0, 140.0], item, page, "table"
            )

            self.assertIsNone(error)

    def test_short_table_crop_is_not_rejected_by_figure_height_threshold(self):
        header = _block(1, 0, "Condition Rate", [40, 70, 280, 78])
        rows = _block(1, 1, "A 2.8 B 44.4", [40, 84, 280, 106])
        caption = _block(1, 2, "Table 8: Compact intervention", [40, 120, 280, 145], "caption")
        page = {"number": 1, "width": 600.0, "height": 800.0, "blocks": [header, rows, caption], "render_path": "/tmp/page.png"}

        bbox, _ = parser_module._caption_crop_bbox(
            {"bbox": caption["bbox"], "caption_block_ids": [caption["id"]]}, page, "table"
        )

        self.assertIsNotNone(bbox)
        self.assertLessEqual(bbox[1], 70)

    def test_cache_isolated_by_render_options(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "input.pdf"
            source.write_bytes(PDF_BYTES)
            with mock.patch.object(parser_module, "_extract_with_pymupdf", return_value=_structured_result()):
                first = parse_source(source, artifact_dir=root / "a", cache_dir=root / "cache", options=ParseOptions(render_pages=False, extract_images=False))
            with mock.patch.object(parser_module, "_extract_with_pymupdf", return_value=_structured_result()), mock.patch.object(
                parser_module, "_render_pages", return_value=("second_renderer", [])
            ):
                second = parse_source(source, artifact_dir=root / "b", cache_dir=root / "cache")
            self.assertFalse(first["cache_hit"])
            self.assertFalse(second["cache_hit"])
            self.assertEqual(second["parser"]["render_backend"], "second_renderer")

    def test_empty_text_is_blocking_and_does_not_write_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "input.pdf"
            source.write_bytes(PDF_BYTES)
            pages = [{"number": 1, "width": 100.0, "height": 100.0, "blocks": [], "render_path": None}]
            with mock.patch.object(parser_module, "_extract_with_pymupdf", return_value=(pages, [], [])):
                with self.assertRaises(ParseError):
                    parse_source(source, artifact_dir=root / "paper", cache_dir=root / "cache", options=ParseOptions(force=True))
            self.assertFalse((root / "paper" / "parsed" / "parsed_document.json").exists())

    def test_bbox_is_finite_bounded_and_ordered(self):
        self.assertEqual(parser_module._bounded_bbox([700, 900, -2, -3], 600, 800), [0.0, 0.0, 600, 800])

    def test_formula_candidates_merge_numbered_lines_and_filter_prose_fragments(self):
        page = {
            "number": 1,
            "width": 600.0,
            "height": 800.0,
            "blocks": [
                _block(1, 0, "We compute the matrix of outputs as:", [40, 40, 560, 60]),
                _block(1, 1, "Attention(Q,K,V) = softmax(QK T", [80, 80, 520, 100]),
                _block(1, 2, "√dk", [240, 105, 360, 125]),
                _block(1, 3, ")V (1)", [240, 130, 360, 150]),
                _block(1, 4, "We used an optimizer with β1 = 0.9 and β2 = 0.98.", [40, 180, 560, 200], "formula"),
                _block(1, 5, "The dimensionality is dmodel = 512.", [40, 220, 560, 240], "formula"),
                _block(1, 6, "the default setting γ=−0.1 is close to zero", [40, 260, 560, 280], "formula"),
            ],
            "render_path": None,
        }

        formulas = parser_module._formula_candidates([page])

        self.assertEqual(len(formulas), 1)
        self.assertEqual(
            formulas[0]["block_ids"],
            ["block:p0001:0001", "block:p0001:0002", "block:p0001:0003"],
        )
        self.assertEqual(formulas[0]["label"], "1")
        self.assertTrue(formulas[0]["text"].startswith("Attention(Q,K,V)"))
        self.assertEqual(page["blocks"][4]["role"], "body")
        self.assertEqual(page["blocks"][5]["role"], "body")

    def test_formula_candidates_trim_appended_prose_and_keep_real_comparisons(self):
        page = {
            "number": 1,
            "width": 600.0,
            "height": 800.0,
            "blocks": [
                _block(1, 0, "dk = dv = dmodel/h = 64. Due to reduced dimensions, computation is cheaper.", [40, 40, 560, 60], "formula"),
                _block(1, 1, "Memory refinement A11−A10 = β/(σg+ϵ) (1,1)≻(1,0)", [40, 80, 560, 100]),
                _block(1, 2, "withϵ=10−6.", [40, 120, 560, 140], "formula"),
            ],
            "render_path": None,
        }

        formulas = parser_module._formula_candidates([page])

        self.assertEqual([item["text"] for item in formulas], [
            "dk = dv = dmodel/h = 64",
            "A11−A10 = β/(σg+ϵ) (1,1)≻(1,0)",
        ])

    def test_formula_candidate_locates_equation_line_inside_prose_block(self):
        text = "The model uses eight heads.\n  dk = dv = dmodel/h = 64\nThis keeps each head compact."
        page = {
            "number": 1,
            "width": 600.0,
            "height": 800.0,
            "blocks": [_block(1, 0, text, [40, 40, 560, 100])],
            "render_path": None,
        }

        formulas = parser_module._formula_candidates([page])

        self.assertEqual(len(formulas), 1)
        self.assertEqual(formulas[0]["text"], "dk = dv = dmodel/h = 64")
        start, end = formulas[0]["char_start"], formulas[0]["char_end"]
        self.assertEqual(text.strip()[start:end], formulas[0]["text"])
        self.assertEqual(page["blocks"][0]["role"], "body")

    def test_formula_candidates_split_multiple_labeled_equations_in_one_block(self):
        text = (
            "Memory refinement\nA11−A10 = β/(σg+ϵ)\n(1, 1) ≻(1, 0)\n"
            "Final-answer gate\nA10−A01 = −γ/(σg+ϵ)\n(1, 0) ≻(0, 1)"
        )
        page = {
            "number": 1,
            "width": 600.0,
            "height": 800.0,
            "blocks": [_block(1, 0, text, [40, 40, 560, 100])],
            "render_path": None,
        }

        formulas = parser_module._formula_candidates([page])

        self.assertEqual(len(formulas), 2)
        self.assertEqual(formulas[0]["text"], "A11−A10 = β/(σg+ϵ) (1, 1) ≻(1, 0)")
        self.assertEqual(formulas[1]["text"], "A10−A01 = −γ/(σg+ϵ) (1, 0) ≻(0, 1)")
        for formula in formulas:
            source = text.strip()[formula["char_start"]:formula["char_end"]]
            self.assertEqual(" ".join(source.split()), formula["text"])

    def test_two_column_reading_order_finishes_left_column_before_right(self):
        blocks = [
            {"text": "right top", "bbox": [310.0, 40.0, 560.0, 60.0]},
            {"text": "left bottom", "bbox": [40.0, 100.0, 290.0, 120.0]},
            {"text": "left top", "bbox": [40.0, 40.0, 290.0, 60.0]},
            {"text": "right bottom", "bbox": [310.0, 100.0, 560.0, 120.0]},
        ]

        ordered = parser_module._layout_reading_order(blocks, 600.0)

        self.assertEqual([item["text"] for item in ordered], ["left top", "left bottom", "right top", "right bottom"])

    def test_unnumbered_hanging_indent_references_and_year_are_not_split(self):
        raw_blocks = [
            _block(1, 0, "References", [40, 20, 150, 35], "heading"),
            _block(1, 1, "Alice Author and Bob Writer.", [40, 40, 290, 55]),
            _block(1, 2, "2023. A complete first paper.", [50, 56, 290, 75]),
            _block(1, 3, "Carol Researcher.", [40, 80, 290, 95]),
            _block(1, 4, "2024. A second paper.", [50, 96, 290, 115]),
            _block(1, 5, "Dan Scientist.", [310, 40, 560, 55]),
            _block(1, 6, "2025. A right-column paper.", [320, 56, 560, 75]),
        ]
        ordered = parser_module._layout_reading_order(raw_blocks, 600.0)
        for order, block in enumerate(ordered):
            block["order"] = order
            block["id"] = f"block:p0001:{order:04d}"
        pages = [{"number": 1, "width": 600.0, "height": 800.0, "blocks": ordered, "render_path": None}]

        references = parser_module._references(pages)

        self.assertEqual(len(references), 3)
        self.assertIn("2023. A complete first paper.", references[0]["raw_reference"])
        self.assertTrue(references[1]["raw_reference"].startswith("Carol Researcher"))
        self.assertTrue(references[2]["raw_reference"].startswith("Dan Scientist"))

    def test_numbered_references_do_not_consume_later_visual_appendix(self):
        pages = [
            {"number": 1, "width": 100.0, "height": 100.0, "blocks": [
                _block(1, 0, "References", [0, 0, 100, 10], "heading"),
                _block(1, 1, "[1] A. Author. Work. 2020.", [0, 10, 100, 20], "reference"),
            ]},
            {"number": 2, "width": 100.0, "height": 100.0, "blocks": [
                _block(2, 0, "Attention Visualizations", [0, 0, 100, 10], "body"),
                _block(2, 1, "not a reference", [0, 10, 100, 20], "body"),
            ]},
        ]
        references = parser_module._references(pages)
        self.assertEqual(len(references), 1)
        self.assertEqual(references[0]["block_ids"], ["block:p0001:0001"])

    def test_reference_marker_inside_misclassified_heading_does_not_end_bibliography(self):
        pages = [{"number": 1, "width": 600.0, "height": 800.0, "blocks": [
            _block(1, 0, "References", [40, 20, 150, 35], "heading"),
            _block(1, 1, "[1] A. Author. First Work. 2020.", [40, 40, 290, 60], "reference"),
            _block(1, 2, "18 pages. [2] B. Author. Second Work. 2021.", [40, 65, 290, 85], "heading"),
            _block(1, 3, "[3] C. Author. Third Work. 2022.", [40, 90, 290, 110], "body"),
        ]}]

        references = parser_module._references(pages)

        self.assertEqual(len(references), 3)
        self.assertIn("[2]", references[1]["raw_reference"])
        self.assertIn("[3]", references[2]["raw_reference"])


if __name__ == "__main__":
    unittest.main()
