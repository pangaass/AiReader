# Paper Visualizer 当前研究目标与数据处理流程

日期：2026-09-07

> 当前口径更新：原“两步”不再串行。每个 Paper–PPT/Poster pair 同时进行展示侧与 PDF 侧抽取，再以对齐审计驱动 Meta-Harness 优化 PDF 抽取 harness。详见
> [当前并行需求：Presentation/Paper 抽取与 Meta-Harness 优化](CURRENT_TWO_STEP_PRESENTATION_EXTRACTION_REQUIREMENT.md)。图片和表格当前不要求展开内部内容，只要求和论文 PDF 中的原图、原表、caption、页码与坐标对应上。

## 1. 当前目标

本项目不是直接训练一个 Poster 或 PPT 生成器。当前目标是利用约 200 个论文—Poster、论文—PPT 配对样本，把作者真实展示作为弱监督，构建并优化最终可独立运行的 **Paper Extraction Harness**：从论文 PDF 中抽取作者可能选择的核心信息，并按 overview → section → detail 组织，同时保留原文证据。Presentation Extraction Pipeline 与 Paper Extraction Harness 从第一轮起并行运行。

核心目标是：

1. 先建立可运行的基础 Paper Extraction Harness，并与 Presentation Extraction Pipeline 一起在 pair 内并行执行。
2. 从 Poster 中识别作者做了哪些粗粒度信息选择、栏目组织、视觉重点和压缩。
3. 从 PPT 中识别作者做了哪些细粒度讲解顺序、内容拆解、渐进展开和证据使用。
4. 对文本、公式、数字、引用、页面角色、栏目角色和讲解结构进行结构化抽取。
5. 对图片和表格只做来源对齐：匹配论文 PDF 中的 Figure/Table、caption、页码和坐标；当前不要求输出图片或表格内部完整内容。
6. 将 Presentation Atom 与论文 Evidence Atom 对齐，区分 direct、paraphrase、synthesis、interpretation、external、unmatched 等关系。
7. 使用 Meta-Harness 或 TextGrad 类 Agent 优化器，根据 Presentation–Paper 对齐误差持续优化论文侧 prompt、工具编排、上下文选择、层级结构、Verifier 和重试策略。
8. 将稳定的 Paper Extraction Harness 用于只有论文 PDF 的新样本，并最终驱动 Paper Visualizer。

整体流程为：

> Benchmark 配对 → 材料过滤 → Pair 内并行 Presentation/PDF 多 Agent 抽取 → Presentation–Paper 对齐与误差归因 → Meta-Harness 更新 Paper Extraction Harness → 固定验证集复跑 → 版本选择 → 仅 PDF 推理 → Visualizer

当前阶段是一个反复执行的并行闭环：

1. **并行抽取**：Presentation agent 逐页/逐区域抽取作者展示了什么；Paper agent 独立读取 PDF 并输出同构的粗到细信息层级。
2. **对齐审计**：独立 judge 比较两侧覆盖、层级、证据、图表与忠实度，区分展示外部内容和论文侧遗漏。
3. **Meta 优化**：Optimizer 根据跨 pair 误差更新 Paper Extraction Harness，并在固定验证集上复跑。

同一次候选比较中冻结 Presentation 侧输出，避免监督目标随 paper prompt 一起变化。只有通过固定验证集的候选才能提升为新的 paper harness 版本。

## 2. 核心研究假设

- Poster 是作者对论文进行的粗粒度重要性压缩，可以监督“应该展示什么”。
- PPT 是作者对论文进行的细粒度叙事展开，可以监督“应该怎样讲解”。
- Paper 是事实来源，所有总结、图表、关系和解释必须能够返回原文证据。
- Poster/PPT 只在数据构建、Harness 搜索和评测阶段提供监督；最终推理时只输入论文，不依赖配套 Poster/PPT。
- Poster/PPT 没有展示的论文内容不等于不重要，只能标记为未选择，不能直接作为负例。
- Poster/PPT 很少完整表达跨章节关系和交互路径，因此关系发现与动态深挖必须单独构建和评测。

