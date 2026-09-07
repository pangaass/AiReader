# Paper Visualizer 当前研究目标与数据处理流程

日期：2026-09-07

## 1. 当前目标

本项目不是直接训练一个 Poster 或 PPT 生成器。第一目标是利用论文—Poster、论文—PPT 配对中包含的人类科学传播经验，自动优化出一个稳定的论文信息抽取 Harness；Paper Visualizer 是该抽取结果的下游应用。

核心目标是：

1. 从 Poster 中学习粗粒度的信息选择、栏目组织、视觉重点和压缩方式。
2. 从 PPT 中学习细粒度的讲解顺序、内容拆解、渐进展开和证据使用方式。
3. 先使用可调用 PDF、图片和文件工具的 CLI Agent，逐页或逐区域抽取 Poster/PPT 的文本、图表及页面语义。
4. 将这些表达结果对齐回论文原文，形成可追溯的弱监督数据。
5. 使用 Meta-Harness 或 TextGrad 类 Agent 优化器，自动优化论文信息抽取、总结、关系发现和验证 Harness。
6. 在自动优化之后，通过人工 Gold 和用户阅读行为进一步对齐人类偏好。
7. 最终生成“海报式总览 + PPT 式细讲 + 可交互关系探索 + 动态全文深挖”的 Paper Visualizer。

整体流程为：

> Benchmark 配对 → 材料过滤 → CLI Agent 逐页/逐区抽取 → 原文证据对齐 → 冻结监督 IR → Meta-Harness/TextGrad 类优化 → Paper Extractor Harness → 人类对齐 → Visualizer

这里明确分成两个相互独立的系统：

1. **数据构建系统**：使用 CLI Agent 批量读取文件，逐页处理 Poster/PPT，生成并冻结监督 IR。
2. **Harness 优化系统**：使用 Meta-Harness、TextGrad 类方法或自定义 Optimizer，通过程序接口反复运行、评分和更新 Paper Extraction Harness，本身不依赖 CLI Agent。

第二步只消费第一步已经冻结的数据。两者不能在同一个优化循环中同时变化。

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
- 图片类型，如方法图、流程图、结果图、示意图、照片或装饰图。
- 图片 Caption、坐标轴、图例、标签和 OCR 文字。
- 图片的可观察内容与大致科学含义。
- 页面中的关键结论、强调对象及其视觉权重。
- PPT 页面与前后页面的承接关系；Poster Panel 之间的空间和语义关系。

图像字段必须区分“画面中直接观察到的内容”和“Agent 推断的解释”。无法确认的解释应降低置信度，不能写成事实。

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

### 4.5 抽取验证与冻结

页面抽取后必须经过独立 Verifier：

- 检查页面数量、文本覆盖率、坐标合法性和 Schema 合规性。
- 抽查文本是否与页面一致，图表是否被正确分类。
- 检查 Agent 是否把推测写成事实，或者漏掉关键结果和 Caption。
- 对低置信度页面再次抽取或进入人工复核队列。

应先在小规模人工标注页面上把 CLI 抽取流程调稳定，然后冻结抽取器版本，再生成 Meta-Harness 使用的监督数据。不能一边优化论文抽取 Harness，一边持续改变 Poster/PPT 目标，否则评分基准会漂移。完成冻结后，后续 Optimizer 不再调用这套 CLI Agent。

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

## 7. 第五阶段：Meta-Harness/TextGrad 类自动优化

Meta-Harness 不修改基础模型参数，而是在固定模型和工具条件下优化模型外部流程。TextGrad 类方法可以作为另一种优化后端。Optimizer 可以作为 Python 框架或服务运行，直接调用 Worker、评分器和候选 Harness，不需要通过 ZCode CLI 逐轮驱动。

Optimizer 每轮只需要完成：选择一个候选 Harness、在搜索集上运行、根据冻结监督 IR 评分、生成改进建议、更新候选并保存轨迹。Meta-Harness 与 TextGrad 类方法应共享统一的候选接口和评分协议，便于公平比较。

这里需要区分数据生成流程与待优化 Harness：

- `Presentation Extraction Pipeline`：由 CLI Agent 从 Poster/PPT 页面构建监督 IR，调试完成后冻结，不参与后续候选搜索。
- `Paper Extraction Harness`：只输入论文并预测 `PaperContentIR`，这是 Meta-Harness 主要优化对象。

单个优化样本应是一篇论文。Worker 只看到论文和允许使用的解析工具；评分器读取冻结的 Poster/PPT 监督 IR 与人工 Gold，不能把目标 Poster/PPT 内容直接放进 Worker 上下文。

第一版不直接搜索整个 Visualizer，而是按模块依次优化：

1. Salience：从论文选择值得展示的内容。
2. Evidence Alignment：为展示内容定位可靠原文证据。
3. Story Planning：组织栏目、讲解顺序和展开层级。
4. Relation Discovery：发现方法、实验、主张、相关工作和局限之间的关系。
5. Visualization Planning：选择卡片、流程图、对比表、原图等表达方式。
6. Dynamic Deep Dive：根据用户兴趣重新检索全文并生成局部解释。
7. Verification：检查事实、数值、引用、证据和输出格式。

每个模块都应固定输入输出 Schema、基础模型、工具范围、搜索集、验证集、测试集和自动评分器。Proposer 可以修改 Prompt、上下文选择、检索排序、工具编排、校验和重试策略，但不能修改 Gold、评分器和测试集。

推荐先优化 Salience 与 Evidence Alignment，再扩展到 Story Planning 和 Relation Discovery，最后进行端到端 Visualizer 优化。

综合评分可以表示为：

\[
S=\alpha S_{salience}+\beta S_{evidence}+\gamma S_{structure}+\delta S_{related}-\lambda H-\mu C
\]

其中分别衡量核心内容选择、证据准确性、结构完整性、相关工作关系抽取，并惩罚幻觉与运行成本。Poster/PPT 相似度只能作为其中一部分，不能取代事实和证据评分。

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

第一阶段只完成以下闭环：

1. 建立一批可靠的 Paper–Poster 和 Paper–PPT 配对。
2. 实现 CLI Agent 的逐页/逐区抽取，生成 `PresentationPageIR`。
3. 在人工标注页面上验证并冻结 Presentation Extraction Pipeline。
4. 完成展示内容与论文 Evidence 的对齐，生成固定监督数据。
5. 建立小规模人工 Gold，并划分搜索集、验证集和测试集。
6. 构建只输入论文的 Paper Extraction Baseline Harness。
7. 使用 Meta-Harness 或 TextGrad 类 Agent 优化 Salience、Evidence 和 Related Work 抽取。
8. 验证有效后，再加入叙事、关系、可视化和动态深挖。

这一阶段的成功标准不是生成完整页面，而是证明：在推理时不提供 Poster/PPT 的情况下，利用这些人类表达信号优化出的 Harness，能够稳定提升论文核心内容、相关工作和原文证据的抽取质量。
