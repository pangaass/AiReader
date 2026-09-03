# Visual Plan

可视化层读取已通过校验的 `paper_ir.json` 和 `content_plan.json`，输出由 `schemas/visual-plan.schema.json` 约束的 `visual_plan.json`。它不读取 PDF、不复制正文，也不生成 HTML。

- 原图、原表始终是主视觉；有可靠数据时才补充派生比较图。
- 派生流程图和关系图只使用 Paper IR 的显式实体与关系，每张图记录 `derived_from`。
- 布局使用 `1000 × 600` 归一化 SVG 坐标，页面生成器可按容器等比缩放。
- 节点、边和 mark 通过 `source_id`、`label_ref` 回查 Paper IR，避免内容副本。
- target 只有绑定全部来自 `body` block 的 Evidence 时才可交互；caption、formula、reference 等来源自动降级为静态。
- 表格中未列入 `discussed_cells` 的数据点保持静态，即使数值被绘制为派生柱形图。

Python API：`build_visual_plan(ir, content_plan)`；文件入口：`plan_visuals_file(ir_path, content_plan_path, output_path=...)`。验证入口为 `validate_visual_plan(...)`。
