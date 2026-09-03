"""Build renderer-neutral visual layouts from Paper IR and a content plan.

The planner never copies paper prose into the visual artifact. Labels are
references to Paper IR fields so the page generator has one canonical source.
All coordinates use a normalized SVG viewBox and can be scaled responsively.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator

from ..artifacts import atomic_write_json
from ..planning import validate_content_plan
from ..validation import validate_ir


SCHEMA_VERSION = "1.0.0"
STAGE_VERSION = "1.0.1"
VIEW_BOX = [0, 0, 1000, 600]


class VisualPlanningError(ValueError):
    """Raised when a visual plan would violate provenance or interaction rules."""


def _digest(value: Mapping[str, Any]) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _indexes(ir: Mapping[str, Any]) -> tuple[dict[str, Mapping[str, Any]], dict[str, str]]:
    entities: dict[str, Mapping[str, Any]] = {}
    types: dict[str, str] = {}
    for group in ("sections", "evidence", "claims", "formulas", "variables", "visuals", "tables", "citations", "relations"):
        for item in ir.get(group, []):
            entities[str(item["id"])] = item
            types[str(item["id"])] = group[:-1] if group.endswith("s") else group
    return entities, types


def _body_evidence_ids(ir: Mapping[str, Any]) -> set[str]:
    roles = {block["id"]: block.get("role", "body") for page in ir.get("pages", []) for block in page.get("blocks", [])}
    return {
        item["id"]
        for item in ir.get("evidence", [])
        if item.get("locator", {}).get("block_ids")
        and all(roles.get(block_id) == "body" for block_id in item["locator"]["block_ids"])
    }


def _label_ref(source_id: str, field: str, *, row: int | None = None, column: int | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"source_id": source_id, "field": field}
    if row is not None:
        result["row"] = row
    if column is not None:
        result["column"] = column
    return result


def _target(target_id: str, source_id: str, evidence_ids: list[str], label_ref: Mapping[str, Any], body_ids: set[str]) -> dict[str, Any]:
    grounded = [item for item in dict.fromkeys(evidence_ids) if item in body_ids]
    return {
        "id": target_id,
        "source_id": source_id,
        "evidence_ids": grounded,
        "interactive": bool(grounded),
        "label_ref": dict(label_ref),
    }


def _empty_layout(engine: str, direction: str = "none") -> dict[str, Any]:
    return {"engine": engine, "view_box": VIEW_BOX, "direction": direction, "nodes": [], "edges": [], "marks": []}


def _has_original_figure_asset(source: Mapping[str, Any]) -> bool:
    return bool(source.get("asset_path"))


def _has_original_table_source(source: Mapping[str, Any]) -> bool:
    has_asset = bool(source.get("asset_path"))
    rows = source.get("rows")
    columns = source.get("columns")
    has_structured_data = isinstance(rows, list) and bool(rows) and isinstance(columns, list) and bool(columns)
    return has_asset or has_structured_data


def _original_figure(request: Mapping[str, Any], source: Mapping[str, Any], body_ids: set[str], sequence: int) -> dict[str, Any]:
    targets = []
    for index, raw in enumerate(source.get("interactive_targets", []), 1):
        targets.append(_target(f"visual-target:{sequence:04d}:{index:04d}", source["id"], list(raw.get("evidence_ids", [])), _label_ref(source["id"], "title"), body_ids))
    return {
        "id": f"visual-plan:{sequence:04d}",
        "request_id": request["id"],
        "section_id": request["section_id"],
        "kind": "paper_original",
        "visual_type": "figure",
        "placement_role": "primary",
        "source_entity_ids": [source["id"]],
        "derived_from": [],
        "caption_policy": "show_official_once",
        "asset_entity_id": source["id"],
        "layout": _empty_layout("source_asset"),
        "targets": targets,
    }


def _original_table(request: Mapping[str, Any], table: Mapping[str, Any], body_ids: set[str], sequence: int) -> dict[str, Any]:
    targets = []
    for index, cell in enumerate(table.get("discussed_cells", []), 1):
        row, column = int(cell["row"]), int(cell["column"])
        targets.append(_target(f"visual-target:{sequence:04d}:{index:04d}", table["id"], list(cell.get("evidence_ids", [])), _label_ref(table["id"], "cell_value", row=row, column=column), body_ids))
    return {
        "id": f"visual-plan:{sequence:04d}",
        "request_id": request["id"],
        "section_id": request["section_id"],
        "kind": "paper_original",
        "visual_type": "table",
        "placement_role": "primary",
        "source_entity_ids": [table["id"]],
        "derived_from": [],
        "caption_policy": "show_official_once",
        "asset_entity_id": table["id"],
        "layout": _empty_layout("table_grid"),
        "targets": targets,
    }


def _comparison(request: Mapping[str, Any], table: Mapping[str, Any], body_ids: set[str], sequence: int) -> dict[str, Any] | None:
    rows = table.get("rows", [])
    columns = table.get("columns", [])
    numeric_column = next((index for index in range(len(columns)) if sum(isinstance(row[index], (int, float)) and not isinstance(row[index], bool) for row in rows if index < len(row)) >= 2), None)
    if numeric_column is None:
        return None
    values = [(index, float(row[numeric_column])) for index, row in enumerate(rows) if numeric_column < len(row) and isinstance(row[numeric_column], (int, float)) and not isinstance(row[numeric_column], bool)]
    if len(values) < 2:
        return None
    values = values[:12]
    low, high = min(value for _, value in values), max(value for _, value in values)
    span = high - min(0.0, low)
    span = span if span > 0 else 1.0
    discussed = {(int(cell["row"]), int(cell["column"])): list(cell.get("evidence_ids", [])) for cell in table.get("discussed_cells", [])}
    marks, targets = [], []
    step = 820 / len(values)
    for ordinal, (row_index, value) in enumerate(values):
        height = 400 * abs(value - min(0.0, low)) / span
        target = _target(f"visual-target:{sequence:04d}:{ordinal + 1:04d}", table["id"], discussed.get((row_index, numeric_column), []), _label_ref(table["id"], "cell_value", row=row_index, column=numeric_column), body_ids)
        targets.append(target)
        marks.append({
            "id": f"visual-mark:{sequence:04d}:{ordinal + 1:04d}", "source_id": table["id"], "mark": "bar",
            "position": {"x": 100 + ordinal * step, "y": 500 - height}, "size": {"width": max(20, step * 0.65), "height": height},
            "value": value, "label_ref": _label_ref(table["id"], "cell_value", row=row_index, column=0),
            "target_id": target["id"] if target["interactive"] else None,
        })
    layout = _empty_layout("bar_chart")
    layout["marks"] = marks
    source_ids = [table["id"], *dict.fromkeys(item for ids in discussed.values() for item in ids)]
    return {
        "id": f"visual-plan:{sequence:04d}", "request_id": request["id"], "section_id": request["section_id"],
        "kind": "derived", "visual_type": "comparison", "placement_role": "supplementary",
        "source_entity_ids": [table["id"]], "derived_from": source_ids, "caption_policy": "label_derived_only",
        "asset_entity_id": None,
        "description": {"display_label": "派生图", "text": "表格数值的比较视图；只有正文明确讨论的数据点可打开 Evidence。", "source_ids": source_ids},
        "layout": layout, "targets": targets,
    }


def _relation(request: Mapping[str, Any], entities: Mapping[str, Mapping[str, Any]], types: Mapping[str, str], body_ids: set[str], sequence: int) -> dict[str, Any] | None:
    claim_ids = [item for item in request.get("derived_from", []) if types.get(item) == "claim"]
    if not claim_ids:
        claim_ids = [item for item in request.get("candidate_entity_ids", []) if types.get(item) == "claim"]
    visual_sources = [entities[item] for item in request.get("candidate_entity_ids", []) if types.get(item) == "visual" and entities[item].get("kind") == "derived"]
    if not claim_ids:
        claim_ids = list(dict.fromkeys(source_id for visual in visual_sources for source_id in visual.get("derived_from", []) if types.get(source_id) == "claim"))
    pairs = [(claim_id, evidence_id) for claim_id in claim_ids for evidence_id in entities[claim_id].get("evidence_ids", []) if evidence_id in entities]
    if not pairs:
        return None
    evidence_ids = list(dict.fromkeys(evidence_id for _, evidence_id in pairs))
    nodes, targets = [], []
    for index, claim_id in enumerate(claim_ids):
        y = 90 + (index + 1) * 420 / (len(claim_ids) + 1)
        target = _target(f"visual-target:{sequence:04d}:claim:{index + 1:04d}", claim_id, list(entities[claim_id].get("evidence_ids", [])), _label_ref(claim_id, "summary"), body_ids)
        targets.append(target)
        nodes.append({"id": f"visual-node:{sequence:04d}:claim:{index + 1:04d}", "source_id": claim_id, "shape": "rounded_rect", "position": {"x": 80, "y": y}, "size": {"width": 300, "height": 72}, "label_ref": _label_ref(claim_id, "summary"), "target_id": target["id"] if target["interactive"] else None})
    for index, evidence_id in enumerate(evidence_ids):
        y = 90 + (index + 1) * 420 / (len(evidence_ids) + 1)
        target = _target(f"visual-target:{sequence:04d}:evidence:{index + 1:04d}", evidence_id, [evidence_id], _label_ref(evidence_id, "verbatim_text"), body_ids)
        targets.append(target)
        nodes.append({"id": f"visual-node:{sequence:04d}:evidence:{index + 1:04d}", "source_id": evidence_id, "shape": "rect", "position": {"x": 620, "y": y}, "size": {"width": 300, "height": 72}, "label_ref": _label_ref(evidence_id, "verbatim_text"), "target_id": target["id"] if target["interactive"] else None})
    node_by_source = {node["source_id"]: node["id"] for node in nodes}
    edges = [{"id": f"visual-edge:{sequence:04d}:{index + 1:04d}", "source_node_id": node_by_source[claim_id], "target_node_id": node_by_source[evidence_id], "line": "solid", "marker": "arrow", "source_ids": [claim_id, evidence_id]} for index, (claim_id, evidence_id) in enumerate(pairs)]
    layout = _empty_layout("layered_dag", "left_to_right")
    layout.update(nodes=nodes, edges=edges)
    sources = list(dict.fromkeys([*(visual["id"] for visual in visual_sources), *claim_ids, *evidence_ids]))
    return {
        "id": f"visual-plan:{sequence:04d}", "request_id": request["id"], "section_id": request["section_id"],
        "kind": "derived", "visual_type": "relation", "placement_role": "primary", "source_entity_ids": claim_ids,
        "derived_from": sources, "caption_policy": "label_derived_only", "asset_entity_id": None,
        "description": {"display_label": "派生图", "text": "Claim 与连续正文 Evidence 的可追溯关系。", "source_ids": sources},
        "layout": layout, "targets": targets,
    }


def _flow(request: Mapping[str, Any], entities: Mapping[str, Mapping[str, Any]], types: Mapping[str, str], body_ids: set[str], sequence: int) -> dict[str, Any] | None:
    sources = [item for item in request.get("derived_from", []) if types.get(item) in {"claim", "evidence", "formula", "visual"}]
    explicit = [item for item in entities.values() if types.get(str(item.get("id"))) == "relation" and item.get("source_id") in sources and item.get("target_id") in sources]
    if len(sources) < 2 or not explicit:
        return None
    nodes, targets = [], []
    for index, source_id in enumerate(sources):
        source_type = types[source_id]
        field = {"claim": "summary", "evidence": "verbatim_text", "formula": "latex", "visual": "title"}[source_type]
        evidence = [source_id] if source_type == "evidence" else list(entities[source_id].get("evidence_ids", []))
        target = _target(f"visual-target:{sequence:04d}:{index + 1:04d}", source_id, evidence, _label_ref(source_id, field), body_ids)
        targets.append(target)
        nodes.append({"id": f"visual-node:{sequence:04d}:{index + 1:04d}", "source_id": source_id, "shape": "rounded_rect", "position": {"x": 80 + index * 820 / max(1, len(sources) - 1), "y": 264}, "size": {"width": 180, "height": 72}, "label_ref": _label_ref(source_id, field), "target_id": target["id"] if target["interactive"] else None})
    node_by_source = {node["source_id"]: node["id"] for node in nodes}
    edges = [{"id": f"visual-edge:{sequence:04d}:{index + 1:04d}", "source_node_id": node_by_source[item["source_id"]], "target_node_id": node_by_source[item["target_id"]], "line": "solid", "marker": "arrow", "source_ids": [item["id"]]} for index, item in enumerate(explicit)]
    layout = _empty_layout("layered_dag", "left_to_right")
    layout.update(nodes=nodes, edges=edges)
    derived = list(dict.fromkeys([*sources, *(item["id"] for item in explicit)]))
    return {"id": f"visual-plan:{sequence:04d}", "request_id": request["id"], "section_id": request["section_id"], "kind": "derived", "visual_type": "flow", "placement_role": "primary", "source_entity_ids": sources, "derived_from": derived, "caption_policy": "label_derived_only", "asset_entity_id": None, "description": {"display_label": "派生图", "text": "基于 Paper IR 显式关系生成的方法流程。", "source_ids": derived}, "layout": layout, "targets": targets}


def _build(ir: Mapping[str, Any], content_plan: Mapping[str, Any]) -> dict[str, Any]:
    entities, types = _indexes(ir)
    body_ids = _body_evidence_ids(ir)
    visuals: list[dict[str, Any]] = []
    coverage: list[dict[str, Any]] = []
    warnings: list[str] = []
    for request in content_plan.get("visual_requests", []):
        produced: list[dict[str, Any]] = []
        candidates = [entities[item] for item in request.get("candidate_entity_ids", []) if item in entities]
        if request["semantic_intent"] == "original_figure":
            original = next(
                (
                    item
                    for item in candidates
                    if types[item["id"]] == "visual"
                    and item.get("kind") == "paper_original"
                    and _has_original_figure_asset(item)
                ),
                None,
            )
            if original:
                produced.append(_original_figure(request, original, body_ids, len(visuals) + len(produced) + 1))
        elif request["semantic_intent"] == "original_table":
            table = next(
                (item for item in candidates if types[item["id"]] == "table" and _has_original_table_source(item)),
                None,
            )
            if table:
                produced.append(_original_table(request, table, body_ids, len(visuals) + len(produced) + 1))
                comparison = _comparison(request, table, body_ids, len(visuals) + len(produced) + 1)
                if comparison:
                    produced.append(comparison)
        elif request["semantic_intent"] == "relation":
            relation = _relation(request, entities, types, body_ids, len(visuals) + 1)
            if relation:
                produced.append(relation)
        elif request["semantic_intent"] == "process":
            flow = _flow(request, entities, types, body_ids, len(visuals) + 1)
            if flow:
                produced.append(flow)
        visuals.extend(produced)
        if produced:
            coverage.append({"request_id": request["id"], "status": "fulfilled", "visual_ids": [item["id"] for item in produced], "reason": None})
        else:
            reason = "缺少足以生成该视觉且不引入推断的已验证来源。"
            coverage.append({"request_id": request["id"], "status": "omitted", "visual_ids": [], "reason": reason})
            warnings.append(f"{request['id']}: {reason}")
    visuals.sort(key=lambda item: (item["request_id"], 0 if item["kind"] == "paper_original" else 1, item["id"]))
    return {
        "schema_version": SCHEMA_VERSION, "stage_version": STAGE_VERSION, "paper_id": ir["paper"]["id"],
        "source_ir_sha256": _digest(ir), "source_content_plan_sha256": _digest(content_plan),
        "selection_policy": {"order": "paper_original_first", "derived_visual_rule": "only_when_supported_by_verified_ir", "interaction_rule": "body_evidence_only"},
        "visuals": visuals, "request_coverage": coverage,
        "review": {"status": "needs_review" if warnings else "passed", "warnings": warnings},
    }


def validate_visual_plan(ir: Mapping[str, Any], content_plan: Mapping[str, Any], visual_plan: Mapping[str, Any], project_root: Path | None = None) -> list[str]:
    root = project_root or Path(__file__).resolve().parents[2]
    schema = json.loads((root / "schemas" / "visual-plan.schema.json").read_text(encoding="utf-8"))
    errors = [f"{'/'.join(map(str, error.path))}: {error.message}" for error in Draft202012Validator(schema).iter_errors(visual_plan)]
    entities, types = _indexes(ir)
    body_ids = _body_evidence_ids(ir)
    request_ids = {item["id"] for item in content_plan.get("visual_requests", [])}
    visual_ids = {item.get("id") for item in visual_plan.get("visuals", [])}
    if visual_plan.get("source_ir_sha256") != _digest(ir):
        errors.append("source_ir_sha256 does not match Paper IR")
    if visual_plan.get("source_content_plan_sha256") != _digest(content_plan):
        errors.append("source_content_plan_sha256 does not match content plan")
    for visual in visual_plan.get("visuals", []):
        if visual.get("request_id") not in request_ids:
            errors.append(f"{visual.get('id')}: unknown visual request")
        sources = [*visual.get("source_entity_ids", []), *visual.get("derived_from", [])]
        if any(source not in entities for source in sources):
            errors.append(f"{visual.get('id')}: unknown source entity")
        if visual.get("kind") == "paper_original":
            asset_id = visual.get("asset_entity_id")
            asset = entities.get(asset_id)
            asset_type = types.get(asset_id)
            if asset_type == "visual" and not _has_original_figure_asset(asset or {}):
                errors.append(f"{visual.get('id')}: paper original figure lacks a validated asset")
            elif asset_type == "table" and not _has_original_table_source(asset or {}):
                errors.append(f"{visual.get('id')}: paper original table lacks an asset or structured data")
            elif asset_type not in {"visual", "table"}:
                errors.append(f"{visual.get('id')}: paper original has an invalid asset entity")
        if visual.get("kind") == "derived" and not visual.get("derived_from"):
            errors.append(f"{visual.get('id')}: derived visual lacks derived_from")
        target_ids: set[str] = set()
        for target in visual.get("targets", []):
            if target.get("id") in target_ids:
                errors.append(f"{visual.get('id')}: duplicate target id {target.get('id')}")
            target_ids.add(target.get("id"))
            evidence_ids = target.get("evidence_ids", [])
            if target.get("interactive") and (not evidence_ids or any(item not in body_ids for item in evidence_ids)):
                errors.append(f"{target.get('id')}: interactive target requires body Evidence")
            if not target.get("interactive") and evidence_ids:
                errors.append(f"{target.get('id')}: noninteractive target must not expose Evidence")
        for collection in (visual.get("layout", {}).get("nodes", []), visual.get("layout", {}).get("marks", [])):
            for primitive in collection:
                if primitive.get("target_id") is not None and primitive.get("target_id") not in target_ids:
                    errors.append(f"{primitive.get('id')}: unknown target id")
    for item in visual_plan.get("request_coverage", []):
        if item.get("request_id") not in request_ids:
            errors.append(f"unknown coverage request {item.get('request_id')}")
        if any(visual_id not in visual_ids for visual_id in item.get("visual_ids", [])):
            errors.append(f"{item.get('request_id')}: coverage references unknown visual")
    return errors


def build_visual_plan(ir: Mapping[str, Any], content_plan: Mapping[str, Any], *, project_root: Path | None = None) -> dict[str, Any]:
    root = project_root or Path(__file__).resolve().parents[2]
    ir_errors = validate_ir(dict(ir), root)
    if ir_errors:
        raise VisualPlanningError("invalid Paper IR: " + "; ".join(ir_errors))
    content_errors = validate_content_plan(ir, content_plan, root)
    if content_errors:
        raise VisualPlanningError("invalid content plan: " + "; ".join(content_errors))
    result = _build(ir, content_plan)
    errors = validate_visual_plan(ir, content_plan, result, root)
    if errors:
        raise VisualPlanningError("invalid visual plan: " + "; ".join(errors))
    return result


def plan_visuals_file(ir_path: Path | str, content_plan_path: Path | str, *, output_path: Path | str, project_root: Path | None = None) -> dict[str, Any]:
    ir = json.loads(Path(ir_path).read_text(encoding="utf-8"))
    content_plan = json.loads(Path(content_plan_path).read_text(encoding="utf-8"))
    result = build_visual_plan(ir, content_plan, project_root=project_root)
    atomic_write_json(Path(output_path), result)
    return result
