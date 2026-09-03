"""Schema and cross-reference quality gates for Paper IR."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


def load_schema(project_root: Path) -> dict[str, Any]:
    return json.loads((project_root / "schemas" / "paper-ir.schema.json").read_text(encoding="utf-8"))


def validate_schema(ir: dict[str, Any], project_root: Path) -> list[str]:
    validator = Draft202012Validator(load_schema(project_root))
    return [f"{'/'.join(str(p) for p in error.path)}: {error.message}" for error in sorted(validator.iter_errors(ir), key=lambda e: list(e.path))]


def validate_semantics(ir: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    entities: dict[str, dict[str, Any]] = {}
    for group in ("sections", "evidence", "claims", "formulas", "variables", "visuals", "tables", "citations", "relations"):
        for entity in ir.get(group, []):
            entity_id = entity.get("id")
            if entity_id in entities:
                errors.append(f"duplicate id: {entity_id}")
            elif entity_id:
                entities[entity_id] = entity

    page_blocks = {
        block["id"]: block["text"]
        for page in ir.get("pages", [])
        for block in page.get("blocks", [])
        if "id" in block and "text" in block
    }
    for item in ir.get("evidence", []):
        locator = item.get("locator", {})
        block_ids = locator.get("block_ids", [])
        if any(block_id not in page_blocks for block_id in block_ids):
            errors.append(f"{item.get('id')}: unknown evidence block")
            continue
        joined = " ".join(page_blocks[block_id].strip() for block_id in block_ids).strip()
        start = locator.get("char_start", 0)
        end = locator.get("char_end", len(joined))
        expected = joined[start:end]
        actual = item.get("verbatim_text", "")
        if expected != actual:
            errors.append(f"{item.get('id')}: verbatim text cannot be reconstructed")
        digest = hashlib.sha256(actual.encode("utf-8")).hexdigest()
        if digest != item.get("text_sha256"):
            errors.append(f"{item.get('id')}: text hash mismatch")
        if item.get("provenance", {}).get("kind") != "paper_verbatim":
            errors.append(f"{item.get('id')}: evidence provenance is not paper_verbatim")

    for claim in ir.get("claims", []):
        if not claim.get("evidence_ids"):
            errors.append(f"{claim.get('id')}: claim has no evidence")
        for evidence_id in claim.get("evidence_ids", []):
            if evidence_id not in entities:
                errors.append(f"{claim.get('id')}: unknown evidence {evidence_id}")
        provenance_kind = claim.get("provenance", {}).get("kind")
        expected_label = {"paper_excerpt": "原文摘录", "generated_summary": "生成式总结"}.get(provenance_kind)
        if expected_label is None or claim.get("display_label") != expected_label:
            errors.append(f"{claim.get('id')}: claim label does not match provenance")
        compact_claim = re.sub(r"[^\w]+", "", re.sub(r"\[[0-9,\s-]+\]", "", str(claim.get("summary") or "").casefold()))
        linked_evidence = [entities.get(item, {}) for item in claim.get("evidence_ids", [])]
        compact_evidence = [
            re.sub(r"[^\w]+", "", re.sub(r"\[[0-9,\s-]+\]", "", str(item.get("verbatim_text") or "").casefold()))
            for item in linked_evidence
        ]
        if provenance_kind == "paper_excerpt" and len(compact_evidence) == 1:
            if not compact_claim or compact_claim not in compact_evidence[0]:
                errors.append(f"{claim.get('id')}: paper excerpt differs from source evidence")
        elif provenance_kind == "generated_summary" and compact_claim in compact_evidence:
            errors.append(f"{claim.get('id')}: generated summary duplicates source evidence")

    for table in ir.get("tables", []):
        for cell in table.get("discussed_cells", []):
            if not cell.get("evidence_ids"):
                errors.append(f"{table.get('id')}: interactive cell lacks evidence")

    for visual in ir.get("visuals", []):
        if visual.get("kind") == "derived" and not visual.get("derived_from"):
            errors.append(f"{visual.get('id')}: derived visual lacks sources")
        for target in visual.get("interactive_targets", []):
            if target.get("interactive") and not target.get("evidence_ids"):
                errors.append(f"{target.get('id')}: interactive target lacks evidence")

    return errors


def validate_ir(ir: dict[str, Any], project_root: Path) -> list[str]:
    return validate_schema(ir, project_root) + validate_semantics(ir)