## 3. 第一阶段：材料过滤

过滤分为“文档是否可信”和“内容是否被论文支持”两层，不能直接根据 Poster/PPT 删除论文内容。

### 3.1 文档级硬过滤

只有满足以下条件的材料才进入后续处理：

- 论文与 Poster/PPT 可以通过标题、作者、DOI、会议或项目标识可靠配对。
- 文件确实是该论文的科研 Poster 或学术报告 PPT，不是课程讲义、宣传材料或综述。
- Poster/PPT 与论文版本基本一致，重大内容没有因版本变化而错位。
- 文件可以正常解析，正文、图片、表格和页面结构具有可用质量。
- 来源、许可和文件用途能够记录。
- 重复文件、相同模板导出的重复版本和明显损坏文件已经去除。

### 3.2 文档级软评分

通过综合质量分保留不同强度的监督：

\[
Q=w_1Q_{pair}+w_2Q_{source}+w_3Q_{content}+w_4Q_{parse}+w_5Q_{coverage}
\]

其中分别表示配对可信度、来源可靠性、学术内容质量、解析质量和核心内容覆盖度。

- A 级：高可信，可用于训练、验证和人工 Gold 候选。
- B 级：基本可信，降低权重后用于弱监督训练。
- C 级：仅用于误差分析，不进入自动优化数据。

Poster 与 PPT 应分别评分。Poster 更重视核心内容覆盖、栏目结构和视觉层级；PPT 更重视讲解完整性、页面顺序和叙事连贯性。

## 4. 第二阶段：CLI Agent 逐页/逐区抽取

不能把整张 Poster 或整套 PPT 当作一个训练样本。建议使用具备文件读取、页面渲染、OCR、视觉理解和结构化输出能力的 CLI Agent，逐页抽取 PPT、逐区域抽取 Poster。

### 4.1 先做确定性预处理

在调用 Agent 前，先用普通程序完成：

- 将 PDF/PPT 渲染为逐页高清图片。
- 提取原生文本、字符坐标、字体大小和阅读顺序。
- 提取内嵌图片，检测图片、表格、公式和文本区域。
- Poster 使用高分辨率整图，同时生成按 Panel 切分的局部图。

原生文本和坐标必须保留，LLM 负责理解，不能让 LLM 重新生成本来可以直接提取的文字。

### 4.2 CLI Agent 的处理单元

- PPT：一个 Agent 任务处理一页，同时输入相邻页面标题，帮助判断上下文。
- Poster：一个 Agent 任务处理一个 Panel，最后再由文档级 Agent 合并整张 Poster。
- 每个页面或 Panel 独立保存输入、输出、模型版本、Prompt、成本、时延和失败日志。
- 页面任务可以并行执行；单页失败只重试该页，不重新处理整套文件。

### 4.3 每页需要抽取的内容

- 页面标题、栏目角色和一句话主旨。
- 精确文本块、Bullet、数值、公式、引用及其坐标。
- 图片类型，如论文原图复用、方法图改绘、结果图改绘、示意图、照片或装饰图。
- 图片与论文 PDF 中 Figure、caption、页码和坐标的候选对应关系。
- 表格与论文 PDF 中 Table、caption、页码和坐标的候选对应关系。
- 页面中的关键结论、强调对象及其视觉权重。
- PPT 页面与前后页面的承接关系；Poster Panel 之间的空间和语义关系。

当前阶段图片和表格不要求输出内部完整内容。只有当图片或表格内部可见文字、数字、标签对论文来源对齐有帮助时，才记录简短的可观察描述；不得把图表内容重新生成成事实说明。

### 4.4 结构化输出

每页或 Panel 形成一个 `PresentationPageIR`，至少包含：

- `document_id`、`page_id`、`region_id` 和来源文件。
- `page_role`、`title`、`takeaways` 和 `sequence_index`。
- `text_blocks`：原文、坐标、样式和重要性。
- `visuals`：类型、位置、Caption、标签、可观察描述和推断解释。
- `citations`、`numbers`、`entities` 和 `equations`。
- `relations`：文本—图片、主张—结果、页面—页面关系。
- `confidence`、`warnings` 和待复核项。

