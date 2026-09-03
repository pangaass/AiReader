"""Resolve validated planning artifacts into a renderer-neutral page model."""

from __future__ import annotations

import base64
import hashlib
import html
import json
import mimetypes
import re
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

from jsonschema import Draft202012Validator

from ..planning import validate_content_plan
from ..related import validate_related_work_plan
from ..validation import validate_ir
from ..visuals import validate_visual_plan


SCHEMA_VERSION = "1.0.0"
STAGE_VERSION = "2.0.0"
SECTION_NUMBERS = {
    "one_minute_read": "01",
    "research_task": "02",
    "existing_methods": "03",
    "motivation": "04",
    "method_overview": "05",
    "method_details": "06",
    "training_inference": "07",
    "experimental_setup": "08",
    "main_results": "09",
    "analysis": "10",
    "conclusion_limitations": "11",
}
SECTION_INTROS = {
    "one_minute_read": "先抓住研究问题、核心方法与关键结果，再按需深入。",
    "research_task": "说明论文要解决什么问题，以及输入、输出和应用场景。",
    "existing_methods": "梳理已有技术路线及其不足，建立研究脉络。",
    "motivation": "从已有方法的缺口推导本文的核心出发点。",
    "method_overview": "先用整体流程建立对方法的全局认识。",
    "method_details": "按论文实际模块逐块解释作用、输入输出和连接关系。",
    "training_inference": "说明目标函数、训练流程、推理过程与关键实现。",
    "experimental_setup": "交代数据集、评价指标、基线和实验配置。",
    "main_results": "结合论文原图和原表解释主要结果及其意义。",
    "analysis": "汇总消融、参数、效率、鲁棒性、案例与失败分析。",
    "conclusion_limitations": "归纳论文结论、适用边界、局限和未来方向。",
}


class PageModelError(ValueError):
    """Raised when page data cannot be resolved without losing provenance."""


def _digest(value: Mapping[str, Any]) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _indexes(ir: Mapping[str, Any]) -> tuple[dict[str, Mapping[str, Any]], dict[str, str]]:
    entities: dict[str, Mapping[str, Any]] = {}
    types: dict[str, str] = {}
    for group in ("sections", "evidence", "claims", "formulas", "variables", "visuals", "tables", "citations", "relations"):
        for item in ir.get(group, []):
            entity_id = str(item["id"])
            entities[entity_id] = item
            types[entity_id] = group[:-1] if group.endswith("s") else group
    return entities, types


def _safe_web_url(value: object) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    parsed = urlsplit(raw)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return None
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, parsed.path, parsed.query, parsed.fragment))


def _source_pdf_url(ir: Mapping[str, Any]) -> str | None:
    source = ir.get("source", {})
    remote = _safe_web_url(source.get("original_url"))
    if remote:
        return remote.split("#", 1)[0]
    # Never serialize machine-local absolute paths into a portable HTML file.
    # The renderer embeds a local PDF once and creates a blob URL at runtime.
    return None


def _page_href(pdf_url: str | None, page: object) -> str | None:
    if not pdf_url:
        return None
    try:
        number = max(1, int(page))
    except (TypeError, ValueError):
        return pdf_url
    return f"{pdf_url.split('#', 1)[0]}#page={number}"


