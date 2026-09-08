# 当前并行需求：Presentation/Paper 抽取与 Meta-Harness 优化

日期：2026-09-07

## 1. 当前目标

当前阶段不再拆成“先抽 Presentation、再抽 Paper”的两个串行步骤，而是先建立一个基础 **Paper Extraction Harness**，然后在每个真实 Paper–PPT/Poster pair 上并行运行两侧抽取。Presentation 侧是作者信息选择与讲解方式的弱监督；Paper 侧是最终需要持续优化的 harness。

这里的 Presentation 指作者围绕论文制作的学术 PPT 和科研 Poster。系统需要理解：

- 作者在做 PPT/Poster 时，从论文中选择了哪些内容。
- 作者如何压缩、总结和重组这些内容。
- 每个部分分别在讲什么。
- 哪些文本、公式、结论、数值、引用和章节被拿出来展示。
- 哪些图片和表格被复用、改绘或引用，并且它们对应论文 PDF 中的哪个原图、原表或原文位置。

本阶段的核心产出不是漂亮页面，而是可验证的两侧结构化抽取、对齐误差，以及一个能被 Meta-Harness 持续改进的 Paper Extraction Harness。最终推理只需要论文 PDF。

## 2. 当前并行闭环

### 2.1 Pair 内并行抽取

对每个 pair 同时启动多个独立任务：Presentation agent 按 slide/panel 提取作者选择、压缩与叙事结构；Paper agent 按 PDF 页组提取研究问题、方法、实验、结论、局限及证据。两侧不得互看，防止把答案泄漏给论文抽取器。

输入包括：

- PPT 文件、Slide PDF 或逐页图片。
- Poster PDF、Poster 图片或按 Panel 切分后的局部图片。
- 可确定性提取的原生文本、OCR 文本、坐标、字号、阅读顺序和页面截图。
- 对应论文 PDF、论文元数据，以及论文侧解析出的章节、文本块、公式、图、表和页码坐标。

Presentation 侧抽取要回答的问题是：

- 作者把论文中的哪些研究问题、动机、方法、实验、结果、结论和局限放进了 PPT/Poster。
- 作者对这些内容做了怎样的总结、压缩、改写或重组。
- 每一页 PPT 或每个 Poster 区域在整篇讲述中承担什么角色。
- 每个部分是如何讲的，例如背景铺垫、问题提出、方法解释、实验验证、结果强调、案例分析或总结展望。
- 页面上的文本、公式、数字、引用、标题和 bullet 分别来自论文的哪些位置。
- 页面上的图片和表格对应论文 PDF 中的哪个 Figure、Table、caption、页面区域或正文描述。

两侧输出随后由 Alignment Judge 比较。Presentation 结果是带噪弱监督，不默认视为 Gold；少量人工审核与确定性校验用于校准 judge。

### 2.2 Meta-Harness 持续优化 Paper Extraction Harness

Meta-Harness 读取跨 pair 的对齐错误，修改论文侧 prompt、PDF 分块、上下文召回、聚合、证据约束和重试策略。每个候选都在固定验证 pair 上重跑，并与上一版本比较；Presentation 抽取结果在比较期间冻结，避免监督目标漂移。

主要优化对象是：

```text
论文 PDF
→ 确定性预处理
→ 页组级 LLM Agent 抽取
→ Paper Evidence Atom
→ 粗粒度—章节—细节层级聚合
→ Verifier
```

Meta-Harness 优化的是模型外部流程，不修改基础模型参数。Presentation Pipeline 也可以单独版本化，但不能在同一次 paper 候选比较中随意变化。Meta-Harness 可以搜索和改进：

- Prompt 与输出格式约束。
- PDF 页组切分、章节识别与上下文组织方式。
- 粗粒度 overview、章节摘要和细粒度 evidence 的层级约束。
- 文本、公式、数字、引用、Figure/Table caption 的抽取策略。
- Paper Evidence Atom 对 Presentation Atom 的候选召回和对齐策略。
- Verifier 的检查规则、重试策略、置信度阈值和人工复核触发条件。
- 成本、时延、失败率和准确率之间的权衡。

