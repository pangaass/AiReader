"""Read-only aggregation and persistence for independent review results."""

from __future__ import annotations

import hashlib
import html as html_module
import json
import os
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Mapping, Sequence

from jsonschema import Draft202012Validator

from .static import run_static_checks


SCHEMA_VERSION = "1.0.0"
STAGE_VERSION = "1.1.1"


BROWSER_HOOKS: tuple[dict[str, Any], ...] = (
    {
        "id": "browser.evidence_dialog",
        "description": "Evidence 卡片可由鼠标和键盘打开、关闭，并恢复焦点。",
        "viewport": {"width": 1280, "height": 800},
        "steps": ["聚焦首个 Evidence 触发器", "按 Enter 打开卡片", "按 Escape 关闭卡片"],
        "assertions": ["卡片展示 registry 原文", "仅有关闭和跳转两个操作", "焦点返回触发器"],
    },
    {
        "id": "browser.formula_variable",
        "description": "公式内部变量可直接用键盘查看定义原文。",
        "viewport": {"width": 1280, "height": 800},
        "steps": ["聚焦公式中的首个变量", "按 Space 打开 Evidence 卡片"],
        "assertions": ["变量位于 math 元素内部", "原文中当前变量被强调", "跳转页与变量定义页一致"],
    },
    {
        "id": "browser.pdf_jump",
        "description": "Evidence 与图表跳转保留准确的 PDF 页码。",
        "viewport": {"width": 1280, "height": 800},
        "steps": ["打开一条 Evidence", "检查跳转链接", "打开一张论文原图"],
        "assertions": ["链接包含 #page=<准确页码>", "本地 PDF 使用内嵌 blob", "外部链接安全打开"],
    },
    {
        "id": "browser.mobile_layout",
        "description": "窄屏下页面无整页横向溢出，导航和卡片保持可用。",
        "viewport": {"width": 390, "height": 844},
        "steps": ["设置移动端视口", "滚动所有章节", "打开并关闭 Evidence 卡片"],
        "assertions": ["document.scrollWidth 不超过 viewport", "导航可操作", "弹卡按钮保持可见"],
    },
    {
        "id": "browser.accessibility_keyboard",
        "description": "主要交互无需鼠标即可完成，且焦点可见。",
        "viewport": {"width": 1280, "height": 800},
        "steps": ["从跳过链接开始连续按 Tab", "操作主题、原图和 Related Work 节点"],
        "assertions": ["焦点顺序合理", "交互元素有可访问名称", "对话框不会丢失焦点"],
    },
    {
        "id": "browser.visual_fidelity",
        "description": "公式、论文图表、内容层级与 Related Work 在实际页面中清晰且语义匹配。",
        "viewport": {"width": 1280, "height": 800},
        "steps": ["检查全部公式与实验图表", "核对实验卡片内容", "检查 Related Work 桌面和移动端布局"],
        "assertions": ["上下标、根号和指数结构可辨认", "表格内容非空且未误裁 caption", "实验内容属于实验章节", "关系类别和关系原文可见", "没有大面积失衡留白或无意义重复卡片"],
    },
)

_STATIC_CATEGORIES = (
    "schema", "upstream_status", "evidence", "pdf_jump", "caption", "formula",
    "links", "duplication", "interaction", "mobile", "accessibility", "hardcoding", "security",
)


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hidden_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.hidden_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self.hidden_depth:
            self.hidden_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self.hidden_depth:
            self.parts.append(data)


def _finding(
    check_id: str, category: str, severity: str, message: str, *evidence: object,
) -> dict[str, Any]:
    return {
        "id": check_id,
        "category": category,
        "severity": severity,
        "status": "failed",
        "message": message,
        "evidence": [str(item) for item in evidence if item is not None],
    }