def _data_uri(asset_path: object) -> str | None:
    raw = str(asset_path or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_file():
        return None
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if mime not in {"image/png", "image/jpeg", "image/webp", "image/gif"} or path.stat().st_size > 25 * 1024 * 1024:
        return None
    data = path.read_bytes()
    signatures = {
        "image/png": data.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/jpeg": data.startswith(b"\xff\xd8\xff"),
        "image/gif": data.startswith((b"GIF87a", b"GIF89a")),
        "image/webp": len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP",
    }
    if not signatures[mime]:
        return None
    payload = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{payload}"


_LATEX_COMMANDS = {
    r"\alpha": "α", r"\beta": "β", r"\gamma": "γ", r"\delta": "δ", r"\epsilon": "ε",
    r"\theta": "θ", r"\lambda": "λ", r"\mu": "μ", r"\pi": "π", r"\rho": "ρ", r"\sigma": "σ",
    r"\phi": "φ", r"\omega": "ω", r"\tau": "τ", r"\sum": "∑", r"\prod": "∏", r"\in": "∈",
    r"\leq": "≤", r"\geq": "≥", r"\neq": "≠", r"\times": "×", r"\cdot": "·",
    r"\le": "≤", r"\ge": "≥", r"\to": "→", r"\approx": "≈", r"\int": "∫",
    r"\rightarrow": "→", r"\leftarrow": "←", r"\infty": "∞", r"\partial": "∂", r"\sqrt": "√",
}


def _formula_display(latex: str) -> str:
    value = latex.strip().strip("$")
    for command, glyph in _LATEX_COMMANDS.items():
        value = value.replace(command, glyph)
    value = re.sub(r"\\(?:mathbf|mathrm|mathit|mathcal|operatorname)\{([^{}]*)\}", r"\1", value)
    value = value.replace(r"\left", "").replace(r"\right", "")
    value = value.replace(r"\,", " ").replace(r"\;", " ").replace(r"\!", "")
    # Restore a multiplication boundary commonly lost by PDF extraction,
    # e.g. ``xW1``. Restrict this to a one-letter lowercase operand followed
    # by an uppercase indexed symbol so ordinary camel-case names stay intact.
    value = re.sub(r"(?<=[a-zα-ω])(?=[A-ZΑ-Ω]\d)", " ", value)
    return value


def _canonical_symbol(symbol: str) -> str:
    """Restore common subscript notation lost by PDF text extraction."""

    value = _formula_display(symbol).strip()
    if match := re.fullmatch(r"([A-Za-zΑ-Ωα-ω]+)_([A-Za-z0-9]+)", value):
        return f"{match.group(1)}_{{{match.group(2)}}}"
    if "_" in value or "^" in value:
        return value
    if re.fullmatch(r"[A-Za-zΑ-Ωα-ω]\d+", value):
        return f"{value[0]}_{{{value[1:]}}}"
    if re.fullmatch(r"[A-ZΑ-Ω][a-z]", value):
        return f"{value[0]}_{{{value[1:]}}}"
    if re.fullmatch(r"[Α-Ωα-ω][A-Za-z]", value):
        return f"{value[0]}_{{{value[1:]}}}"
    if match := re.fullmatch(r"(head)([a-z0-9])", value, re.IGNORECASE):
        return f"{match.group(1)}_{{{match.group(2)}}}"
    return value


def _formula_notation(latex: str, variables: list[Mapping[str, Any]]) -> str:
    """Return conservative LaTeX-like notation suitable for MathML parsing.

    PDF text layers frequently flatten scripts and occasionally drop the
    division stroke before a square root.  Repairs are syntax-based and use
    the formula's declared variable inventory; no paper title or sample data
    is consulted.
    """

    value = _formula_display(latex)
    value = value.replace("−", "-").replace("µ", "μ")

    # Matrix projection notation is commonly flattened as ``W Q i``.
    value = re.sub(r"\b([QKV])W\s+([A-Z])\s+([A-Za-z0-9]+)\b", r"\1 W_{\3}^{\2}", value)
    value = re.sub(r"\bW\s+([A-Z])\s+([A-Za-z0-9]+)\b", r"W_{\2}^{\1}", value)
    value = re.sub(r"\bW\s+([A-Z])\b", r"W^{\1}", value)
    value = re.sub(r"\b([A-Z])([A-Z])T\b", r"\1 \2^T", value)

    # A negative decimal immediately following an identifier in extracted
    # formula text is an exponent, not prose subtraction.
    value = re.sub(r"\b([A-Za-z][A-Za-z0-9_{}]*)-([01]\.[05])\b", r"\1^{-\2}", value)
    value = re.sub(r"\bd\^{-0\.5\}\s+model\b", r"d_{model}^{-0.5}", value)

    replacements: list[tuple[str, str]] = []
    for variable in variables:
        raw = _formula_display(str(variable.get("symbol", ""))).strip()
        canonical = _canonical_symbol(raw)
        if not raw:
            continue
        aliases = {raw, re.sub(r"[_{}\s]", "", raw)}
        for alias in aliases:
            if alias and alias != canonical:
                replacements.append((alias, canonical))
    for alias, canonical in sorted(replacements, key=lambda item: len(item[0]), reverse=True):
        value = re.sub(rf"(?<![A-Za-z0-9_]){re.escape(alias)}(?![A-Za-z0-9_])", lambda _: canonical, value)

    value = re.sub(r"\b([Ar])([0-9]{2})\b", r"\1_{\2}", value)
    value = re.sub(r"([Α-Ωα-ω])([A-Za-z])\b", r"\1_{\2}", value)
    value = re.sub(r"\bhead([0-9ih])\b", r"head_{\1}", value)
    value = re.sub(r"√\s*([A-Za-zΑ-Ωα-ω](?:_\{?[A-Za-z0-9]+\}?)?)", r"√{\1}", value)
    # Softmax scaling is often extracted with the fraction bar omitted while
    # retaining the radical denominator.
    value = re.sub(r"(softmax\([^()]*)\s+√", r"\1 / √", value, flags=re.IGNORECASE)
    value = re.sub(
        r"(softmax\()([^()]*)\s*/\s*(√\{(?:[^{}]|\{[^{}]*\})+\})",
        lambda match: f"{match.group(1)}\\frac{{{match.group(2).strip()}}}{{{match.group(3)}}}",
        value,
        flags=re.IGNORECASE,
    )
    # Positional-encoding powers are commonly flattened to 10000 followed by
    # the exponent text.
    value = re.sub(r"\b10000\s*2i/([A-Za-z](?:_\{?[A-Za-z0-9]+\}?)?)", r"10000^{2i/\1}", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _formula_mathml(latex: str, variables: list[Mapping[str, Any]]) -> str:
    """Build escaped, dependency-free MathML with interactive variables."""

    notation = _formula_notation(latex, variables)
    variable_by_key: dict[str, Mapping[str, Any]] = {}
    for variable in variables:
        raw = _formula_display(str(variable.get("symbol", ""))).strip()
        for alias in {raw, _canonical_symbol(raw), re.sub(r"[_{}\s]", "", raw)}:
            key = re.sub(r"[_{}\s]", "", alias)
            if key:
                variable_by_key.setdefault(key, variable)

    functions = {"Attention", "Concat", "FFN", "MultiHead", "PE", "cos", "head", "lrate", "max", "min", "sin", "softmax"}

    class Parser:
        def __init__(self, value: str) -> None:
            self.value = value
            self.index = 0

        def row(self, stop: str | None = None, *, allow_interactive: bool = True) -> tuple[str, str]:
            rendered: list[str] = []
            plain: list[str] = []
            while self.index < len(self.value):
                if stop and self.value[self.index] == stop:
                    self.index += 1
                    break
                if self.value[self.index].isspace():
                    self.index += 1
                    if rendered and (not rendered[-1].endswith("</mo>")):
                        rendered.append('<mspace width=".22em"/>')
                    continue
                markup, key = self.atom(allow_interactive=allow_interactive)
                if markup:
                    rendered.append(markup)
                    plain.append(key)
            return "".join(rendered), "".join(plain)

        def group(self, *, allow_interactive: bool = True) -> tuple[str, str]:
            if self.index >= len(self.value):
                return "", ""
            if self.index < len(self.value) and self.value[self.index] == "{":
                self.index += 1
                return self.row("}", allow_interactive=allow_interactive)
            signed_number = re.match(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)", self.value[self.index :])
            if signed_number:
                value = signed_number.group(0)
                self.index += len(value)
                sign = "−" if value.startswith("-") else ("+" if value.startswith("+") else "")
                number = value[1:] if sign else value
                markup = (f"<mo>{sign}</mo>" if sign else "") + f"<mn>{html.escape(number)}</mn>"
                return markup, sign + number
            return self.atom(allow_scripts=False, allow_interactive=allow_interactive)

        def atom(self, *, allow_scripts: bool = True, allow_interactive: bool = True) -> tuple[str, str]:
            if self.index >= len(self.value):
                return "", ""
            ch = self.value[self.index]
            if self.value.startswith(r"\frac", self.index):
                self.index += len(r"\frac")
                numerator, numerator_key = self.group()
                denominator, denominator_key = self.group()
                return f"<mfrac><mrow>{numerator}</mrow><mrow>{denominator}</mrow></mfrac>", numerator_key + "/" + denominator_key
            if ch == "√":
                self.index += 1
                radicand, key = self.group()
                return f"<msqrt><mrow>{radicand}</mrow></msqrt>", "sqrt" + key
            if ch == "{":
                self.index += 1
                body, key = self.row("}")
                return f"<mrow>{body}</mrow>", key
            if ch == "\\":
                match = re.match(r"\\([A-Za-z]+)", self.value[self.index :])
                word = match.group(1) if match else ""
                self.index += len(match.group(0)) if match else 1
                base_markup, base_key = f'<mi mathvariant="normal">{html.escape(word)}</mi>', word
            elif ch.isalpha() or "\u0391" <= ch <= "\u03ff":
                match = re.match(r"[A-Za-zΑ-Ͽ]+", self.value[self.index :])
                word = match.group(0) if match else ch
                self.index += len(word)
                variant = ' mathvariant="normal"' if word in functions or len(word) > 1 else ""
                base_markup, base_key = f"<mi{variant}>{html.escape(word)}</mi>", word
            elif ch.isdigit() or (ch == "." and self.index + 1 < len(self.value) and self.value[self.index + 1].isdigit()):
                match = re.match(r"(?:\d+(?:\.\d*)?|\.\d+)", self.value[self.index :])
                number = match.group(0) if match else ch
                self.index += len(number)
                base_markup, base_key = f"<mn>{html.escape(number)}</mn>", number
            else:
                self.index += 1
                operator = "−" if ch == "-" else ch
                return f"<mo>{html.escape(operator)}</mo>", operator

            sub_markup = sub_key = sup_markup = sup_key = None
            if allow_scripts:
                while self.index < len(self.value) and self.value[self.index] in "_^":
                    kind = self.value[self.index]
                    self.index += 1
                    # A script may itself be a modeled variable (for example
                    # the O in W^O). Keep it interactive unless the complete
                    # scripted symbol (for example d_k) is modeled as one
                    # variable below.
                    script_markup, script_key = self.group(allow_interactive=allow_interactive)
                    if kind == "_":
                        sub_markup, sub_key = script_markup, script_key
                    else:
                        sup_markup, sup_key = script_markup, script_key
            scripted_key = base_key + (sub_key or "") + (sup_key or "")
            full_scripted_variable = variable_by_key.get(re.sub(r"[_{}\s]", "", scripted_key)) if (sub_key or sup_key) else None
            subscript_variable = variable_by_key.get(re.sub(r"[_{}\s]", "", base_key + (sub_key or ""))) if sub_key else None
            superscript_variable = variable_by_key.get(re.sub(r"[_{}\s]", "", base_key + (sup_key or ""))) if sup_key and not sub_key else None
            scripted_variable = full_scripted_variable or subscript_variable or superscript_variable
            base_variable = variable_by_key.get(re.sub(r"[_{}\s]", "", base_key))
            # When no composite variable owns the full notation, bind the base
            # independently before assembling scripts. This avoids nesting the
            # clickable O inside a clickable W for W^O.
            if allow_interactive and scripted_variable is None and base_variable is not None:
                evidence_id = html.escape(str(base_variable.get("definition_evidence_id") or ""), quote=True)
                variable_id = html.escape(str(base_variable.get("id") or ""), quote=True)
                symbol = html.escape(str(base_variable.get("symbol") or base_key), quote=True)
                base_markup = (
                    f'<mrow class="math-var" role="button" tabindex="0" data-evidence-id="{evidence_id}" '
                    f'data-variable-id="{variable_id}" data-variable-symbol="{symbol}" '
                    f'aria-label="查看变量 {symbol} 的论文定义">{base_markup}</mrow>'
                )
            if sub_markup is not None and sup_markup is not None and subscript_variable is not None and full_scripted_variable is None:
                base_markup = f"<msub>{base_markup}<mrow>{sub_markup}</mrow></msub>"
                evidence_id = html.escape(str(subscript_variable.get("definition_evidence_id") or ""), quote=True)
                variable_id = html.escape(str(subscript_variable.get("id") or ""), quote=True)
                symbol = html.escape(str(subscript_variable.get("symbol") or (base_key + (sub_key or ""))), quote=True)
                base_markup = (
                    f'<mrow class="math-var" role="button" tabindex="0" data-evidence-id="{evidence_id}" '
                    f'data-variable-id="{variable_id}" data-variable-symbol="{symbol}" '
                    f'aria-label="查看变量 {symbol} 的论文定义">{base_markup}</mrow>'
                )
                base_markup = f"<msup>{base_markup}<mrow>{sup_markup}</mrow></msup>"
                scripted_variable = None
            elif sub_markup is not None and sup_markup is not None:
                base_markup = f"<msubsup>{base_markup}<mrow>{sub_markup}</mrow><mrow>{sup_markup}</mrow></msubsup>"
            elif sub_markup is not None:
                base_markup = f"<msub>{base_markup}<mrow>{sub_markup}</mrow></msub>"
            elif sup_markup is not None:
                base_markup = f"<msup>{base_markup}<mrow>{sup_markup}</mrow></msup>"
            full_key = base_key + (sub_key or "")
            variable = scripted_variable
            if allow_interactive and variable is not None:
                evidence_id = html.escape(str(variable.get("definition_evidence_id") or ""), quote=True)
                variable_id = html.escape(str(variable.get("id") or ""), quote=True)
                symbol = html.escape(str(variable.get("symbol") or full_key), quote=True)
                base_markup = (
                    f'<mrow class="math-var" role="button" tabindex="0" data-evidence-id="{evidence_id}" '
                    f'data-variable-id="{variable_id}" data-variable-symbol="{symbol}" '
                    f'aria-label="查看变量 {symbol} 的论文定义">{base_markup}</mrow>'
                )
            return base_markup, full_key + (("^" + sup_key) if sup_key else "")

    body, _ = Parser(notation).row()
    aria = html.escape(notation, quote=True)
    return f'<math display="block" aria-label="{aria}"><mrow>{body}</mrow></math>'


def _formula_tokens(latex: str, variables: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    display = _formula_display(latex)
    patterns: list[tuple[str, Mapping[str, Any]]] = []
    seen_patterns: set[str] = set()
    for variable in variables:
        symbol = _formula_display(str(variable.get("symbol", "")))
        if not symbol:
            continue
        subscript = re.fullmatch(r"([A-Za-z\u0391-\u03a9\u03b1-\u03c9]+)_\{?([A-Za-z0-9]+)\}?", symbol)
        if subscript:
            base, suffix = subscript.groups()
            # Accept both canonical d_{model}/d_model and the common PDF
            # extraction form dmodel while keeping the displayed source text.
            token_pattern = rf"{re.escape(base)}(?:_?\{{?{re.escape(suffix)}\}}?)"
        else:
            token_pattern = re.escape(symbol)
        if token_pattern not in seen_patterns:
            patterns.append((token_pattern, variable))
            seen_patterns.add(token_pattern)
    if not patterns:
        return [{"text": display, "variable_id": None, "evidence_id": None, "symbol": None}]
    patterns.sort(key=lambda item: len(item[0]), reverse=True)
    pattern = re.compile(r"(?<!\w)(" + "|".join(f"(?:{item[0]})" for item in patterns) + r")(?!\w)")
    parts = pattern.split(display)
    tokens: list[dict[str, Any]] = []
    for part in parts:
        if not part:
            continue
        variable = next((item for token_pattern, item in patterns if re.fullmatch(token_pattern, part)), None)
        tokens.append({
            "text": part,
            "variable_id": variable.get("id") if variable is not None else None,
            "evidence_id": variable.get("definition_evidence_id") if variable is not None else None,
            "symbol": part if variable else None,
        })
    return tokens


def _resolve_label(ref: Mapping[str, Any], entities: Mapping[str, Mapping[str, Any]]) -> str:
    source = entities.get(str(ref.get("source_id")), {})
    field = str(ref.get("field", ""))
    if field == "cell_value":
        try:
            return str(source.get("rows", [])[int(ref["row"])][int(ref["column"])])
        except (IndexError, KeyError, TypeError, ValueError):
            return ""
    return str(source.get(field) or "")


def _visual_component(
    visual: Mapping[str, Any], entities: Mapping[str, Mapping[str, Any]], pdf_url: str | None,
) -> dict[str, Any] | None:
    source_id = str(visual.get("asset_entity_id") or "")
    source = entities.get(source_id, {})
    targets = {item["id"]: item for item in visual.get("targets", [])}
    base = {
        "id": visual["id"], "kind": visual["kind"], "visual_type": visual["visual_type"],
        "label": "论文原图" if visual["kind"] == "paper_original" else "派生图",
        "description": (visual.get("description") or {}).get("text"),
    }
    if visual["visual_type"] == "figure":
        locator = source.get("locator") or {}
        base.update({
            "component": "figure", "title": source.get("title") or "Figure",
            "caption": source.get("caption") if visual.get("caption_policy") == "show_official_once" else None,
            "asset_data_uri": _data_uri(source.get("asset_path")), "alt": source.get("title") or source.get("caption") or "论文原图",
            "page": locator.get("page"), "href": _page_href(pdf_url, locator.get("page")),
        })
        return base
    if visual["visual_type"] == "table":
        locator = source.get("locator") or {}
        interactive = {}
        for target in visual.get("targets", []):
            ref = target.get("label_ref", {})
            if target.get("interactive") and target.get("evidence_ids"):
                interactive[f"{ref.get('row')}:{ref.get('column')}"] = target["evidence_ids"][0]
        base.update({
            "component": "table", "title": "论文表格", "caption": source.get("caption"),
            "columns": list(source.get("columns", [])), "rows": list(source.get("rows", [])),
            "asset_data_uri": _data_uri(source.get("asset_path")), "alt": source.get("caption") or "论文原表",
            "interactive_cells": interactive, "page": locator.get("page"), "href": _page_href(pdf_url, locator.get("page")),
        })
        return base
    layout = visual.get("layout", {})
    nodes = []
    for node in layout.get("nodes", []):
        target = targets.get(node.get("target_id"), {})
        nodes.append({
            **node, "label": _resolve_label(node.get("label_ref", {}), entities),
            "evidence_id": (target.get("evidence_ids") or [None])[0] if target.get("interactive") else None,
        })
    node_by_id = {node["id"]: node for node in nodes}
    edges = []
    for edge in layout.get("edges", []):
        start = node_by_id.get(edge.get("source_node_id"))
        end = node_by_id.get(edge.get("target_node_id"))
        if not start or not end:
            continue
        edges.append({
            **edge,
            "x1": float(start["position"]["x"]) + float(start["size"]["width"]) / 2,
            "y1": float(start["position"]["y"]) + float(start["size"]["height"]) / 2,
            "x2": float(end["position"]["x"]) + float(end["size"]["width"]) / 2,
            "y2": float(end["position"]["y"]) + float(end["size"]["height"]) / 2,
        })
    marks = []
    for mark in layout.get("marks", []):
        target = targets.get(mark.get("target_id"), {})
        marks.append({
            **mark, "label": _resolve_label(mark.get("label_ref", {}), entities),
            "evidence_id": (target.get("evidence_ids") or [None])[0] if target.get("interactive") else None,
        })
    base.update({
        "component": "derived_visual", "title": visual["visual_type"].replace("_", " ").title(),
        "view_box": layout.get("view_box", [0, 0, 1000, 600]), "nodes": nodes,
        "edges": edges, "marks": marks,
    })
    return base


def _related_component(plan: Mapping[str, Any], evidence: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    impact_by_kind = {
        "same_problem": ("研究问题", "界定任务与关键现象"),
        "foundation_inheritance": ("提出方法", "继承、扩展或改造技术路线"),
        "data_evaluation": ("设计实验", "形成数据、指标、基线或协议"),
        "improvement_comparison": ("动机与差异", "暴露缺口并说明本文的不同选择"),
    }
    nodes = list(plan.get("nodes", []))
    clusters = list(plan.get("clusters", []))
    cluster_by_id = {item["id"]: item for item in clusters}
    edge_by_target = {item["target_id"]: item for item in plan.get("edges", [])}
    resolved: list[dict[str, Any]] = []
    for node in nodes:
        metadata = node.get("metadata", {})
        edge = edge_by_target.get(node["id"], {})
        cluster_id = str(edge.get("cluster_id") or (node.get("cluster_ids") or [""])[0])
        cluster = cluster_by_id.get(cluster_id, {})
        evidence_id = (node.get("description", {}).get("evidence_ids") or [None])[0]
        impact_label, impact_description = impact_by_kind.get(str(edge.get("kind") or ""), ("研究脉络", "说明本文与前序工作的关系"))
        evidence_item = evidence.get(str(evidence_id or ""), {})
        resolved.append({
            "id": node["id"],
            "title": metadata.get("title") or node.get("raw_reference"), "year": metadata.get("year"),
            "url": _safe_web_url(metadata.get("url")), "cluster_ids": list(node.get("cluster_ids", [])),
            "relation_kind": edge.get("kind"), "relation_label": cluster.get("label"),
            "evidence_id": evidence_id,
            "evidence_excerpt": evidence_item.get("verbatim_text"),
            "impact_label": impact_label, "impact_description": impact_description,
            "verification": node.get("verification", {}).get("status", "unverified"),
        })
    by_id = {item["id"]: item for item in resolved}
    relation_groups = []
    for cluster in clusters:
        members = [by_id[item] for item in cluster.get("member_node_ids", []) if item in by_id]
        members.sort(key=lambda item: (item.get("year") is None, item.get("year") or 0, str(item.get("title") or "")))
        relation_groups.append({
            "id": cluster["id"], "kind": cluster["id"].removeprefix("cluster:"),
            "label": cluster.get("label"), "description": cluster.get("description"),
            "impact_label": impact_by_kind.get(cluster["id"].removeprefix("cluster:"), ("研究脉络", ""))[0],
            "nodes": members,
        })
    return {
        "component": "related_work", "current": plan.get("current_paper", {}).get("metadata", {}),
        "current_id": plan.get("paper_id"), "nodes": resolved, "relation_groups": relation_groups,
        "warnings": list(plan.get("review", {}).get("warnings", [])),
    }


def validate_page_model(model: Mapping[str, Any], project_root: Path | None = None) -> list[str]:
    root = project_root or Path(__file__).resolve().parents[2]
    schema = json.loads((root / "schemas" / "page-model.schema.json").read_text(encoding="utf-8"))
    errors = [f"{'/'.join(map(str, error.path))}: {error.message}" for error in Draft202012Validator(schema).iter_errors(model)]
    evidence_ids = set(model.get("evidence", {}))
    for section in model.get("sections", []):
        for component in section.get("components", []):
            references = list(component.get("evidence_ids", []))
            references += [item.get("evidence_id") for item in component.get("tokens", [])]
            references += list(component.get("interactive_cells", {}).values())
            references += [item.get("evidence_id") for item in component.get("nodes", [])]
            references += [item.get("evidence_id") for item in component.get("marks", [])]
            for evidence_id in filter(None, references):
                if evidence_id not in evidence_ids:
                    errors.append(f"{component.get('id')}: unknown Evidence {evidence_id}")
    return errors


def build_page_model(
    ir: Mapping[str, Any], content_plan: Mapping[str, Any], visual_plan: Mapping[str, Any],
    related_work: Mapping[str, Any] | None = None, *, project_root: Path | None = None,
    allow_unreviewed: bool = False,
) -> dict[str, Any]:
    """Build a deterministic page model after validating all upstream layers."""

    root = project_root or Path(__file__).resolve().parents[2]
    upstream_errors = validate_ir(dict(ir), root)
    upstream_errors += validate_content_plan(ir, content_plan, root)
    upstream_errors += validate_visual_plan(ir, content_plan, visual_plan, root)
    if related_work is not None:
        upstream_errors += validate_related_work_plan(ir, related_work, root)
    review_artifacts = [content_plan, visual_plan, *([related_work] if related_work is not None else [])]
    if not allow_unreviewed:
        pending = [str(item.get("paper_id", "artifact")) for item in review_artifacts if item.get("review", {}).get("status") != "passed"]
        if pending:
            upstream_errors.append("downstream rendering requires review.status=passed; use allow_unreviewed only for a marked preview")
    if upstream_errors:
        raise PageModelError("invalid upstream artifact: " + "; ".join(upstream_errors[:8]))

    entities, types = _indexes(ir)
    pdf_url = _source_pdf_url(ir)
    evidence = {
        item["id"]: {
            "id": item["id"], "verbatim_text": item["verbatim_text"],
            "page": item["locator"]["page"], "href": _page_href(pdf_url, item["locator"]["page"]),
            "section_id": item.get("section_id"),
        }
        for item in ir.get("evidence", [])
        if item.get("provenance", {}).get("kind") == "paper_verbatim"
    }
    item_map = {item["id"]: item for item in content_plan.get("items", [])}
    visual_by_section: dict[str, list[dict[str, Any]]] = {}
    for visual in visual_plan.get("visuals", []):
        component = _visual_component(visual, entities, pdf_url)
        if component:
            visual_by_section.setdefault(str(visual["section_id"]), []).append(component)

    sections: list[dict[str, Any]] = []
    for section in content_plan.get("sections", []):
        components: list[dict[str, Any]] = []
        section_visuals = visual_by_section.get(section["id"], [])
        visual_sources = {source for visual in section_visuals for source in visual.get("source_entity_ids", [])}
        for item_id in section.get("item_ids", []):
            item = item_map.get(item_id)
            if not item:
                continue
            content_type = item["content_type"]
            if content_type == "narrative":
                components.append({
                    "component": "narrative",
                    "id": item["id"], "title": item["title"], "body": item.get("body"),
                    "evidence_ids": list(item.get("evidence_ids", [])),
                })
            elif content_type == "formula":
                formula = entities.get(str(item.get("formula_id")), {})
                variables = [entities[var_id] for var_id in item.get("variable_ids", []) if var_id in entities]
                locator = formula.get("locator", {})
                components.append({
                    "component": "formula", "id": item["id"], "title": item["title"], "label": formula.get("label"),
                    "latex": formula.get("latex", ""), "tokens": _formula_tokens(str(formula.get("latex", "")), variables),
                    "mathml": _formula_mathml(str(formula.get("latex", "")), variables),
                    "render_note": "该公式包含暂不支持的命令，需人工复核排版。" if re.search(r"\\(?!frac\b)[A-Za-z]+", _formula_notation(str(formula.get("latex", "")), variables)) else None,
                    "page": locator.get("page"), "href": _page_href(pdf_url, locator.get("page")),
                })
            elif content_type == "citation" and related_work is None:
                citation = entities.get(str(item.get("citation_id")), {})
                components.append({
                    "component": "citation", "id": item["id"], "title": citation.get("title") or citation.get("raw_reference"),
                    "authors": citation.get("authors", []), "year": citation.get("year"),
                    "url": _safe_web_url(citation.get("external_url")), "verification": citation.get("verification"),
                    "evidence_ids": list(item.get("evidence_ids", [])),
                })
            elif content_type in {"paper_visual", "table"}:
                # Visual-plan components are appended once below; the content item is only a placement reference.
                continue
        components.extend(section_visuals)
        if section["kind"] == "existing_methods" and related_work is not None:
            # The relationship graph already explains each cited work in
            # context. Standalone citation cards would repeat the same material.
            components = [
                item for item in components
                if item.get("component") not in {"citation"}
            ]
            components.append(_related_component(related_work, evidence))
        sections.append({
            "id": section["id"].replace(":", "-"), "kind": section["kind"],
            "number": SECTION_NUMBERS.get(section["kind"], ""), "title": section["title"],
            "intro": SECTION_INTROS.get(section["kind"], ""), "status": section["status"],
            "fallback_reason": section.get("fallback_reason"), "components": components,
        })

    paper = ir["paper"]
    planned_metadata = content_plan.get("paper_metadata", {})
    model = {
        "schema_version": SCHEMA_VERSION, "stage_version": STAGE_VERSION,
        "source_hashes": {"ir": _digest(ir), "content_plan": _digest(content_plan), "visual_plan": _digest(visual_plan), "related_work": _digest(related_work) if related_work else None},
        "paper": {
            "id": paper["id"], "title": planned_metadata.get("title") or paper["title"],
            "authors": list(planned_metadata.get("authors") or paper.get("authors", [])),
            "affiliations": list(planned_metadata.get("affiliations") or []),
            "year": paper.get("year"), "venue": planned_metadata.get("venue") or paper.get("venue"),
            "published_at": planned_metadata.get("published_at"),
            "doi": planned_metadata.get("doi") or paper.get("doi"),
            "arxiv_id": planned_metadata.get("arxiv_id") or paper.get("arxiv_id"),
            "abstract": paper.get("abstract", ""),
            "external_url": _safe_web_url(paper.get("external_url")), "pdf_url": pdf_url,
            "page_count": ir.get("source", {}).get("page_count"),
        },
        "preview": any(item.get("review", {}).get("status") != "passed" for item in review_artifacts) or any(
            component.get("component") == "formula" and component.get("render_note")
            for section in sections for component in section["components"]
        ),
        "navigation": [{"id": section["id"].replace(":", "-"), "label": section["title"]} for section in content_plan.get("sections", [])],
        "sections": sections, "evidence": evidence,
    }
    errors = validate_page_model(model, root)
    if errors:
        raise PageModelError("invalid page model: " + "; ".join(errors[:8]))
    return model
