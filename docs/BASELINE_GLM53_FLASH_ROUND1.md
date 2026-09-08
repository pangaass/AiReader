# GLM-5.3-Flash 对齐 Harness 首轮基线

日期：2026-09-07

## 结论

本轮已打通一个可重复运行的小规模闭环：真实 Presentation 与论文 PDF 独立抽取，
对齐裁判定位误差，Meta-Harness 修改论文侧指令，再在冻结的 Presentation 结果上复跑。
这只是 smoke baseline，不是对约 200 个 pair 的正式实验，也不能替代人工 Gold。

在 PosterSum 的一个视觉 pair 上，同一 GLM judge 给出的 paper extraction 对齐分数从
`0.68` 提升到 `0.79`（绝对提升 `0.11`）。在 SciDuet 的一个结构化 slide pair 上，
基础版本得分为 `0.82`。由于样本极少且生成与评分使用同一模型，这些数字只证明闭环
可运行，不能证明统计显著或跨数据集泛化。

## 模型与能力探针

- 调用方式：`claude --model glm-5.3-flash`。
- Claude Code：2.1.197。
- 文本 JSON 探针：通过。
- 本地图片探针：通过；模型正确识别蓝色背景、黄色三角形与文字
  `VISION_OK_735`。
- 探针报告：`artifacts/alignment_harness/probe/probe_report.json`。

## 真实样本

1. `postersum-icml2024-37030`
   - 论文：arXiv 2406.14762，21 页 PDF。
   - 展示：PosterSum test split 指向的 ICML 2024 原始 Poster PNG，2036×2880。
   - 输入形态：真实视觉 Poster；可用于当前视觉 smoke test。
2. `sciduet-test-955`
   - 论文：arXiv 1805.05361，10 页 PDF。
   - 展示：GEM/SciDuet test split 的 11 页结构化 slide 内容。
   - 输入形态：公开发布的文本/图片引用结构，不是原始 PPTX 或 slide 截图；因此本轮
     只评估内容选择与顺序，不能用于版式、坐标或视觉层级指标。

下载 URL、用途说明和本地相对路径保存在
`configs/real_pair_manifest.sample.json`；下载收据额外记录文件大小与 SHA-256。

## Harness 实现

每个 pair 的执行拓扑如下：

```text
Poster 四个重叠 panel / PPT 四页一组 ─┐
                                      ├─ 并行局部 agents ─ 聚合为 Presentation IR ─┐
PDF 确定性分页 / 最多 16 KB 一组 ─────┘                                          │
                                      └─ 并行 paper agents ─ 聚合为 Paper IR ─────┤
                                                                                  ↓
                                                                       Alignment Judge
                                                                                  ↓
                                                                     Meta prompt optimizer
                                                                                  ↓
                                                                下一版 paper-only harness
```

关键可靠性措施：

- 两侧 agent 不互看原始输入，防止将 Presentation 答案泄漏给 paper worker。
- Meta 比较期间冻结 Presentation IR，只重跑论文侧。
- PDF 先转成带页码和源文件哈希的 Markdown，再均匀保留各页内容，避免整份 PDF 读取
  超时和只保留论文前半部。
- Poster 被切成四个带重叠的 panel；SciDuet slides 每四页组成一个任务。
- Claude Code 使用 JSON Schema 约束输出；另有 JSON repair 兜底。
- 单个分片失败会单独写入 `.error.json`，其余分片仍可聚合。
- `--resume` 能复用已完成的两侧聚合结果，只重试 judge 或继续下一轮。
- 默认 `--effort low`、单 agent 360 秒上限；pair 内两侧并行、每侧最多两个局部请求，
  避免供应商侧并发排队无限放大。

## 观察到的结果

### Poster baseline（iteration 0）

- GLM judge 总分：`0.68`。
- 分项：coverage `0.62`、hierarchy `0.72`、evidence grounding `0.78`、
  visual alignment `0.66`、faithfulness `0.80`。
