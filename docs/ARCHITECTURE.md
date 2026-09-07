# Paper Visualizer 系统架构

## 1. 设计原则

- 输入可以是本地 PDF、公开 PDF URL 或本地路径；下载后先计算 SHA-256，后续缓存均以内容哈希为键。
- PDF 解析结果、论文知识、内容编排和 HTML 页面模型分层保存，模板不包含论文专属内容。
- 原文与总结严格分离。`evidence.verbatim_text` 只能来自同页连续文本 span；生成内容必须带 `provenance.kind=generated_summary`。
- 所有关键结论、公式、图表和引用都保留页码与 PDF 跳转链接；缺少可靠页码的项目不能进入主页面。
- 任何正文未讨论的数据点只能作为静态数值展示，不得绑定 Evidence。
- 论文原图标记为 `paper_original`；系统生成图标记为 `derived`，并记录其数据和依据。
- 每个阶段先写中间产物，再运行质量门；失败可重试、可从缓存恢复、可进入人工审核队列。

## 2. 分层数据流

1. **Ingest**：解析本地路径或 URL，保存不可变源文件、哈希、来源 URL 和 PDF 页数。
2. **Parse**：逐页抽取文本块、章节、公式、Figure、Table、caption、参考文献和坐标。
3. **Knowledge**：建立 claim、连续原文 evidence、formula、variable、citation、visual target 及其关系。
4. **Content Plan**：把知识图谱编排成速读、问题、方法、公式、实验和 Related Work 页面章节；来源索引仅保留为内部审核数据。
5. **Visual Plan**：挑选论文原图；从已验证数据生成流程图、比较图和关系图，不引入论文外事实。
6. **Related Work**：从正文引用语境抽取“研究问题来源、相关工作与差异、方法来源、数据与评测来源”四类关系；边统一由来源论文指向当前论文，类别不设数量上限，外部检索只核验元数据和链接。
7. **Page Model**：把内容计划转换为模板可消费的组件树；组件仅引用知识节点 ID。
8. **Render**：把 Page Model、资源和通用 CSS/JS 打包成单文件 HTML。
9. **Review**：执行结构校验、原文核对、页码核对、链接、重复、交互、移动端和无障碍检查。

## 3. 八个模块与质量门

| 模块 | 主要输入 | 主要输出 | 阻断条件 |
|---|---|---|---|
| PDF 解析 | PDF | `parsed_document.json`、页面图、原图资源 | 无页码、caption 与对象失配、正文为空 |
| 知识建模 | parsed document | `paper_ir.json` | Evidence 非连续原文、claim 无来源、类型关系非法 |
| 内容设计 | paper IR | `content_plan.json` | 速读无来源、章节重复、关键方法/结果缺失 |
| 可视化 | IR + content plan | `visual_plan.json`、资源 | 派生图无依据、原图来源未标记 |
| Related Work | 引用与正文 | `related_work.json` | 元数据未核验、节点无论文入口、描述非本文原文 |
| 页面生成 | 全部结构化数据 | 单文件 HTML | 外部运行时依赖、论文内容硬编码在模板 |
| Reviewer | IR + HTML + PDF | `review_report.json` | 原文、页码、caption、链接、可访问性任一关键错误 |
| 集成测试 | 两篇异构论文 | 测试与验收报告 | 任一样例需模板分支或论文 ID 特判 |

## 4. 运行目录与缓存

```text
artifacts/<paper-id>/
  source.pdf
  manifest.json
  parsed/parsed_document.json
  parsed/pages/*.png
  parsed/figures/*
  ir/paper_ir.json
  plans/content_plan.json
  plans/visual_plan.json
  plans/related_work.json
  review/human_review.json
  review/review_report.json
  output/index.html
cache/<sha256>/<stage>/<stage-version>.json
```

每个阶段产物包含 `schema_version`、`stage_version`、`input_hashes`、`created_at`、`attempt` 和 `status`。缓存命中要求输入哈希与阶段版本都一致。

## 5. 失败、重试与人工审核

- 阶段任务采用有上限的指数退避；解析器按“结构化解析器 → 本地文本/图像回退”降级。
- 质量门失败不会覆盖上一次通过的产物，而是写入新的 attempt 目录。
- `needs_human_review` 条目保存原因、候选值、PDF 页和建议操作；人工决定写入独立 overlay，不改原始解析层。
- Reviewer 是只读角色，不直接修复产物；修复请求返回对应模块重新运行。

## 6. 安全边界

- 只从环境变量读取 API Key；配置文件仅保存变量名，不保存值。
- 日志对名称包含 `KEY`、`TOKEN`、`SECRET`、`PASSWORD` 的字段统一脱敏。
- URL 下载限制为 `http/https`，设置大小、超时和重定向上限；本地路径在运行时显式解析。
- HTML 对所有文本转义；只有受控 SVG/MathML 生成器可输出标记。
