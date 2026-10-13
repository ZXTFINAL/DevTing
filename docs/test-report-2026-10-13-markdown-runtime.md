# 2026-10-13 Markdown 与角色 Runtime 浏览器核验

> 结论：隔离浏览器中，首页构想和创始人愿景文档的常见 Markdown 已正确形成 HTML 语义节点；原始 HTML 与 `javascript:` 链接未执行。团队页能选择平台、Codex CLI 和 Claude Code CLI Runtime。该记录仅覆盖本次实际检查的页面与语法，不代表全站浏览器验收或真实外部 CLI 验收通过。

## 环境

- 使用本地临时服务 `127.0.0.1:8767`，SQLite 数据位于 `/private/tmp`，模型 API key 为空。
- 通过项目创建 UI 创建合成项目“Markdown 渲染验收”，没有使用或修改现有项目数据库。
- 项目描述包含标题、段落、粗体、斜体、行内代码、嵌套无序列表、有序列表、表格、JavaScript 围栏代码、引用、原始 `<script>` 文本和 `javascript:` 链接。

## 结果

| 检查项 | 结果 | 证据与范围 |
|---|---|---|
| 首页初始构想 Markdown | 通过 | 标题、强调、行内代码、嵌套列表、表格、围栏代码和引用显示为对应 DOM 元素 |
| 创始人愿景文档正文 | 通过 | 打开《创始人构想与产品愿景》，上述 Markdown 格式仍正确渲染 |
| 群组消息正文 | 通过 | 项目创建期间同步的初始构想消息具有真实标题、列表、表格和代码块节点 |
| 原始 HTML | 通过（本样本） | `<script>` 仅显示为转义文本；消息 DOM 中 `script` 节点数为 0 |
| 危险链接 | 通过（本样本） | `javascript:` 链接仅显示标签和目标文本；DOM 中未生成危险锚点 |
| 逐角色 Runtime 设置 | 通过（界面核对） | 打开前端工程师 Runtime 配置，选项含平台 Deep Agents、Codex CLI 和 Claude Code CLI；工作区路径及提示词入口可见 |
| Runtime 状态提示 | 通过（界面核对） | 临时服务显示平台等待模型配置、Codex 命令可用、Claude Code 未检测；未将命令存在误报为已登录 |

## 边界与后续

- 本次未检查图片、裸链接、Setext 标题、波浪线代码围栏等语法的浏览器呈现，也未逐项浏览记忆、决策、会议、路线图和追溯页面。
- 没有保存角色 Runtime 选择、启动 Codex/Claude CLI 或调用模型；供应商 CLI 的登录、权限、输出兼容和代码修改仍待单独验收。
- 临时服务使用隔离数据库，现有项目数据库未被触碰。

## 自动检查

- `node --check app.js`：通过。
- `.venv/bin/python -m py_compile backend/app.py tests/test_mvp_closed_loop.py`：通过。
- `.venv/bin/python -m unittest discover -s tests -v`：3 项通过；覆盖隔离 API 闭环、假 CLI 子进程和中断会议恢复，不调用真实外部 CLI。