- 四个 Poster panel 均成功。
- PDF 的 `pages-1-5` 分片在旧的 360 秒配置下超时，其余四个页组成功并聚合；失败
  没有抹掉其他结果。
- 主要误差：论文首页缺失导致动机和贡献遗漏；图内 `SwissRoll/8 Gaussians`、
  `IVLR/ILVR`、λ 值和坐标读数存在不一致或无法核验。

### Meta-Harness 修改

Optimizer 没有加入具体论文答案，而是增加了可迁移约束：

- 图内 legend、axis、dataset 与参数标签逐字保留。
- 模糊字符串显式标 `?`，不自动“纠正”为看似合理的名称。
- 数字只有在标注清晰时才记录，并带置信度说明。
- 同一名称出现多种形式时全部保留并报告冲突。
- 所有层级都区分直接观察和解释推断。
- 明确声明没有读到的页或章节，避免把缺失伪装成不存在。

### Poster candidate（iteration 1）

- 冻结同一 Presentation IR 后，judge 总分：`0.79`。
- 五个 PDF 页组全部成功，包括 baseline 超时的 `pages-1-5`。
- 相对 baseline：`+0.11`。但这不是干净的 prompt A/B：baseline 的 `pages-1-5`
  超时，而 candidate 五个页组全部成功，因此提升同时混入了覆盖恢复效应。这只能算
  正向候选证据；必须在相同完整输入、更多 pair、独立 judge 与人工 Gold 上复测，才能
  晋升为稳定版本。

### SciDuet baseline

- 11 页 slide 内容拆成三个 presentation agent；6 页论文拆成五个 paper agent。
- 所有分片、聚合与 judge 均成功。
- GLM judge 总分：`0.82`。
- 完整耗时约 188 秒；记录成本 `$1.599107`。

Poster 的第二轮与断点续跑摘要记录成本 `$1.344095`；基线曾经过多次诊断性失败与
重试，且旧版 resume 覆盖过部分成本元数据。失败或被人工终止的 Claude 调用也没有
可靠成本回执，因此本轮不能给出完整 Poster 账单，现有数字只能用于局部比较。

## 已知限制与下一轮门禁

- 只有两个 pair，远未达到约 200 份目标；下一步应先扩到 10–20 个、完成许可与配对
  复核，再逐步扩容。
- SciDuet 当前是结构化 slide 内容，不是视觉 PPT；需要从 DOC2PPT、作者主页或明确许可
  来源补充真实 PPT/PDF deck 才能评测视觉布局。
- 当前 judge 与 worker 都是 GLM-5.3-Flash，存在自我偏好；下一轮应加入人工 Gold 和
  独立模型/规则评分。
- `overview_count_recall` 等确定性指标只检查结构与证据存在性，不代表语义正确。
- 单一 pair 上的 `0.68 → 0.79` 不足以证明稳定提升。正式晋升要求固定验证集均值提升、
  无明显 faithfulness 回退，并在未参与搜索的 test split 复现。
- Poster panel 目前按四象限切分；后续应使用版面检测或 OCR 块做语义 panel 分割。

## 复现

```bash
python3 scripts/test_glm53_flash.py
python3 scripts/download_alignment_pairs.py configs/real_pair_manifest.sample.json

# 单 pair 两轮优化；中断后可追加 --resume
python3 scripts/run_alignment_round.py configs/real_pair_manifest.sample.json \
  --pair-id postersum-icml2024-37030 \
  --output artifacts/alignment_harness/poster-run \
  --iterations 2 --timeout 360 --effort low

# SciDuet 内容/顺序 smoke test
python3 scripts/run_alignment_round.py configs/real_pair_manifest.sample.json \
  --pair-id sciduet-test-955 \
  --output artifacts/alignment_harness/sciduet-run \
  --iterations 1 --timeout 360 --effort low
```