def _supplemental_checks(
    ir: Mapping[str, Any],
    page_model: Mapping[str, Any],
    html: str,
    *,
    parsed_document: Mapping[str, Any] | None,
    content_plan: Mapping[str, Any] | None,
    visual_plan: Mapping[str, Any] | None,
    related_work: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """Checks intentionally kept independent from the producer validators."""

    findings: list[dict[str, Any]] = []

    # A syntactically valid claim can still be useless to a reader when a PDF
    # line fragment is presented as an excerpt or summary. Keep this gate independent from
    # the producer so regressions cannot silently pass through the same logic.
    hanging_end = re.compile(
        r"(?:-|\b(?:a|an|and|as|at|by|for|from|in|of|or|the|to|with|while|that))\s*$",
        re.IGNORECASE,
    )
    for claim in ir.get("claims", []):
        summary = re.sub(r"\s+", " ", str(claim.get("summary") or "")).strip()
        starts_like_fragment = bool(summary and summary[0].islower())
        lacks_sentence_end = not bool(re.search(r"[.!?。！？…][\]\)\"']?$", summary))
        if len(summary) < 30 or starts_like_fragment or lacks_sentence_end or hanging_end.search(summary):
            findings.append(_finding(
                f"evidence:summary:{claim.get('id', 'unknown')}", "evidence", "blocker",
                "Claim 文本必须是完整、可独立阅读的句子，不能直接暴露 PDF 断行残片。",
                claim.get("id"), summary,
            ))

    # Every numbered equation found by the parser is a high-confidence formula
    # and must survive into IR. This catches split equations whose final line
    # carries the number while the mathematical body is on preceding lines.
    if parsed_document is not None:
        modeled_formula_blocks = {
            block_id
            for formula in ir.get("formulas", [])
            for block_id in formula.get("locator", {}).get("block_ids", [])
        }
        for candidate in parsed_document.get("formula_candidates", []):
            if candidate.get("label") is None:
                continue
            candidate_blocks = {str(item) for item in candidate.get("block_ids", [])}
            if not candidate_blocks or not candidate_blocks <= modeled_formula_blocks:
                findings.append(_finding(
                    f"formula:labeled-coverage:{candidate.get('id', 'unknown')}", "formula", "blocker",
                    "带编号的论文公式未完整进入 Paper IR。",
                    candidate.get("id"), *sorted(candidate_blocks),
                ))

    for formula in ir.get("formulas", []):
        latex = re.sub(r"\s+", " ", str(formula.get("latex") or "")).strip()
        prose = re.sub(r"\\[A-Za-z]+", "", latex)
        word_count = len(re.findall(r"[A-Za-z]{3,}", prose))
        math_marks = sum(latex.count(token) for token in ("=", "+", "/", "−", r"\sum", r"\int", r"\sqrt", "≤", "≥", "→", "≻"))
        if formula.get("label") is None and word_count > 6 and math_marks < 5:
            findings.append(_finding(
                f"formula:prose:{formula.get('id', 'unknown')}", "formula", "blocker",
                "公式候选包含过多正文叙述，需重新分段或过滤。",
                formula.get("id"), latex,
            ))
    for name, artifact in (("content_plan", content_plan), ("visual_plan", visual_plan), ("related_work", related_work)):
        if artifact is not None and artifact.get("review", {}).get("status") != "passed":
            findings.append(_finding(
                f"upstream:{name}", "upstream_status", "blocker",
                f"{name} 尚未通过其上游质量门。", artifact.get("review", {}).get("status"),
            ))
    if page_model.get("preview") is True:
        findings.append(_finding("upstream:preview", "upstream_status", "blocker", "预览 Page Model 不能作为发布产物。"))

    page_count = ir.get("source", {}).get("page_count")
    for evidence_id, item in page_model.get("evidence", {}).items():
        page = item.get("page") if isinstance(item, Mapping) else None
        if isinstance(page_count, int) and (not isinstance(page, int) or page > page_count):
            findings.append(_finding(
                f"pdf:{evidence_id}", "pdf_jump", "blocker",
                "Evidence 跳转页超出 PDF 页数。", evidence_id, page, page_count,
            ))

    visible_parser = _VisibleTextParser()
    visible_parser.feed(html)
    visible = re.sub(r"\s+", " ", " ".join(visible_parser.parts)).strip()
    semantic_texts: dict[str, list[str]] = {}
    for section in page_model.get("sections", []):
        for component in section.get("components", []):
            component_id = str(component.get("id") or component.get("component") or "component")
            kind = component.get("component")
            caption = component.get("caption")
            if kind in {"figure", "table"} and caption:
                occurrences = visible.count(re.sub(r"\s+", " ", str(caption)).strip())
                if occurrences == 0:
                    findings.append(_finding(f"caption:{component_id}", "caption", "blocker", "论文正式 caption 未显示。", component_id))
                elif occurrences > 1:
                    findings.append(_finding(f"caption:{component_id}", "caption", "warning", "论文正式 caption 在主页面重复显示。", component_id, occurrences))
            if kind == "formula":
                mathml = str(component.get("mathml") or "")
                if "<math" not in html or not mathml.startswith("<math"):
                    findings.append(_finding(f"formula:{component_id}", "formula", "blocker", "公式未使用浏览器数学标记渲染。", component_id))
                if r"\sqrt" in str(component.get("latex") or "") and "<msqrt>" not in mathml:
                    findings.append(_finding(f"formula:radical:{component_id}", "formula", "blocker", "根号结构在页面模型中丢失。", component_id))
                if any("_" in str(token.get("symbol") or "") for token in component.get("tokens", [])) and "<msub" not in mathml:
                    findings.append(_finding(f"formula:subscript:{component_id}", "formula", "blocker", "公式变量下标在页面模型中丢失。", component_id))
                for token in component.get("tokens", []):
                    if token.get("variable_id"):
                        variable_id = html_module.escape(str(token.get("variable_id") or ""), quote=True)
                        if not variable_id or f'data-variable-id="{variable_id}"' not in mathml:
                            findings.append(_finding(f"formula:{component_id}:{token.get('variable_id')}", "formula", "blocker", "公式变量缺少公式内交互绑定。", component_id, token.get("variable_id")))
            for field in ("body", "caption", "description"):
                value = component.get(field)
                normalized = re.sub(r"\s+", " ", str(value or "")).strip().casefold()
                if len(normalized) >= 60:
                    semantic_texts.setdefault(normalized, []).append(component_id)
    for text, owners in semantic_texts.items():
        if len(set(owners)) > 1:
            findings.append(_finding("duplication:" + hashlib.sha256(text.encode()).hexdigest()[:12], "duplication", "warning", "相同长文本在多个组件重复展示。", *sorted(set(owners))))

    if related_work is not None:
        for node in related_work.get("nodes", []):
            node_id = str(node.get("id") or "related-node")
            url = node.get("metadata", {}).get("url")
            if not isinstance(url, str) or not re.match(r"^https?://", url):
                findings.append(_finding(f"links:{node_id}", "links", "blocker", "Related Work 节点缺少安全的论文入口。", node_id))
            if node.get("verification", {}).get("status") != "verified":
                findings.append(_finding(f"links:verification:{node_id}", "links", "warning", "Related Work 元数据尚未完全核验。", node_id))

    interaction_tokens = ("data-evidence-id", "event.key==='Enter'", "event.key===' '", "event.key==='Escape'", ".focus()")
    missing = [token for token in interaction_tokens if token not in html]
    if missing:
        findings.append(_finding("interaction:runtime", "interaction", "blocker", "键盘交互或焦点管理运行时不完整。", *missing))
    if not re.search(r'<meta\s+[^>]*name=["\']viewport["\']', html, re.IGNORECASE):
        findings.append(_finding("mobile:viewport", "mobile", "blocker", "页面缺少移动端 viewport 声明。"))
    return findings


def _add_static_pass_checks(checks: list[dict[str, Any]]) -> None:
    failed_categories = {item["category"] for item in checks if item["status"] != "passed"}
    for category in _STATIC_CATEGORIES:
        if category not in failed_categories:
            checks.append({
                "id": f"pass:{category}",
                "category": category,
                "severity": "blocker",
                "status": "passed",
                "message": f"{category} 静态质量门通过。",
                "evidence": ["独立静态检查未发现该类别问题。"],
            })


def _digest_json(value: Mapping[str, Any] | None) -> str | None:
    if value is None:
        return None
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _browser_result(value: object) -> tuple[str, str | None]:
    if value is True:
        return "passed", None
    if value is False:
        return "failed", "浏览器断言失败。"
    if isinstance(value, str):
        status = value if value in {"passed", "failed", "pending"} else "failed"
        return status, None if status != "failed" else value
    if isinstance(value, Mapping):
        status = str(value.get("status", "pending"))
        if status not in {"passed", "failed", "pending"}:
            status = "failed"
        details = value.get("details")
        return status, str(details) if details is not None else None
    return "pending", None


def _browser_checks(
    results: Mapping[str, object] | None,
    *,
    require_browser: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    supplied = results or {}
    hooks: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []
    for definition in BROWSER_HOOKS:
        status, details = _browser_result(supplied.get(definition["id"]))
        hook = {**definition, "status": status, "details": details}
        hooks.append(hook)
        if status == "passed":
            check_status, severity = "passed", "blocker"
            message = f"浏览器检查通过：{definition['description']}"
        elif status == "failed":
            check_status, severity = "failed", "blocker"
            message = f"浏览器检查失败：{definition['description']}"
        else:
            check_status = "skipped"
            severity = "blocker" if require_browser else "warning"
            message = f"尚未执行浏览器检查：{definition['description']}"
        checks.append({
            "id": definition["id"],
            "category": "browser",
            "severity": severity,
            "status": check_status,
            "message": message,
            "evidence": [details] if details else [],
        })
    return hooks, checks


def validate_review_report(report: Mapping[str, Any], project_root: Path | None = None) -> list[str]:
    """Validate a report without mutating it."""

    root = project_root or Path(__file__).resolve().parents[2]
    schema = json.loads((root / "schemas" / "review-report.schema.json").read_text(encoding="utf-8"))
    return [
        f"{'/'.join(map(str, error.path))}: {error.message}"
        for error in Draft202012Validator(schema).iter_errors(report)
    ]


def review_artifacts(
    paper_ir: Mapping[str, Any],
    page_model: Mapping[str, Any],
    html: str,
    *,
    parsed_document: Mapping[str, Any] | None = None,
    human_review_overlay: Mapping[str, Any] | None = None,
    content_plan: Mapping[str, Any] | None = None,
    visual_plan: Mapping[str, Any] | None = None,
    related_work: Mapping[str, Any] | None = None,
    browser_results: Mapping[str, object] | None = None,
    source_paths: Sequence[Path | str] = (),
    require_browser: bool = False,
    project_root: Path | None = None,
) -> dict[str, Any]:
    """Review in-memory artifacts and return a schema-valid, read-only report."""

    root = project_root or Path(__file__).resolve().parents[2]
    html_hash = _digest_text(html)
    checks = run_static_checks(
        paper_ir,
        page_model,
        html,
        content_plan=content_plan,
        visual_plan=visual_plan,
        related_work=related_work,
        parsed_document=parsed_document,
        human_review_overlay=human_review_overlay,
        source_paths=source_paths,
        project_root=root,
    )
    checks.extend(_supplemental_checks(
        paper_ir,
        page_model,
        html,
        parsed_document=parsed_document,
        content_plan=content_plan,
        visual_plan=visual_plan,
        related_work=related_work,
    ))
    _add_static_pass_checks(checks)
    bound_browser_results = browser_results
    if browser_results is not None:
        supplied_hash = str(browser_results.get("base_html_sha256") or "")
        binding_matches = supplied_hash == html_hash
        checks.append({
            "id": "browser.artifact_binding",
            "category": "browser",
            "severity": "blocker" if require_browser else "warning",
            "status": "passed" if binding_matches else "failed",
            "message": "浏览器结果已绑定当前 HTML。" if binding_matches else "浏览器结果未绑定当前 HTML 或已过期。",
            "evidence": [supplied_hash] if supplied_hash else [],
        })
        if not binding_matches:
            bound_browser_results = None
    hooks, dynamic_checks = _browser_checks(bound_browser_results, require_browser=require_browser)
    checks.extend(dynamic_checks)
    blocker_count = sum(
        item["severity"] == "blocker" and item["status"] != "passed" for item in checks
    )
    warning_count = sum(
        item["severity"] == "warning" and item["status"] != "passed" for item in checks
    )
    report = {
        "schema_version": SCHEMA_VERSION,
        "stage_version": STAGE_VERSION,
        "paper_id": str(paper_ir.get("paper", {}).get("id") or "unknown-paper"),
        "status": "failed" if blocker_count else "passed",
        "input_hashes": {
            "paper_ir": _digest_json(paper_ir),
            "page_model": _digest_json(page_model),
            "html": html_hash,
            "parsed_document": _digest_json(parsed_document),
            "human_review_overlay": _digest_json(human_review_overlay),
            "content_plan": _digest_json(content_plan),
            "visual_plan": _digest_json(visual_plan),
            "related_work": _digest_json(related_work),
        },
        "summary": {
            "blocker_count": blocker_count,
            "warning_count": warning_count,
            "passed_count": sum(item["status"] == "passed" for item in checks),
            "skipped_count": sum(item["status"] == "skipped" for item in checks),
        },
        "checks": checks,
        "browser_hooks": hooks,
    }
    errors = validate_review_report(report, root)
    if errors:
        raise ValueError("invalid review report: " + "; ".join(errors[:8]))
    return report


def write_review_report(path: Path | str, report: Mapping[str, Any], *, project_root: Path | None = None) -> Path:
    """Validate and atomically write a report; all reviewed inputs remain untouched."""

    errors = validate_review_report(report, project_root)
    if errors:
        raise ValueError("invalid review report: " + "; ".join(errors[:8]))
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, destination)
    return destination


def review_files(
    *,
    paper_ir_path: Path | str,
    page_model_path: Path | str,
    html_path: Path | str,
    output_path: Path | str,
    parsed_document_path: Path | str | None = None,
    human_review_overlay_path: Path | str | None = None,
    content_plan_path: Path | str | None = None,
    visual_plan_path: Path | str | None = None,
    related_work_path: Path | str | None = None,
    browser_results_path: Path | str | None = None,
    source_paths: Sequence[Path | str] = (),
    require_browser: bool = False,
    project_root: Path | None = None,
) -> dict[str, Any]:
    """Load artifacts, review them, and write ``review_report.json``."""

    def load(path: Path | str | None) -> Mapping[str, Any] | None:
        return json.loads(Path(path).read_text(encoding="utf-8")) if path is not None else None

    ir = load(paper_ir_path)
    model = load(page_model_path)
    if ir is None or model is None:
        raise ValueError("paper_ir_path and page_model_path are required")
    browser_results = load(browser_results_path)
    report = review_artifacts(
        ir,
        model,
        Path(html_path).read_text(encoding="utf-8"),
        parsed_document=load(parsed_document_path),
        human_review_overlay=load(human_review_overlay_path),
        content_plan=load(content_plan_path),
        visual_plan=load(visual_plan_path),
        related_work=load(related_work_path),
        browser_results=browser_results,
        source_paths=source_paths,
        require_browser=require_browser,
        project_root=project_root,
    )
    write_review_report(output_path, report, project_root=project_root)
    return report
