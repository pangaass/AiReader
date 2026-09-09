# TableTale 表格—正文叙事对齐借鉴方案

日期：2026-09-08

## 1. 目标

将 TableTale 的多粒度正文—表格链接方法吸收到 Paper Visualizer，但继续遵守本项目已有的 Evidence 门禁：只有能回到连续论文原文、合法表格结构和可验证计算的数据关系才允许交互。

最终体验不是给所有单元格增加弹窗，而是让读者沿作者的论证顺序逐层查看：

`段落结论 → 句子支持范围 → 文本 mention → 表格行/列/区域/单元格 → PDF 原文与原表`

## 2. TableTale 中值得复用的部分

### 2.1 三种链接机制

- `semantic`：方法名、数据集名、指标名，以及 “our model”“this setting”“the strongest baseline” 等指代或推断实体。
- `numeric`：正文中的原始数值、经过舍入或单位转换的数值，以及由多个单元格计算出的差值、相对提升或均值。
- `structural`：正文中的 “第一行”“最后一列”“左侧结果”等结构位置描述。

### 2.2 多粒度对齐

文本侧需要支持 `paragraph → sentence → mention`；表格侧需要支持 `table → region → row/column → cell`。当前 `discussed_cells` 只能表达最细粒度单元格链接，不能表达整表总结、整行比较、跨列区域或一对多派生数值。

### 2.3 从粗到细的渐进交互

- 段落级：提示该段引用或讨论了哪张表。
- 句子级：悬停时只强调支撑当前句子的表格范围。
- mention 级：点击句子后再显示可交互 mention；悬停具体 mention 时定位精确单元格或计算来源。
- 所有提示按需显示，避免让整张表长期处于多色高亮状态。

### 2.4 表格始终留在当前阅读上下文

当原表不在视口内时，展示一份临时镜像；当原表已可见时，直接在原表上叠加高亮。对本项目而言，中栏 Visualizer 可承担语义表格或派生比较视图，右栏 PDF 保留原表核验，因此无需机械复制 TableTale 的页面边缘布局。

## 3. 与当前实现的差距

当前 Paper IR 的表格结构包含 `columns`、`rows`、`asset_path` 和 `discussed_cells`。每个 `discussed_cell` 只有 `row`、`column` 与 `evidence_ids`。Visual Planner 会据此决定原表单元格或派生柱形图数据点能否打开 Evidence。

这个设计已经正确实现了“正文未讨论的数据点不可点击”，应当保留。但还缺少：

1. 正文中精确 mention 的字符范围。
2. mention 类型与链接机制。
3. 行、列、区域和整表级 target。
4. 一个 mention 对多个单元格的映射。
5. 派生数值的计算表达式、输入单元格和复算结果。
6. 句子级 target 聚合，以及段落级 Table Narrative。
7. 表格单元格自身的 bbox 或稳定 cell ID。

## 4. 建议的数据模型

新增 `table_narratives`，并把 `discussed_cells` 保留为兼容投影。建议概念结构如下：

```json
{
  "id": "table-narrative:0001",
  "table_id": "table:0001",
  "paragraph_evidence_id": "evidence:0012",
  "sentences": [
    {
      "id": "table-sentence:0001",
      "text_span": {"start": 85, "end": 164},
      "target_ids": ["table-target:region:0001"],
      "mentions": [
        {
          "id": "table-mention:0001",
          "text": "improves by 4.2%",
          "text_span": {"start": 121, "end": 137},
          "mention_type": "derived_value",
          "linking_mechanism": "numeric",
          "target_ids": ["table-target:cell:0003", "table-target:cell:0004"],
          "derivation": {
            "operation": "subtract",
            "input_target_ids": ["table-target:cell:0003", "table-target:cell:0004"],
            "reported_value": 4.2,
            "computed_value": 4.2,
            "tolerance": 0.05
          },
          "confidence": 0.98,
          "verification": "verified"
        }
      ]
    }
  ],
  "provenance": {
    "kind": "paper_verbatim",
    "source_ids": ["evidence:0012"],
    "verification": "verified"
  }
}
```

`table-target` 统一表示以下粒度：

- `table`：整张表。
- `row`：完整逻辑行，可跨合并单元格。
- `column`：完整逻辑列。
- `region`：连续矩形区域或多个明确子区域。
- `cell`：稳定 cell ID、行列索引、原始值、标准化值与 bbox。

`mention_type` 至少包括：

- `named_entity`
- `referential_entity`
- `inferred_entity`
- `raw_value`
- `derived_value`
- `structural_reference`

## 5. 建议的抽取与验证流水线

### 5.1 确定性预处理

1. 为表格建立稳定 cell ID，并保存行列跨度、层级表头和 cell bbox。
2. 用显式 “Table 2”“Tab. 2” 引用召回段落。
3. 合并被分页、分栏或版面切断的相邻文本块。
4. 使用 Caption、表头实体、数值和章节位置补充没有显式表号的候选段落。

### 5.2 多阶段对齐

