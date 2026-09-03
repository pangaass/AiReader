# 内容设计模块

`build_content_plan(paper_ir, overlay=None)` 将通过校验的 Paper IR 编排成通用 `content_plan.json`。文件契约见 `schemas/content-plan.schema.json`，文件入口为 `plan_ir_file(...)`。

## 输出结构

- 固定十一类叙事章节：一分钟速读、研究任务、已有方法、本文动机、方法总览、方法拆解、训练与推理、实验设计、主要结果、消融与分析、结论与局限。
- 来源索引仍保存在内部 Content Plan 中用于审核，但不再作为页面章节展示。
- `items` 只保存展示决策与 IR ID；Evidence 原文、caption、公式、表格值和引用元数据仍从 Paper IR 解引用。
- `narrative` 是面向阅读的解释卡片，不显示原文段落或生成标签；每张卡仍直接绑定 Evidence ID。
- Electron 构建传入 `--llm` 时，内容设计智能体会将证据改写为中文叙事，并从前两页补全可验证的作者、机构、会议和时间信息。
- `visual_requests` 向可视化模块声明用途、候选实体、派生依据、caption 与交互策略。
- `placement_ledger` 指定唯一展示位置；一分钟速读使用独立的三张摘要卡，不会挪走正式内容。
- `coverage` 与 `omissions` 明确记录缺失和回退，论文没有对应内容时不会补写。

## 安全与人工审核

- 表格数据只有 `discussed_cells` 绑定正文 `body` Evidence 时才允许证据交互；caption 或公式 Evidence 不能替代正文讨论。
- 未核验或没有入口的引用保持 `pending`，等待 Related Work 模块处理。
- 人工 overlay 必须同时绑定 Paper IR 与基础 plan 的 SHA-256，并提供 reviewer 与 reason。
- overlay 可调整章节选取/顺序以及叙事卡片的标题和正文；Evidence、来源 ID、公式、caption、表格值或引用元数据不可改。

## 示例

```python
from paper_visualizer.planning import plan_ir_file

plan_ir_file(
    "artifacts/paper/ir/paper_ir.json",
    output_path="artifacts/paper/plans/content_plan.json",
    overlay_path="artifacts/paper/review/content_plan.overlay.json",
)
```
