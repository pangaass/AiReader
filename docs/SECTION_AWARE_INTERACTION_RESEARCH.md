# 栏目感知的论文交互设计调研

日期：2026-09-03

## 1. 结论

论文可视化不应把所有栏目都渲染成卡片列表。更合适的原则是：**栏目表达论文中的关系，组件只承载证据**。因此每个栏目采用不同的视觉语法，同时保持统一的“点击即定位原文”。

| 栏目 | 主要问题 | 推荐表达 | 核心交互 |
|---|---|---|---|
| 论文信息 | 这是什么论文 | 紧凑元数据头 | 点击字段定位首页；DOI/arXiv 可外跳 |
| 一分钟速读 | 为什么读、做了什么、结果怎样 | 问题/方法/结果三格摘要 | 点击任一格定位最关键证据 |
| 研究任务 | 输入、目标、输出、约束是什么 | 任务画布 | 沿输入→目标→输出阅读；逐项定位证据 |
| 已有方法 | 本文从哪里来、与谁不同 | Connected Papers 式溯源图 + 对比表 | 筛选关系、拖拽缩放、查看引用语境、打开论文 |
| 本文动机 | 观察怎样导出缺口和主张 | 因果链 | 观察→缺口→设计要求逐步展开 |
| 方法总览 | 方法如何整体运转 | 横向方法流 | 点击步骤定位正文，并提示联动查看论文原方法图 |
| 方法拆解 | 各模块怎样衔接 | 纵向步骤流 | 顺序阅读模块；点击步骤定位对应段落 |
| 训练与推理 | 两个阶段有哪些差异 | 双泳道 | 在训练/推理语境间切换，公式就地显示 |
| 实验设计 | 如何验证主张 | 实验蓝图 | 数据/基线/指标/设置按实验单元聚合 |
| 主要结果 | 哪项主张被什么结果支持 | 结果账本 | 结论与图表相邻；点击数值或结论回到证据 |
| 消融与分析 | 为什么有效、何时失效 | 诊断矩阵 | 按组件、效率、鲁棒性、案例切换 |
| 结论与局限 | 能相信到什么边界 | 结论—边界双栏 | 区分论文结论、限制和未来方向 |

## 2. 可复用的科学文献任务

这里复用的是任务定义，不绑定论文提出的某种 RAG 或模型结构；实现仍由本项目的 Harness 调度 Worker、Verifier、Schema 和 Evidence 门禁。

| 来源任务 | 可复用能力 | 在本产品中的位置 | Harness 输出 |
|---|---|---|---|
| SciREX 文档级 IE | 抽取 Task、Method、Dataset、Metric 及跨句关系 | 任务、方法、实验、结果 | 核心实体 + 文档级关系 + Evidence ID |
| SciERC | 科学实体、关系、共指 | 合并同一方法的别名，减少重复模块 | 实体簇 + 关系边 + 原文 span |
| QASPER / PaperQA | 问题驱动的全文检索、相关段落判断、证据式回答 | “向本文提问”和栏目内追问 | answer + supporting evidence + insufficient-evidence 状态 |
| PaperQA2 / SciFact | 文献综合、矛盾发现、科学主张验证 | 结果核验、跨论文差异与争议 | claim + support/refute/uncertain + citations |
| CiteSee / Threddy | 引文语境、熟悉度、研究线程组织 | Related Work 溯源图 | citation context + relation type + thread |
| Scim | 按信息类型提示并保留原文上下文 | 速读、正文联动 | facet + salience + page/box |
| Semantic Reader / ScholarPhi 类任务 | 术语定义、符号解释、引用预览、上下文增强 | 公式变量、术语、图表解释 | target span + definition/context + evidence |
| Evidence Inference | 干预、对照、结局、效应方向及证据 | 医学论文的实验与结果 | PICO + effect direction + evidence span |

## 3. 推荐的 Harness 任务层

1. `document-structure`：解析章节、段落、图、表、公式及坐标。
2. `core-contribution`：只抽取核心 Problem、Method、Dataset、Metric，避免把 Related Work 和普通组件混入核心方法。
3. `relation-and-coreference`：合并别名，建立方法—数据—指标—结果及引用关系。
4. `section-story`：把同一栏目中的证据组织成任务图、因果链、方法流或实验蓝图。
5. `evidence-qa`：接受用户问题，先给结论，再给可点击证据；证据不足时明确拒答。
6. `claim-check`：对主要结果做支持/反驳/不确定判断，并寻找论文内部或跨论文矛盾。
7. `interaction-verifier`：验证每个可视元素都能回到页码、框选区域或图表。

每个任务都使用统一信封：输入是 Paper IR 与允许访问的论文集合；输出是版本化 JSON；Verifier 独立检查 Schema、Evidence 连续性、页码坐标与引用身份。这样可以替换底层 Harness 或模型，而不改 UI。

## 4. 优先级

- P0：栏目专属视觉语法、Related Work 溯源、方法图联动、Evidence 定位。
- P1：论文内证据问答、术语/公式解释、结果—实验条件联动。
- P2：跨论文对比、矛盾检测、个性化阅读历史和研究线程。
- 暂不做：把检索架构写死在产品中、自动把所有出现过的术语当成核心贡献、无证据的自由生成。

## 5. 依据

- [SciREX](https://www.aminer.cn/pub/5eafe7e091e01198d398670e)：文档级显著实体与 N 元关系。
- [SciERC / SciIE](https://www.aminer.cn/pub/5bbacbad17c44aecc4eb00bc)：实体、关系和共指的联合任务。
- [PaperQA](https://www.aminer.cn/pub/657a69f7939a5f4082cedac3)：全文检索、段落相关性判断和有来源回答。
- [PaperQA2](https://www.aminer.cn/pub/66f21c3c01d2a3fbfcb668ce)：真实文献检索、综合和矛盾发现任务。
- [Scim](https://www.aminer.cn/pub/627b29bb5aee126c0f0fe671)：按内容类型的论文速读提示。
- [Semantic Reader Project](https://www.aminer.cn/pub/64225b7590e50fcafde11ce1)：发现、效率、理解、综合和可访问性五类阅读挑战。
- [CiteSee](https://www.aminer.cn/pub/63ed9f3090e50fcafd0f0a2d)：带阅读历史与引用语境的论文发现。
- [Threddy](https://www.aminer.cn/pub/62f1d07190e50fcafd889f3b)：按研究线程探索和组织文献。
- [Evidence Inference 2.0](https://www.aminer.cn/pub/5eb9223291e0118cfef982c2)：PICO、效应方向与证据联合任务。
- [SciRIFF](https://www.aminer.cn/pub/666a529d01d2a3fbfc6327e1)：覆盖问答、抽取、验证等科学文献指令任务。
- 本地评测依据：`projects/02-information-extraction/reports/ZScience_科研信息抽取评测总体报告.md`、`ZScience_信息抽取评测建设方案.md`、`ZScience_会议BenchmarkTrack扩展数据集.md`。

## 6. 本分支落地范围

本分支先实现 P0：页面模型显式声明每个栏目的 `presentation`，模板按该字段选择不同布局；Related Work 继续使用可拖拽、可筛选的溯源图；方法流与原论文图并置并联动高亮。Electron 设置扩展为“阅读 + 构建模型”两组，加入主题、内容密度、默认侧栏、默认 PDF 缩放和自动打开上次论文。
