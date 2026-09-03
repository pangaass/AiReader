# 多智能体协议

## 1. 分层组织

- **Orchestrator** 只负责依赖图、状态、重试、缓存和质量门，不修改模块产物内容。
- 八个 **Module Lead** 分别拥有 PDF 解析、知识建模、内容设计、可视化、Related Work、页面生成、Reviewer、集成测试模块。
- 每个 Module Lead 至少调用一个 **Worker/Verifier**。Worker 负责执行或候选生成，Verifier 负责独立复核；Lead 合并后对模块产物签字。
- Reviewer 与集成测试负责人不能复用页面生成负责人的未审结论。

## 2. 统一任务信封

任务输入：

- `run_id`、`paper_id`、`module`、`task_id`
- `input_artifacts`：路径、SHA-256、Schema 版本
- `acceptance_checks`：机器可判定的完成条件
- `constraints`：不可修改目录、网络策略、环境变量白名单
- `attempt`、`deadline_seconds`

任务输出：

- `status`: `passed | needs_review | retryable_error | fatal_error`
- `output_artifacts`：路径、哈希、Schema 版本
- `checks`：每项验收的 `pass/fail` 与证据
- `warnings`、`review_items`、`retry_hint`
- `agent_role`、`reviewed_by`

## 3. 证据纪律

- Worker 提交 Evidence 时必须同时提交页码、块 ID、字符区间和原文哈希。
- Lead 必须重新从解析层切片并比对哈希，不能相信生成文本本身。
- 生成式 claim summary 必须引用至少一个 claim/evidence ID，并设置 `display_label=生成式总结`。
- Related Work 节点的“本文如何描述”只能引用当前论文连续原文；外部学术源只用于核验元数据和入口。
- 派生图只能读取 Schema 内已验证字段，并列出 `derived_from`。

## 4. 状态机

`queued → running → validating → passed`

异常路径：

- `running → retryable_error → queued`：临时网络、解析器超时、可恢复格式问题。
- `validating → needs_review`：页码歧义、caption 归属不明、元数据冲突。
- `running/validating → fatal_error`：加密 PDF、输入损坏、Schema 不兼容且无迁移器。

下游模块只消费 `passed` 产物；人工 overlay 通过后可把 `needs_review` 提升为 `passed`。

## 5. 模块交接检查

- Parse → Knowledge：页面文本、坐标、caption、对象 ID 稳定。
- Knowledge → Content：关键 claim 均有 Evidence；原文与总结分离。
- Content → Visual：每个视觉请求给出目的、候选数据和来源。
- Related Work → Page：题目、作者、年份、链接状态和本文原文描述已核验。
- Page → Review：单文件可离线打开，所有交互目标可键盘操作。
- Review → Integration：没有 blocker；warning 有明确豁免或人工确认。

