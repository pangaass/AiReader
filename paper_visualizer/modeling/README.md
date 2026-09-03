# Knowledge modeling

`build_paper_ir(parsed_document, overlay=None)` converts the parser contract to
the strict `schemas/paper-ir.schema.json` shape. `model_parsed_file(...)` is the
file entry point and writes `paper_ir.json` atomically.

Default modeling is deterministic and intentionally conservative:

- Evidence is copied from one source block and stores page, block IDs,
  character range, bounding box, PDF page link, and SHA-256.
- Deterministic claims are typography-normalized source excerpts labelled
  `原文摘录` with `provenance.kind=paper_excerpt`; they always link back to the
  continuous Evidence span. Only a real human-authored rewrite is labelled
  `生成式总结` with `provenance.kind=generated_summary`.
- Formula variables support compact PDF forms such as `dk`, `W1`, `β1`, and
  explicit forms such as `d_k` or `warmup_steps`. Subscripts are restored and
  the inventory is deduplicated by canonical symbol. Variables are exposed only when
  nearby source Evidence explicitly defines or assigns the symbol; unresolved
  symbols are retained as review items instead of fabricated definitions.
- Table cells are not marked as discussed from value matching alone.
- Bibliographic metadata remains `unverified` until Related Work enrichment.

Human overlay supports `paper`, `evidence`, `claims`, `formulas`, `variables`,
`visuals`, `tables`, `citations`, `relations`, and `review`. List entries use an
ID-based upsert. Evidence text and hashes cannot be authored by the overlay:
they are always reconstructed from its locator. A non-passed parse additionally
requires an `approval` object containing `status: approved`, the exact
`base_source_sha256`, a reviewer, and a reason.