目标是得到一个更稳定、更可迁移的 **Paper Extraction Harness**，使它在没有 PPT/Poster 的新论文上，也能输出接近真实学术展示的信息选择和从粗到细的组织。

## 3. Presentation 侧要抽取什么

### 3.1 文档级信息

每个 PPT/Poster 文档需要抽取：

- `document_id`
- `source_file`
- `presentation_type`: `ppt` / `poster`
- `paper_id`
- `title`
- `authors`
- `venue_or_event`
- `version_or_date`
- `page_count` 或 `panel_count`
- `language`
- `source_license`
- `pairing_confidence`
- `parse_confidence`
- `warnings`

这些字段用于判断文件是否确实和论文配对，以及是否适合进入后续优化。

### 3.2 PPT 页面级信息

每页 PPT 输出一个 `PresentationPageIR`。

需要抽取：

- 页面标题。
- 页面在整套 PPT 中的顺序。
- 页面角色，例如 `motivation`、`problem`、`method_overview`、`method_detail`、`experiment_setup`、`result`、`ablation`、`case_study`、`related_work`、`conclusion`。
- 一句话主旨。
- 页面上的文本块、bullet、强调句、公式、数字和引用。
- 每个文本块的坐标、字号、样式和视觉重要性。
- 页面上的图片区域、表格区域和公式区域。
- 页面和前后页面之间的承接关系。
- 页面内容与论文 Evidence 的候选对齐关系。

PPT 页面不要被当成孤立截图处理。抽取时应保留前后页标题或摘要，帮助判断当前页是在铺垫、展开、证明还是总结。

### 3.3 Poster 区域级信息

Poster 不应整张图一次性抽完，应先切分为 Panel 或区域，再对每个区域输出 `PresentationPageIR` 或 `PresentationRegionIR`。

需要抽取：

- 区域标题。
- 区域角色，例如背景、方法、实验、结果、讨论、结论、参考文献。
- 区域在 Poster 中的位置、面积和视觉权重。
- 区域主旨。
- 区域中的文本块、bullet、公式、数字和引用。
- 区域中的图片、表格和图示对象。
- 区域之间的空间关系和阅读顺序。
- 区域内容与论文 Evidence 的候选对齐关系。

Poster 的空间布局很重要。字号、面积、颜色强调和区域位置都可以作为作者选择内容优先级的弱监督信号。

## 4. 图片和表格的特殊规则

当前阶段对图片和表格采用轻量输出策略：**不要求输出图片或表格内部的完整内容，只要求把它们和论文 PDF 对应起来。**

### 4.1 图片

对于 PPT/Poster 中出现的图片，只需要输出：

- 图片在 PPT/Poster 页面或区域中的位置。
- 图片类型，例如论文原图复用、方法图改绘、结果图改绘、示意图、照片、装饰图。
- 图片是否疑似来自论文。
- 对应论文 PDF 中的 Figure ID、caption ID、页码和坐标。
- 如果是改绘图，记录候选来源 Figure、相关正文 Evidence 和置信度。
- 对齐状态：`matched` / `candidate` / `unmatched` / `external_or_decorative`。

不要求在本阶段完整解析图片内部的曲线、节点、图例或像素级内容。只有当这些内容对判断来源对齐必要时，才记录简短的可观察描述。

### 4.2 表格

对于 PPT/Poster 中出现的表格，只需要输出：

- 表格在 PPT/Poster 页面或区域中的位置。
- 表格是否疑似来自论文。
- 对应论文 PDF 中的 Table ID、caption ID、页码和坐标。
- 如果是裁剪、简化或重排后的表格，记录候选来源 Table 和置信度。
- 对齐状态：`matched` / `candidate` / `unmatched` / `external_or_decorative`。

不要求在本阶段输出完整单元格内容，也不要求重建二维表格结构。若页面文本提取器已经稳定拿到少量关键数值，可以作为文本或数字 atom 输出，但表格本体仍以“对齐到论文原表”为主。