随后由文档级 Aggregator 将所有页面合并为 `PresentationDocumentIR`，恢复整套 PPT 的叙事顺序或整张 Poster 的栏目结构。

### 4.5 抽取验证与优化准备

页面抽取后必须经过独立 Verifier：

- 检查页面数量、文本覆盖率、坐标合法性和 Schema 合规性。
- 抽查文本是否与页面一致，公式和数字是否被正确抽取。
- 检查图片和表格是否只输出对齐信息，而不是展开内部内容。
- 检查图片/表格对论文 Figure/Table、caption、页码和坐标的候选匹配是否合理。
- 检查 Agent 是否把推测写成事实，或者漏掉关键结果和 Caption。
- 对低置信度页面再次抽取或进入人工复核队列。

第一轮约 200 个 PPT/Poster 的 LLM Agent 抽取结果是 seed extraction data，不直接视为完全正确的 Gold。它们需要经过自动校验和少量人工抽查，并在一次候选比较中冻结，作为 Meta-Harness 优化 Paper Extraction Harness 的搜索、验证、测试和评分依据。

### 4.6 展示原子与论文证据原子

从 `PresentationPageIR` 中继续拆出可独立对齐的展示单元：

- 标题、栏目标题和正文文本块。
- Bullet、结论句和强调语句。
- 图片、表格、公式及 Caption。
- Poster 中的位置、面积、字号和视觉强调。
- PPT 中的页码、出现顺序和前后依赖。

每个展示单元形成一个 Presentation Atom，至少包含：内容、类型、栏目、位置、视觉权重、顺序和来源文件。

论文侧则拆成 Evidence Atom：章节、段落、连续原文句子、图片、表格、公式、Caption、引用和页码坐标。

## 5. 第三阶段：Poster/PPT 与论文证据对齐

每个 Presentation Atom 在论文中寻找候选 Evidence Atom：

1. 使用标题、术语、实体、数值、数据集名和 Caption 做精确召回。
2. 使用语义检索寻找改写或压缩后的相关段落。
3. 使用图像、表格和 Caption 信息匹配复用或改绘的视觉内容。
4. 使用独立验证器判断内容是否被论文支持，并保存页码和坐标。

对齐关系分为：

- `Direct`：直接摘录或轻微压缩。
- `Paraphrase`：语义一致的改写。
- `Synthesis`：综合论文中多个位置。
- `Interpretation`：加入了讲者或制作者的解释。
- `External`：来自论文之外的信息。
- `Unmatched`：当前无法确认来源。

`Direct`、`Paraphrase` 和经过验证的 `Synthesis` 可作为主要监督；`Interpretation` 只用于学习讲解方式，不能直接作为论文事实；`External` 和 `Unmatched` 不进入事实监督。

最终不产生一份被裁剪后的论文，而是产生四类数据：

- 高置信度的展示—证据对齐数据。
- 中等置信度、等待人工抽查的数据。
- 低置信度或外部内容。
- 论文中未被本次 Poster/PPT 选择的 `unlabeled` 内容池。

## 6. 第四阶段：形成不同的监督信号

Poster 主要提供：

- 内容显著性与首页选择。
- 栏目归属与粗粒度分组。
- 关键图片、表格和结果的视觉优先级。
- 内容压缩程度和图文比例。

PPT 主要提供：

- 讲解顺序和前置知识。
- 方法步骤的拆解粒度。
- 从问题、方法到实验结论的叙事链。
- 一项主张应使用哪些图表、实验和解释支持。
- 从概览到细节的渐进展开方式。

两者合并后形成“粗粒度总览—细粒度展开”的层级监督，但保留各自来源，便于进行 Poster-only、PPT-only 和 Poster+PPT 消融实验。

### 6.1 最终论文信息抽取目标

经过优化的 Paper Extractor 在推理时只接收一篇论文，输出统一的 `PaperContentIR`：

