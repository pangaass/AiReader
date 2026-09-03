# 实施计划

## 阶段 A：基线分析与契约

1. 冻结参考页只读校验 SHA-256，提取视觉 token、组件类型与交互清单。
2. 定义 `paper-ir.schema.json`、任务信封和模块质量门。
3. 建立 Python 包、命令行入口、配置与缓存目录。

完成标准：Schema 可验证最小论文；模板目录不含 `AWM`、`2608.25618` 等论文专属值。

## 阶段 B：解析与知识层

1. 支持本地 PDF、公开 URL 和缓存命中。
2. 使用 PDF 文本块与页面坐标抽取章节、caption、Figure/Table、公式候选和参考文献。
3. 建立连续原文 Evidence，并用原文哈希阻止生成文本混入。
4. 生成 claim、formula、variable、citation 与 visual target 关系。

完成标准：每个 Evidence 可由 `page + block_ids + char_range` 重建；两篇样例均通过。

## 阶段 C：内容、视觉与 Related Work

1. 生成一分钟速读、方法、公式、实验和 Related Work 的内容计划。
2. 论文原图优先；派生流程图、比较图、关系图使用通用图元和数据驱动布局。
3. 核验引用元数据、外部入口与本文引用语境。

完成标准：没有无来源关键结论；每张派生图有 `derived_from`；所有引用节点有核验状态。

## 阶段 D：页面生成

1. 实现无论文硬编码的 Jinja 风格通用模板与组件库。
2. 内嵌 CSS、JS、MathML/SVG 和图片，输出单文件 HTML。
3. 实现 Evidence 弹卡、PDF 精确跳转、公式内变量交互、图片放大、主题、阅读进度和移动端导航。

完成标准：HTML 断网可用；弹卡只有关闭和跳转；模板无论文 ID 条件分支。

## 阶段 E：独立评审与双论文验收

1. 使用 AWM 作为视觉回归样例。
2. 使用 Attention Is All You Need（arXiv:1706.03762）作为异构样例：多公式、多表格、不同章节结构。
3. Reviewer 核对原文、页码、caption、公式、链接、重复、交互、移动端和无障碍。
4. 集成测试扫描源代码，禁止论文 ID、标题或样例文件名出现在通用模板与运行时代码。

完成标准：两篇论文由同一入口和同一模板生成；新论文测试不需要修改源码；验收报告无 blocker。

## 交付目录

- `paper_visualizer/`：模块源码与编排器
- `schemas/`：统一中间数据 Schema
- `templates/`：单文件页面模板和组件
- `config/`：非敏感配置示例
- `tests/`：单元、契约、集成和回归测试
- `examples/`：两篇论文的结构化样例与生成说明
- `output/`：最终单文件 HTML
- `reports/`：自动化验收与独立评审报告