## 5. 文本、公式和其他内容的输出规则

### 5.1 文本

文本是 Presentation 侧的核心抽取对象。

需要尽量保留：

- 页面标题。
- 栏目标题。
- bullet 原文。
- 强调句。
- 简短段落。
- 结论句。
- 数值表达。
- 引用标记。

每个文本单元都应记录：

- 原始文本。
- 坐标。
- 字号或相对视觉权重。
- 所属页面或区域。
- 页面/区域角色。
- 与论文原文的候选对齐关系。
- 是否为直接摘录、改写、综合、解释、外部内容或无法匹配。

### 5.2 公式

公式需要作为独立 atom 抽取。

需要记录：

- 页面或区域中的公式文本。
- 公式坐标。
- 公式附近解释文本。
- 公式变量或符号，如果能稳定识别。
- 对应论文 PDF 中的公式位置、定义 Evidence 或候选 Evidence。
- 对齐状态和置信度。

公式不要求在第一版做到完美二维结构解析，但必须能保留原始形态、页面位置和论文候选来源。

### 5.3 数字与结论

数字、指标和结果句需要尽量抽取，因为它们往往直接反映作者在 PPT/Poster 中强调了哪些贡献。

需要记录：

- 数字或指标原文。
- 单位、数据集、baseline、metric 等局部上下文。
- 它支持的页面 takeaway 或结论句。
- 对应论文实验、表格、图或正文分析的候选来源。

如果数字只出现在图表内部，而当前阶段不解析图表内部内容，则只记录图表与论文对象的对齐，不强行生成数字 atom。

## 6. Presentation Atom

Presentation 抽取结果最终要拆成 `PresentationAtom`，作为后续对齐和优化的基本单元。

推荐 atom 类型：

- `title`
- `section_heading`
- `bullet`
- `paragraph`
- `takeaway`
- `formula`
- `number`
- `citation`
- `figure_ref`
- `table_ref`
- `visual_region`

每个 atom 至少包含：

- `id`
- `document_id`
- `page_id` 或 `region_id`
- `atom_type`
- `text`
- `bbox`
- `style`
- `visual_weight`
- `section_role`
- `sequence_index`
- `paper_alignment_candidates`
- `alignment_status`
- `confidence`
- `warnings`

其中 `figure_ref` 和 `table_ref` 的 `text` 可以为空或只保存标题/标签，不要求保存图片、表格内部内容。

## 7. 论文侧对齐

Presentation Atom 需要对齐到论文侧的 Evidence Atom。

论文侧 Evidence Atom 包括：

- 章节标题。
- 连续正文段落或句子。
- 公式。
- Figure、caption 和图片区域。
- Table、caption 和表格区域。
- 引用语境。
- 页码和坐标。

对齐关系分为：

- `direct`: PPT/Poster 直接摘录论文文字。
- `paraphrase`: PPT/Poster 对论文内容做了语义一致的改写。
- `synthesis`: PPT/Poster 综合了论文中多个位置。
- `figure_match`: PPT/Poster 图片对应论文 Figure。
- `table_match`: PPT/Poster 表格对应论文 Table。
- `formula_match`: PPT/Poster 公式对应论文公式或定义。
- `interpretation`: PPT/Poster 加入了作者演讲式解释，论文中没有直接原句。
- `external`: 来自论文之外。
- `unmatched`: 暂时无法确认来源。

只有 `direct`、`paraphrase`、经过验证的 `synthesis`、`figure_match`、`table_match` 和 `formula_match` 可以作为主要监督信号。

`interpretation` 可以用于学习讲解风格，但不能当成论文事实监督。

`external` 和 `unmatched` 不进入事实监督，只进入错误分析或人工复核。

## 8. Meta-Harness 优化任务定义

Meta-Harness 的任务是利用冻结的 Presentation 弱监督优化 Paper Extraction Harness，而不是优化最终页面。

候选 Paper Harness 的输入：

