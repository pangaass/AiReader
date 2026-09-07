# Paper Visualizer

通用、证据可追溯的多智能体论文可视化系统。输入本地 PDF 或公开 PDF URL，输出由统一 Paper IR 驱动的自包含交互式 HTML；模板与运行时代码不包含 AWM 或其他样例论文特判。

## 快速开始

要求 Python 3.11+：

```bash
cd /Users/canal/Documents/ChatGPT/zscience/projects/07-paper-visualizer
python3 -m venv .venv
.venv/bin/pip install -e '.[enhanced,test]'
.venv/bin/paper-visualizer /path/to/paper.pdf --project-root .
```

公开 URL 也可直接作为输入。默认输出到 `output/<paper-id>-visualizer.html`，全部中间产物保存在 `artifacts/<paper-id>/`。

默认命令会在解析存在非阻断歧义时继续生成带“预览”标记的页面，并把待审核项写入 `artifacts/<paper-id>/review/`；它不会把预览冒充发布版本。若要求缺少人工 overlay 时立即停止，增加 `--require-reviewed`。

## Visualizer + PDF 原文对齐阅读器

Electron 应用位于 `paper-reader-app/`，提供“论文目录 / 生成式 Visualizer / 原始 PDF”三栏界面，左栏可隐藏。Visualizer 的卡片、公式变量、图表、图片、表格数据与 Related Work 引文均会连接 Evidence ID；点击后，右侧 PDF 会跳转到对应页并按 IR 中的原始坐标高亮。没有 Evidence ID 的论文图片和表格会按页码与内容类型回退定位。

```bash
cd paper-reader-app
npm install
npm start
```

应用会自动发现 `artifacts/*/manifest.json` 中已经构建完成的论文，也可通过“打开文件夹”批量加入 PDF。若 PDF 没有配套 Visualizer，中栏显示提示，右栏原文与左侧文章目录仍可正常使用。

打开尚未生成页面的 PDF 后，可以直接点击中栏“构建 Visualizer”，启动完整 Agent 流水线并查看阶段进度；完成后页面会自动载入，无需再运行终端命令。

本地解析无需网络；右上角“设置”可调整主题、密度、默认侧栏、PDF 缩放和恢复上次论文，也可配置 Harness 使用的 Endpoint、Token 与模型。Token 只保存在系统安全存储中。

发布前建议启用完整门禁：

```bash
.venv/bin/paper-visualizer /path/to/paper.pdf \
  --project-root . \
  --paper-id my-paper \
  --require-browser-review
```

首次浏览器复核前，先按 `paper_visualizer/review/README.md` 的格式写入 `artifacts/<paper-id>/review/browser_results.json`。如需 AMiner 核验 Related Work，设置环境变量 `AMINER_API_KEY`，再增加 `--verify-related-work`；任何 Token 或 API Key 都不会写入代码、日志或产物。

## 架构与数据

- 流水线：`parse → model → content → visual → related → page → render → review`；`related` 阶段会生成有向研究溯源图。
- 每阶段通过统一 `TaskEnvelope` / `TaskResult` 协议执行 Lead → Worker → Verifier，并支持内容哈希缓存、有限重试、失败 attempt 保存与人工审核 overlay。
- 默认 backend 是可复现的进程内 worker/verifier；`SubprocessExecutionBackend` 提供 JSON-over-stdio 的独立进程或外部 agent 接口，并要求 verifier 返回身份与检查证据。这里的“进程内 agent”是职责隔离，不宣称独立 LLM 会话。
- 统一 Schema 位于 `schemas/`；实现位于 `paper_visualizer/`；通用模板位于 `templates/`。
- Evidence 仅保存可由页码、block 和字符区间重建的连续原文；生成内容统一标记“生成式总结”。
- 论文原图标记为 `paper_original`，派生图标记为 `derived`；公式变量可直接打开其定义 Evidence。

详细设计：

- [参考实现分析](docs/REFERENCE_ANALYSIS.md)
- [系统架构](docs/ARCHITECTURE.md)
- [Agent 协议](docs/AGENT_PROTOCOL.md)
- [实施计划](docs/IMPLEMENTATION_PLAN.md)
- [产品要求](docs/PRODUCT_REQUIREMENTS_V1.md)
- [论文研究溯源图设计](docs/PROVENANCE_GRAPH_DESIGN.md)
- [栏目感知交互与文献任务调研](docs/SECTION_AWARE_INTERACTION_RESEARCH.md)
- [海报与 PPT Benchmark、可行性和设计建议](docs/POSTER_PPT_REFERENCE_AND_BENCHMARK.md)
- [当前研究目标与数据处理流程](docs/CURRENT_RESEARCH_GOAL_AND_DATA_PIPELINE.md)
- [当前状态与新窗口交接](docs/CURRENT_STATUS_2026-09-04.md)

## 验证与示例

```bash
.venv/bin/pytest
```

公式使用结构化 MathML，变量上下标可独立交互；Related Work 现在渲染为一张由四类来源汇聚到当前论文终点的交互网络，语义边均可回到当前论文的连续原文。正式流水线复跑时，各阶段支持内容哈希缓存：

- [AWM 生成页](output/awm-generated-visualizer.html)
- [Attention Is All You Need 生成页](output/attention-is-all-you-need-visualizer.html)
- [验收报告](reports/INTEGRATION_ACCEPTANCE.md)
- [机器可读验收结果](reports/INTEGRATION_REVIEW.json)

## 冻结基准

以下文件只读，未被本系统覆盖：

- `reference/awm-paper-visualizer-v1.html`：`1a1734c96a429b2f591fce69791659de8349bcd730923cef8538764ac10aa3c7`
- `output/awm-paper-visualizer.html`：`eca4d847769b3425386059104b8b449d4945507e98963f42b218b17a6e7f837c`
