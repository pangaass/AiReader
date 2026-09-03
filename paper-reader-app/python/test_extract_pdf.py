import importlib.util
import sys
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("extract_pdf.py")
SPEC = importlib.util.spec_from_file_location("extract_pdf", MODULE_PATH)
extract_pdf = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = extract_pdf
SPEC.loader.exec_module(extract_pdf)


class ExtractorUnitTests(unittest.TestCase):
    def test_responses_endpoint_appends_resource_path(self):
        self.assertEqual(
            extract_pdf.responses_endpoint("https://gateway.example/v1"),
            "https://gateway.example/v1/responses",
        )
        self.assertEqual(
            extract_pdf.responses_endpoint("https://gateway.example/v1/responses"),
            "https://gateway.example/v1/responses",
        )

    def test_split_sentences_preserves_contributing_boxes(self):
        lines = [
            extract_pdf.Line("First sentence.", (10, 10, 100, 20), 10, "Times"),
            extract_pdf.Line("Second sentence continues", (10, 22, 140, 32), 10, "Times"),
            extract_pdf.Line("on another line.", (10, 34, 90, 44), 10, "Times"),
        ]
        result = extract_pdf.split_sentences(lines)
        self.assertEqual([item[0] for item in result], ["First sentence.", "Second sentence continues on another line."])
        self.assertEqual(result[1][1], (10, 22, 140, 44))

    def test_normalize_bbox_clamps_to_page(self):
        bbox = extract_pdf.normalize_bbox((-2, 5, 102, 55), 100, 50)
        self.assertEqual(bbox["x"], 0)
        self.assertEqual(bbox["y"], 0.07)
        self.assertEqual(bbox["width"], 1)
        self.assertEqual(bbox["height"], 0.93)

    def test_equation_heuristic(self):
        equation = extract_pdf.Line("Attention(Q,K,V) = softmax(QKᵀ / √dₖ)V", (120, 100, 470, 118), 10, "CMR10 CMMI10", 0.6)
        prose = extract_pdf.Line("We use dimension d = 512 in every layer.", (60, 100, 500, 118), 10, "Times-Roman CMMI10", 0.08)
        self.assertTrue(extract_pdf.line_is_equation(equation, 600))
        self.assertFalse(extract_pdf.line_is_equation(prose, 600))

    def test_merge_continuations_across_pdf_blocks(self):
        blocks = [
            {"id": "a", "type": "text", "text": "This sentence continues", "source_bbox": [10, 10, 190, 20], "bbox": {}},
            {"id": "b", "type": "text", "text": "on the following line.", "source_bbox": [35, 22, 170, 32], "bbox": {}},
        ]
        merged = extract_pdf.merge_text_continuations(blocks, 200, 300)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["text"], "This sentence continues on the following line.")
        self.assertEqual(merged[0]["source_bbox"], [10, 10, 190, 32])

    def test_two_columns_are_read_top_to_bottom(self):
        def block(block_id, x, y):
            return {"id": block_id, "type": "text", "source_bbox": [x, y, x + 180, y + 12]}
        blocks = [block("left-1", 40, 100), block("right-1", 320, 102), block("left-2", 40, 130), block("right-2", 320, 132)]
        ordered = extract_pdf.reading_order(blocks, 600)
        self.assertEqual([item["id"] for item in ordered], ["left-1", "left-2", "right-1", "right-2"])

    def test_arxiv_sidebar_is_margin_text(self):
        self.assertTrue(extract_pdf.is_margin_text((8, 180, 30, 600), "arXiv:1706.03762v7", 600, 800))

    def test_table_candidate_cues_use_caption_and_numeric_density(self):
        blocks = [
            {"id": "caption", "type": "text", "page": 3, "text": "Table 2: Main results", "source_bbox": [40, 80, 250, 95]},
            {"id": "row-1", "type": "text", "page": 3, "text": "Model 71.2 68.4", "source_bbox": [40, 110, 250, 125]},
            {"id": "row-2", "type": "text", "page": 3, "text": "Baseline 69.5 65.1", "source_bbox": [40, 130, 250, 145]},
        ]
        cues = extract_pdf.page_table_cues(blocks)
        self.assertIn("存在 Table/Tab. 表题", cues)
        self.assertEqual(extract_pdf.local_table_candidate_pages(blocks), {3})

    def test_visual_table_replacement_is_non_destructive(self):
        doc = extract_pdf.pymupdf.open()
        doc.new_page(width=600, height=800)
        blocks = [
            {
                "id": "block-00001", "type": "text", "page": 1, "text": "Table 1: Results",
                "source_bbox": [50, 80, 300, 98], "bbox": extract_pdf.normalize_bbox((50, 80, 300, 98), 600, 800),
            },
            {
                "id": "block-00002", "type": "text", "page": 1, "text": "Method Accuracy",
                "source_bbox": [50, 120, 300, 140], "bbox": extract_pdf.normalize_bbox((50, 120, 300, 140), 600, 800),
            },
            {
                "id": "block-00003", "type": "text", "page": 1, "text": "Nearby prose remains visible.",
                "source_bbox": [50, 320, 300, 340], "bbox": extract_pdf.normalize_bbox((50, 320, 300, 340), 600, 800),
            },
        ]
        detected = [{
            "page": 1,
            "caption": "Table 1: Results",
            "bbox": {"x": 0.075, "y": 0.14, "width": 0.6, "height": 0.2},
            "rows": [["Method", "Accuracy"], ["Ours", "91.2"]],
            "source_block_ids": ["block-00002"],
            "confidence": "high",
            "continues_from_previous": False,
            "continues_to_next": False,
        }]
        stats = extract_pdf.apply_visual_tables(doc, blocks, detected)
        table = next(block for block in blocks if block["type"] == "table")
        self.assertEqual(stats["tables_added"], 1)
        self.assertEqual(table["replaces_block_ids"], ["block-00002"])
        self.assertEqual(next(block for block in blocks if block["id"] == "block-00002")["replaced_by"], table["id"])
        self.assertNotIn("replaced_by", next(block for block in blocks if block["id"] == "block-00001"))
        self.assertNotIn("replaced_by", next(block for block in blocks if block["id"] == "block-00003"))

    def test_visual_table_rows_are_rectangular(self):
        rows = extract_pdf.normalized_table_rows([["A", "B", ""], ["1", "2", "3"]])
        self.assertEqual(rows, [["A", "B", ""], ["1", "2", "3"]])

    def test_discovery_sends_every_page_as_text_and_image(self):
        doc = extract_pdf.pymupdf.open()
        doc.new_page(width=300, height=400)
        doc.new_page(width=300, height=400)
        blocks = [
            {"id": "a", "type": "text", "page": 1, "text": "ordinary prose", "bbox": {"x": 0.1, "y": 0.1, "width": 0.8, "height": 0.05}},
            {"id": "b", "type": "text", "page": 2, "text": "Table 1: Results", "bbox": {"x": 0.1, "y": 0.1, "width": 0.8, "height": 0.05}},
        ]
        captured = {}
        original = extract_pdf.call_responses_api

        def fake_call(_key, _model, _system, content, _name, _schema):
            captured["content"] = content
            return {"pages": [
                {"page": 1, "has_table": False, "confidence": "high", "reason": "prose"},
                {"page": 2, "has_table": True, "confidence": "high", "reason": "table"},
            ]}

        extract_pdf.call_responses_api = fake_call
        try:
            pages, errors, calls = extract_pdf.discover_table_pages(doc, blocks, "key", "model")
        finally:
            extract_pdf.call_responses_api = original
        self.assertEqual(pages, {2})
        self.assertEqual(errors, [])
        self.assertEqual(calls, 1)
        images = [item for item in captured["content"] if item["type"] == "input_image"]
        self.assertEqual(len(images), 2)
        self.assertTrue(all(item["detail"] == "low" for item in images))

    def test_awm_composite_figure_and_borderless_tables_are_whole_blocks(self):
        pdf = MODULE_PATH.parents[2] / "artifacts" / "awm-generated" / "source.pdf"
        if not pdf.exists():
            self.skipTest("AWM fixture unavailable")
        doc = extract_pdf.pymupdf.open(pdf)
        blocks, _ = extract_pdf.page_blocks(doc[2], 3, [0])
        figures = [block for block in blocks if block["type"] == "figure"]
        tables = [block for block in blocks if block["type"] == "table"]
        self.assertEqual(len(figures), 1)
        self.assertTrue(figures[0]["label"].startswith("Figure 1:"))
        self.assertEqual(len(tables), 2)
        self.assertFalse(any(block["type"] == "text" and "final answer" in block.get("text", "") for block in blocks))

    def test_inline_math_stays_inside_attention_sentence(self):
        pdf = MODULE_PATH.parents[2] / "artifacts" / "attention-is-all-you-need" / "source.pdf"
        if not pdf.exists():
            self.skipTest("Attention fixture unavailable")
        doc = extract_pdf.pymupdf.open(pdf)
        blocks, _ = extract_pdf.page_blocks(doc[1], 2, [0])
        matching = [block for block in blocks if "continuous representations z =" in block.get("text", "")]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["type"], "text")
        self.assertFalse(any(block["type"] == "equation" for block in blocks))

    def test_prose_with_inline_formula_is_not_display_equation(self):
        pdf = MODULE_PATH.parents[2] / "artifacts" / "awm-generated" / "source.pdf"
        if not pdf.exists():
            self.skipTest("AWM fixture unavailable")
        doc = extract_pdf.pymupdf.open(pdf)
        blocks, _ = extract_pdf.page_blocks(doc[5], 6, [0])
        matching = [block for block in blocks if "Benchmarks" in block.get("text", "")]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["type"], "text")


if __name__ == "__main__":
    unittest.main()