- 论文 PDF 的确定性解析结果。
- 带页码、坐标和章节信息的文本块。
- Figure、Table、公式和 caption 候选。
- 不包含配套 PPT/Poster 内容。

候选 Paper Harness 的输出：

- `PaperContentIR`
- overview → section → detail 的层级信息。
- 可回到 PDF 页码、坐标和连续原文的 Evidence Atom。
- 与冻结 Presentation Atom 可比较的角色、主张与视觉引用。
- verifier warnings 和人工复核项。

Meta-Harness 每轮执行：

1. 选择或生成一个候选 Paper Extraction Harness。
2. 在固定训练/搜索集上运行该 harness。
3. 使用自动评分器和少量人工标注结果打分。
4. 生成改进建议。
5. 更新 prompt、工具编排、上下文选择、对齐策略或 verifier 规则。
6. 保存候选版本、运行轨迹、成本、失败样本和评分。

Meta-Harness 不得修改：

- Gold 标注。
- 固定测试集。
- 评分器定义。
- Schema 语义。
- 论文或 PPT/Poster 原始文件。

## 9. 评分指标

第一版评分重点不是页面美观，而是抽取和对齐质量。

推荐指标：

- 页面标题识别准确率。
- 页面/区域角色分类准确率。
- 文本块覆盖率。
- 文本坐标准确率。
- takeaway 是否忠实反映页面内容。
- 公式召回率和论文来源对齐准确率。
- 数字、指标和结论句召回率。
- 图片到论文 Figure 的匹配准确率。
- 表格到论文 Table 的匹配准确率。
- Presentation Atom 到 Paper Evidence Atom 的对齐准确率。
- `direct` / `paraphrase` / `synthesis` / `interpretation` / `external` / `unmatched` 分类准确率。
- 幻觉率：页面上不存在或论文无法支持的内容被写成事实的比例。
- 低置信度内容进入人工复核的召回率。
- Schema 合规率。
- 单文档成本、时延和失败率。

## 10. 数据集划分

约 200 个 PPT/Poster 样本建议分为：

- 搜索/训练集：用于 Meta-Harness 迭代候选流程。
- 验证集：用于选择候选版本和调阈值。
- 测试集：只用于最后报告，不参与 prompt、规则和阈值修改。
- 人工 Gold 子集：小规模但高质量，用于校准自动评分器。

Poster 和 PPT 应分别统计指标，因为两者的结构不同：

- Poster 更看重 panel 切分、空间层级、视觉权重和内容压缩。
- PPT 更看重页面顺序、叙事连贯性、前后页依赖和逐步展开。

## 11. 成功标准

当前并行需求的长期成功标准是：

- 系统能批量处理约 200 个 PPT/Poster，并为每个页面或区域生成结构化 IR。
- 文本、公式、数字、引用和页面角色被稳定抽取。
- 图片和表格不展开内容，但能稳定对齐到论文 PDF 中的原图、原表、caption、页码和坐标。
- 抽取结果能拆成 Presentation Atom，并和论文 Evidence Atom 建立候选或已验证对齐。
- Meta-Harness 能围绕固定评分集迭代优化 Paper Extraction Harness。
- 每轮优化都有可追踪的候选版本、评分、失败样本、成本和变更说明。
- 在不提供配套展示的新论文上，Paper Extraction Harness 相对基础版本有稳定提升。

## 12. 后续关系

完成当前并行闭环并达到固定测试集门槛后，再进入下游阶段：

```text
冻结版本化 Presentation 弱监督与稳定版 Paper Extraction Harness
→ 在未见过的论文上只输入 PDF
→ 驱动 Paper Visualizer 的内容选择、叙事规划、证据定位和交互生成
```

也就是说，当前阶段的直接目标就是用真实 Presentation 对齐信号持续优化 Paper Extraction Harness；Paper Visualizer 是下游应用。

当前已实现的小规模可运行版本及其限制见
[GLM-5.3-Flash 对齐 Harness 首轮基线](BASELINE_GLM53_FLASH_ROUND1.md)。该 smoke run 不代表
约 200 个样本的正式数据构建已经完成。
