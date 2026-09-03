# Related Work 模块

输入为已通过质量门的 `paper_ir.json`，输出为 `related-work.schema.json` 定义的关系图计划。模块只从当前论文的 `citation.cited_in_evidence_ids` 建立实线引用关系；同一研究簇的虚线关系是基于当前论文引用语境的派生关系。

关键边界：

- 节点的“本文描述”只保存当前论文连续原文 Evidence ID，页面渲染时再解引用，禁止外部摘要或生成文本写入。
- 外部学术源只补充或核验题名、作者、年份、入口及关系状态；冲突会保留并进入人工审核。
- 未在正文出现的参考文献保留为 `reference_metadata_only`，不生成实线引用边。
- AMiner 客户端只从 `AMINER_API_KEY` 环境变量取令牌。缓存只保存查询哈希和响应记录，不保存请求头或令牌。
- `verify_related_work` 支持有界重试、内容寻址缓存和 `max_queries` 预算；Provider 可替换，测试使用本地假实现。

典型调用：

```python
from pathlib import Path
from paper_visualizer.related import AMinerTitleSearch, build_related_work_plan, verify_related_work

plan = build_related_work_plan(paper_ir)
verified, cost = verify_related_work(
    plan,
    AMinerTitleSearch(),
    cache_dir=Path("cache/related-metadata"),
)
```

`cost` 明确记录 Provider、单价、实际 API 调用数、缓存命中和总成本；页面不展示引用量，也不以节点大小暗示引用量。
