# Paper Visualizer Reader

把生成式 Paper Visualizer 与 PDF 原文定位集成到一个 Electron 应用中。

## 启动

```bash
cd /Users/canal/Documents/ChatGPT/zscience/projects/07-paper-visualizer/paper-reader-app
npm install
npm start
```

应用默认发现项目 `artifacts/` 中已经构建完成的论文，也可以打开任意本地 PDF 或整个论文文件夹：

- 左栏：论文目录与当前论文文章目录，可通过左上角按钮隐藏。
- 中栏：完整的生成式 Visualizer。
- 右栏：原始 PDF。

点击中栏的文字卡片、公式变量、图表节点、论文图片、表格数据或 Related Work 引文，右栏会跳转并高亮原文。已生成论文优先使用 Paper IR 中 Evidence ID 对应的精确页码与坐标；其他本地 PDF 使用页码、类型和文本匹配作为回退。

“本地解析”设置包含 Endpoint、Token 和 Model Name。Token 使用 Electron 系统安全存储加密保存；现有 Visualizer 与 PDF 阅读无需 Token，只有主动启用 LLM 表格增强时才会访问配置的 Endpoint。

## 在应用中构建新论文

1. 点击“打开 PDF”选择新论文。
2. 中栏没有对应页面时，点击右上角“构建 Visualizer”。
3. 等待解析、知识建模、内容规划、可视化、页面生成与独立审核完成。
4. 构建完成后应用会自动载入新页面，并将论文加入左侧目录。

阶段结果保存在项目的 `artifacts/<paper-id>/`，最终页面保存在 `output/<paper-id>-visualizer.html`。构建支持阶段缓存，中断后再次点击可以复用已完成阶段。

本地解析不需要网络。若需要补充识别复杂表格，点击右上角“模型已配置”，再选择“启用并重新解析”。

LLM 表格 Harness 分两阶段运行：

1. 每 6 页发送一次文本块、坐标和低清页面图，筛选疑似表格页。
2. 候选页使用高清页面图和原始文本块解析二维单元格，并与本地结果合并。

视觉表格会记录 `replaces_block_ids`，被覆盖的原句记录 `replaced_by`。界面隐藏这些原句，但 JSON 中不会删除它们，因此可以追溯和回退。解析统计保存在结果的 `llm` 字段中，包括候选页、调用次数、表格数和单页错误。

## 验证

```bash
npm test
```
