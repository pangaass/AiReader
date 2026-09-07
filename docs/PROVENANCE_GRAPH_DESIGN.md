# 论文研究溯源图设计

## 目标

- 只使用一张交互网络，避免把研究问题、方法、相关工作和数据集拆成互不连通的小图。
- 当前论文固定为右侧最终节点，所有上游论文沿有向边汇入它。
- 图表达“当前论文明确说明这篇文献如何影响自己”，不是一般性的论文相似度，也不把引用量当成影响强度。

## 图模型

- 四条来源泳道：研究问题来源、相关工作与差异、方法来源、数据与评测来源。
- 每篇来源论文先连接到所属关系汇聚点，再连接到当前论文，降低大量边直接交叉造成的视觉噪声。
- 每条语义边必须绑定一段连续的当前论文原文，且该原文同时包含引用标记和关系表达。
- 外部服务只补标题、作者、年份、DOI、入口和引用关系，不生成“本文为何引用它”的解释。
- 无法从正文证明关系的参考文献不进入主图；缺少外部链接的节点仍可显示，但标记为待核验。

## 交互

- 点击论文节点查看关系类别、引用语境和论文入口。
- 按四类来源筛选；支持画布拖动、滚轮或按钮缩放、节点拖动和一键复位。
- 当前论文始终是最右侧终点，箭头方向为“来源论文 → 当前论文”。
- 图本身承担浏览功能，不再附加论文对照表。

## 构图 Harness

1. `related-work-lead` 从 Paper IR 的正文 Evidence、引用标记和参考文献生成候选边。
2. `related-work-verifier` 运行 JSON Schema 与语义校验，检查端点方向、引用绑定、连续原文和关系类型。
3. 可选 AMiner 核验只更新书目信息；密钥不进入产物，查询结果走内容缓存。
4. Page 阶段把已验证的语义图转换成确定性泳道布局；浏览器脚本只负责交互，不重新推断语义。

可以单独重建图数据：

```bash
.venv/bin/paper-provenance-graph artifacts/<paper-id>/ir/paper_ir.json \
  --output artifacts/<paper-id>/plans/related_work.json \
  --project-root .
```

完整流水线仍使用 `paper-visualizer`，会自动执行构图、布局、渲染和复核。

## 数据获取顺序

1. 首选当前 PDF：参考文献表提供候选论文，正文引用语境决定边的类型和证据。
2. 用 DOI、arXiv ID 或规范化标题在 AMiner、OpenAlex、Crossref、Semantic Scholar 中消歧并补全元数据。
3. 需要扩图时，再获取一跳参考文献、被引论文或语义相似论文；这些节点必须标记为“外部发现”，在当前论文没有引用证据前不能伪装成来源边。
4. 数据集优先从正文的 dataset/corpus/benchmark 语境识别，再解析其被引用论文；Papers with Code 等目录只能作为核验和入口补充。

## 相关工作与取舍

- [Connected Papers](https://www.connectedpapers.com/) 适合作为交互和空间组织参考，但它面向相似性探索，中心论文不是因果或证据链终点。本实现保留其“直接操纵节点网络”的体验，改变图语义和方向。
- [S2ORC: the Semantic Scholar Open Research Corpus](https://www.aminer.cn/pub/5dc932743a55acc104249820) 提供大规模论文全文与引用语境，支持从正文而不只是参考文献表理解关系。
- [Structural Scaffolds for Citation Intent Classification](https://www.aminer.cn/pub/5ca5deb5e1cd8e2c77f98dd9) 说明引用意图分类需要结合论文结构；本实现因此使用章节和局部连续语境，而不是只匹配引用编号。
- [scite: A Smart Citation Index](https://www.aminer.cn/pub/6197f3fe5244ab9dcbd9f41a) 证明展示引用上下文和意图比单一引用计数更有解释力；本实现同样把可回到原文的语境作为边依据。
- [OpenAlex](https://www.aminer.cn/pub/627332775aee126c0f18d56c) 是可用于元数据、引用关系和开放标识符补全的外部图谱，但不会替代当前论文原文证据。
- [Semantic Scholar Academic Graph API](https://api.semanticscholar.org/api-docs/graph) 可补 references、citations 和 recommendations；推荐边应单独标为相似关系，不能混入溯源边。

## 后续扩展

- 增加“外部发现”虚线层，区分正文已证实来源与系统推荐的相似论文。
- 对同一来源论文允许多条有证据的关系边，而不是当前的单一最高优先级分类。
- 在有可靠数据集实体解析后，加入论文与数据集的二部节点，但继续要求正文 Evidence。