1. `mention-detector`：在候选句中抽取可能指向表格的 mention 与字符范围。
2. `entity-resolver`：先做表头、行名、别名和共指解析。
3. `numeric-resolver`：做精确值、舍入、百分比、单位转换和容差匹配。
4. `derivation-resolver`：为提升量、差值、比值、均值等生成显式计算表达式和输入单元格。
5. `sentence-region-aggregator`：把 mention targets 合并成保留表格结构的行、列或区域。
6. `table-narrative-verifier`：验证文本 span、target 边界、数值计算、结构完整性和 Evidence 来源。

确定性规则应先于 LLM。LLM 用于指代消解、语义匹配和候选解释，不直接决定最终可交互关系。

### 5.3 Evidence 门禁

- mention 必须位于已注册的正文 Evidence 字符范围内。
- row、column、region 和 cell 必须解析到真实表格结构。
- `raw_value` 必须能在允许的舍入或单位转换规则内匹配。
- `derived_value` 必须保存输入单元格和可复算操作；复算失败时不得交互。
- `inferred_entity` 必须通过独立 verifier，低置信度结果进入人工复核。
- Caption 只能证明表格身份，不能代替正文对某个结果的讨论。
- 未通过验证的链接可以作为候选保存在审核产物中，但不能进入发布页面。

TableTale 报告 mention resolution 在复杂表格上的准确率只有 62.0%，因此本项目不能直接采用“一次 LLM 对齐后即渲染”的做法。

## 6. 推荐交互

### 6.1 Visualizer 中栏

- 结果段落旁显示低干扰的 Table cue。
- 点击 cue 后让关联原表或派生图进入 active 状态。
- 悬停句子时强调对应行、列或区域。
- 点击句子后显示 mention；悬停 mention 时强调精确 cell 或多个计算来源 cell。
- 点击表格 target 后显示连续原文 Evidence；跳转按钮继续定位右栏 PDF。
- 原表和派生柱形图共享同一组 `table-target`，避免分别维护两套证据关系。

### 6.2 PDF 右栏

- 当原表在当前页可见时，在原始 PDF 坐标上叠加轻量高亮。
- 当原表不在当前页时，中栏保留语义表格或原表镜像，右栏跳到正文 Evidence；用户需要核对原表时再跳表格页。
- 高亮只响应当前 paragraph、sentence 或 mention，不同时展示全部关系。

### 6.3 双向关系

- 文本 → 表格：回答“这句话由表中哪里支持”。
- 表格 → 文本：回答“作者在哪里解释了这个值或比较”。
- 派生数值 → 来源单元格：显示计算式，但不生成正文没有表达的新结论。

## 7. 评测

自动评测至少拆成：

- mention detection precision / recall / F1。
- target resolution accuracy，并按简单、标准、复杂表格分层。
- row、column、region 的结构匹配分数。
- derived value 的复算通过率。
- unsupported interactive link rate；该项必须接近零。
- Evidence 文本与 PDF 坐标的重建一致性。
- Table Narrative 对核心结果句的覆盖率。

用户任务分别测量：

1. 定位某个数值或模型。
2. 核验正文结论是否被表格支持。
3. 找出最佳方法、主要差异或失败条件。
4. 复述作者围绕表格建立的论证。

同时记录完成时间、正确率、NASA-TLX 和用户是否需要手动翻页。

## 8. 实施顺序

### P0：数据层和门禁

- cell ID、cell bbox 与层级表头。
- paragraph-table candidate pairs。
- mention span、target granularity 和多 cell 映射。
- raw value 与 derived value verifier。

### P1：Visualizer 渐进联动

- paragraph cue。
- sentence → row/column/region 高亮。
- mention → cell/multi-cell 高亮。
- 原表与派生图共享 target。

### P2：PDF 双向定位与深挖

- PDF 原表坐标叠加。
- 表格 target 反查所有正文讨论。
- 基于已验证 table narrative 的问答和动态深挖。

## 9. 不直接照搬的部分

- 不把显式表号引用作为唯一段落召回方式。
- 不把 GPT-4o 或任何单次 LLM 输出直接当作发布链接。
- 不给所有数值自动生成 Evidence。
- 不长期展示所有高亮，也不让高亮颜色承担复杂语义分类。
- 不把镜像表格当作原始证据；原 PDF 和稳定坐标仍是最终核验入口。
- 不把表格联动做成独立孤岛；它应进入结果账本、Claim–Evidence 图和后续问答。

## 10. 与现有结构的兼容策略

`discussed_cells` 暂时保留，作为所有 verified mention targets 的 cell 级去重投影。现有 Visual Planner 和 Reviewer 可以继续工作；新交互逐步改为读取 `table_narratives`。待原表、派生图和 PDF overlay 都完成迁移后，再决定是否废弃 `discussed_cells`。

参考论文：[TableTale: Reviving the Narrative Interplay Between Data Tables and Text in Scientific Papers](https://arxiv.org/abs/2602.22908)。