- 研究问题、背景动机和现有缺口。
- 核心贡献、主要主张和创新点。
- 方法模块、步骤、输入输出、公式和依赖关系。
- 数据、样本、实验设置、指标、基线和消融。
- 关键结果、图表、数值、结论与适用条件。
- 局限、失败情况、不确定性和未来工作。
- 值得用于总览的关键图片、表格和公式。
- Related Work 中的重要论文、比较对象及其关系类型。
- 每个生成字段对应的连续原文、页码、坐标和置信度。

Related Work 不能只抽论文名称，还要抽取当前论文如何描述它，例如采用、比较、改进、扩展、反驳、共享任务或指出局限。由于 Poster/PPT 经常省略相关工作，这一字段主要由论文正文、引用关系和小规模人工 Gold 监督，不能把 Poster/PPT 缺失视为负例。

### 6.2 当前 Benchmark 的分工

- Paper2Poster、P2PEval：提供 Poster 级内容选择、核心覆盖和评测信号。
- DOC2PPT、SciDuet：提供逐页 PPT 内容、顺序和论文—Slide 对齐信号。
- SciPostGen、PosterSum：提供大规模但较弱的 Poster 监督。
- SciPostLayout：提供 Panel、位置、图文比例和布局信号。

不同 Benchmark 先转换到统一 IR，再进入优化过程，不能让每个数据集使用一套互不兼容的输出格式。

## 7. 第五阶段：Meta-Harness/TextGrad 类自动优化 Paper Extraction Harness

Meta-Harness 不修改基础模型参数，而是在固定模型和工具条件下优化模型外部流程。TextGrad 类方法可以作为另一种优化后端。Optimizer 可以作为 Python 框架或服务运行，直接调用 Worker、评分器和候选 Harness，不需要通过 ZCode CLI 逐轮驱动。

Optimizer 每轮只需要完成：选择一个候选 Paper Extraction Harness、在搜索集上运行、与冻结的 Presentation IR 和小规模人工 Gold 对齐评分、生成改进建议、更新候选并保存轨迹。Meta-Harness 与 TextGrad 类方法应共享统一的候选接口和评分协议，便于公平比较。

当前主要优化对象是 PDF 信息抽取 pipeline：

- `Presentation Extraction Pipeline`：输入 PPT/Poster 页面或区域，输出 `PresentationPageIR`、`PresentationDocumentIR` 和 `PresentationAtom`，作为版本化、可冻结的弱监督锚点。
- `Paper Extraction Harness`：只输入论文并预测 `PaperContentIR`。这是当前 Meta-Harness 的直接优化对象。

单个优化样本是一份论文 PDF。Paper Worker 只看到 PDF 与其确定性解析产物；评分器可以读取冻结的 Presentation IR、人工 Gold 与自动一致性结果，但不得把 Presentation 内容泄漏给 Paper Worker。

第一版不直接搜索整个 Visualizer，而是按 Paper Extraction 的模块依次优化：

1. PDF Parsing：稳定提取带页码、坐标、章节和对象标签的内容。
2. Coarse-to-fine Selection：形成 overview → section → detail 层级。
3. Formula/Number Extraction：抽取公式、数字、指标和局部上下文。
4. Figure/Table Selection：登记值得展示的 Figure/Table、caption、页码和用途。
5. Evidence Atomization：把论文内容拆成可评分、可追溯的 Evidence Atom。
6. Presentation Alignment：只在评分器中比较 Presentation Atom 与 Paper Evidence Atom。
7. Verification：检查事实、页码、Schema、图表引用、置信度和失败恢复。

每个模块都应固定输入输出 Schema、基础模型、工具范围、搜索集、验证集、测试集和自动评分器。Proposer 可以修改 Prompt、上下文选择、检索排序、工具编排、校验和重试策略，但不能修改 Gold、评分器和测试集。

推荐先优化问题、方法、结果与结论的层级选择和证据对齐，再扩展到图表、公式、数字、关系与动态深挖。

综合评分可以表示为：

