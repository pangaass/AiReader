# Reviewer 模块

Reviewer 只读取上游产物，不修复或覆盖解析、知识、内容、视觉、Related Work 与页面输出。总入口 `review_artifacts` 返回符合 `schemas/review-report.schema.json` 的报告；`write_review_report` 只负责原子写入报告文件。

## 检查范围

- Evidence：按页、连续 block 与字符区间重建，并核对原文哈希、来源类型和 HTML registry。
- PDF 跳转：页码范围、registry 页码、远程 `#page=N` 和本地内嵌 PDF 运行时。
- 内容：caption 展示、生成式总结标签、内容重复、正文未讨论数据点的交互绑定。
- 公式：公式存在、变量双向关系、变量位于公式内部且可键盘操作。
- 页面：链接协议、自包含资源、弹卡操作、键盘事件、移动端断点、无障碍语义。
- 通用性：扫描可复用运行时代码和模板中的样例论文标识及疑似密钥字面量。
- 状态：检查解析及各计划的上游状态；预览产物不能作为发布通过结果。

## Python 调用

```python
from paper_visualizer.review import review_artifacts, write_review_report

report = review_artifacts(
    paper_ir,
    page_model,
    html,
    parsed_document=parsed_document,
    content_plan=content_plan,
    visual_plan=visual_plan,
    related_work=related_work,
    source_paths=[project_root / "paper_visualizer", project_root / "templates"],
)
write_review_report(artifact_dir / "review" / "review_report.json", report)
```

`status=failed` 表示至少存在一个未通过或未执行的 blocker；warning 不阻断。报告不含 API Key、Token 或上游文件内容之外的本机配置。浏览器检查还必须覆盖视觉保真度：公式结构、图表有效裁剪、实验内容归类，以及 Related Work 的关系依据与响应式布局。

## 浏览器复核 hook

静态评审会输出五个可供 Playwright 或人工执行的 hook：Evidence 弹卡、公式变量、PDF 页跳转、移动端布局和键盘无障碍。调用方可通过 `browser_results` 回填：

```python
browser_results = {
    "base_html_sha256": "<current HTML SHA-256>",
    "browser.mobile_layout": {
        "status": "passed",
        "details": "390x844: document.scrollWidth == innerWidth",
    }
}
```

默认未执行的 hook 记为 warning；发布流水线可传 `require_browser=True`，将未执行 hook 升级为 blocker。

## 纯常数公式例外

Paper IR 中每个公式默认必须关联至少一个变量及其已核验的论文原文定义。纯常数公式可以在 `paper_ir.review.items` 中显式豁免：

```json
{
  "kind": "formula_variable_exception",
  "formula_id": "formula:0001",
  "reason": "pure_constant",
  "rationale": "The expression contains numeric constants and operators only.",
  "status": "approved"
}
```

缺少批准状态或具体理由的例外仍记为 blocker。
