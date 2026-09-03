# 页面生成模块

页面层只接收通过 Schema 校验的 `paper_ir`、`content_plan`、`visual_plan` 和可选的 `related_work`。`build_page_model(...)` 负责解引用 ID，`render_html(...)` 使用 `templates/` 中的通用组件生成单文件 HTML。

- 正文、caption、元数据都由 Jinja 自动转义；运行时不加载外部脚本、字体或样式。
- PNG/JPEG/WebP/GIF 原图转为 Data URI；SVG 不作为不可信资产直接内嵌。
- 本地 PDF 只在 HTML 中内嵌一次，由浏览器创建 Blob URL；不会写出本机绝对路径。公开 PDF 使用经过校验的 HTTP(S) URL，并追加准确的 `#page=N`。
- Evidence Registry 只包含 IR 中 `paper_verbatim` 的连续原文；弹卡只从该 Registry 读取，且只有关闭和跳转两个圆形操作。
- 公式由原始 LaTeX 的安全文本表示生成原生 MathML 容器；已建模变量在公式内部可聚焦、可点击，定义仍来自 Evidence。
- `review.status` 默认必须为 `passed`。只有显式传入 `allow_unreviewed=True` 才能生成带“预览”标记的人工审核页面。

文件入口示例：

```python
from paper_visualizer.rendering import render_files

render_files(
    "artifacts/paper/ir/paper_ir.json",
    "artifacts/paper/plans/content_plan.json",
    "artifacts/paper/plans/visual_plan.json",
    related_work_path="artifacts/paper/plans/related_work.json",
    output_path="artifacts/paper/output/index.html",
)
```