\[
S=\alpha S_{text}+\beta S_{layout}+\gamma S_{alignment}+\delta S_{role}-\lambda H-\mu C
\]

其中分别衡量文本抽取、版面结构、论文对象对齐、页面/区域角色分类，并惩罚幻觉与运行成本。Poster/PPT 页面相似度只能作为其中一部分，不能取代事实、坐标和论文 PDF 对齐评分。

为获得“稳定”而不是偶然的高分结果，每个候选需要在多个学科、多个随机种子和固定验证集上重复运行，同时记录均值、方差、失败率、成本和时延。

## 8. 第六阶段：人类对齐

Meta-Harness 自动评分提高后，还需要用小规模高质量人工数据校准：

- 专家审核展示内容是否真正代表论文贡献。
- 审核总结、关系和原文证据是否一致。
- 对多个 Visualizer 结果进行成对偏好选择。
- 记录用户找到方法、实验、局限和原文证据所需时间。
- 使用阅读问答评估理解提升，而不是只评价页面美观。

人工数据主要用于验证、重排和偏好校准，不应与 Meta-Harness 的搜索集混用。

## 9. Visualizer 的目标行为

- 默认状态展示不会过度压缩的 Poster 式总览。
- 展开某个模块后，提供 PPT 式的细粒度讲解。
- 方法卡片可以打开相关实验、结果表格、原图、原文和模型分析。
- 实验卡片可以反向打开对应方法、研究主张、实验条件和局限。
- 相关工作、引言动机、方法设计、实验验证和结论之间具有可追溯关系。
- 用户点击“继续深挖”后，系统重新分析全文中与当前对象相关的位置，而不是只展开预生成文本。

## 10. 评测与消融

至少比较以下系统：

- 直接使用基础 LLM。
- 人工设计 Harness。
- Poster-only Harness。
- PPT-only Harness。
- Poster+PPT Harness。
- Poster+PPT+Meta-Harness。
- 去掉关系发现或动态深挖的版本。

主要指标包括事实正确性、核心内容覆盖、Evidence 定位精度、叙事结构、阅读问答正确率、任务完成时间、幻觉率、成本和时延。

## 11. 当前最小可行版本

当前并行需求先完成以下可扩展闭环：

1. 建立约 200 个可靠的 Paper–Poster 和 Paper–PPT 配对。
2. 用多个 LLM Agent 并行抽取 PPT/Poster 与论文 PDF，生成初始 Presentation IR 与 PaperContentIR。
3. 抽取作者从论文中选择并总结的文本、公式、数字、引用、页面角色和栏目讲法。
4. 对图片和表格只输出与论文 PDF 的 Figure/Table、caption、页码和坐标对应关系，不输出图表内部完整内容。
5. 完成 Presentation Atom 与论文 Evidence Atom 的候选对齐。
6. 建立自动校验、小规模人工 Gold、搜索集、验证集和测试集。
7. 使用 Meta-Harness 或 TextGrad 类 Agent 优化 Paper Extraction Harness，并冻结同轮 Presentation 输出。
8. 验证优化后的 paper harness 相比基础版本在固定 pair 上有稳定提升，最后用未参与搜索的测试集验证。

这一阶段的成功标准不是生成完整页面，而是证明：Presentation 弱监督能驱动 Meta-Harness 把只输入 PDF 的信息抽取改得更接近真实作者展示，并且从粗粒度到细粒度都可追溯、可复核。

## 12. 当前可运行基线

仓库已加入 `alignment_harness/`、真实 pair 下载清单、GLM-5.3-Flash 文本/图像探针、
分片多 Agent 抽取、独立对齐 judge、Meta prompt 优化、候选晋升门禁与断点续跑。
首轮两个 smoke pair 的输入限制、分数、失败样本与下一轮门禁记录在
[GLM-5.3-Flash 对齐 Harness 首轮基线](BASELINE_GLM53_FLASH_ROUND1.md)。这些结果只证明
工程闭环可运行；约 200 份数据、人工 Gold 和固定 train/validation/test 评测仍属于后续扩容。
