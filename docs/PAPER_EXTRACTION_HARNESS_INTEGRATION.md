# AiReader 论文信息抽取 Harness 集成

日期：2026-09-09

AiReader 的 Visualizer 构建现在可以在确定性 PDF 解析之后运行优化过的
`paper-alignment-candidate-v1`。该阶段只读取论文，不读取 Poster/PPT；Poster/PPT 仅在研究阶段
用于优化和评估 harness。

生产链路为：

```text
PDF → 确定性页面/文本块解析 → GLM 页组级语义抽取 → 原文 Evidence 门禁
    → Paper IR → 内容计划 → Visualizer
```

语义抽取输出保存在：

```text
artifacts/<paper-id>/extraction/paper_content.json
```

只有同时满足以下条件的总结才会进入 `paper_ir.json`：

1. 模型提供短原文引用和页码。
2. 引用能在同一页的确定性 PDF Evidence 中精确或高置信匹配。
3. 总结不是原文的伪装复制，也不是截断句。

未通过门禁的候选仍保留在 `paper_content.json`，并记录在 Paper IR 的 review 项中，但不会用于页面内容。
页组请求最多执行三次；初始并发发生断流时只顺序补跑失败页组，不重复已成功的调用。
桌面 AiReader 的“构建 Visualizer”默认启用该阶段。命令行可显式启用：

```bash
python3 -m paper_visualizer paper.pdf \
  --project-root . \
  --paper-id example \
  --extraction-harness
```

默认模型是 `glm-5.3-flash`，调用使用本机 Claude Code。可通过
`--extraction-model` 或 `PAPER_EXTRACTION_MODEL` 覆盖。基础的无模型构建仍可不加
`--extraction-harness` 运行。
