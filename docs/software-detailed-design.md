# 软件详细设计

> 版本：v2.8 · 状态：根据当前 MVP 源码整理，待评审  
> 日期：2026-10-13

## 1. 模块和文件

| 模块 | 位置 | 职责 |
|---|---|---|
| Web UI | `index.html`, `app.js`, `styles.css` | 页面渲染、交互、项目状态本地缓存、API 调用；聊天、文档、需求、记忆、决策、会议和追溯正文共用安全 Markdown 渲染 |
| 服务与业务接口 | `backend/app.py` | FastAPI 路由、项目状态、Agent 调用、需求门禁、会议和文档历史 |
| 依赖清单 | `requirements.txt` | Python 运行依赖 |
| 项目事实源 | `.data/zhidian.sqlite3` | 项目状态、项目元数据、事件和文档快照 |
| 产品/工程说明 | `docs/*.md` | 可审阅基线与变更说明 |

## 2. 主要状态结构

项目状态是 JSON 对象，关键字段包括：

```json
{
  "projectId": "项目标识",
  "projectName": "名称",
  "projectDescription": "初始描述",
  "productShape": "产品形态",
  "mode": "敏捷迭代 / 瀑布式 / 混合",
  "goal": "目标或待确认",
  "projectStage": "当前阶段",
  "requirements": [], "facts": [], "requirementPlans": {}, "tasks": [], "docs": [], "meetings": [],
  "chats": {"group": [], "pdt": [], "pm": [], "se": [], "pe": [], "ui": [], "fe": [], "be": [], "data": [], "algo": [], "qa": [], "ops": []},
  "projectDecisions": [], "pendingServerActions": [], "memberCounts": {}, "rolePrompts": {}, "reportingLines": {},
  "cadence": 5, "meetingEnabled": false, "meetingAgenda": [], "meetingLive": false,
  "maxParallelTasks": 3,
  "tasks": [{"parallelAllowed": true}]
}
```

每条会议记录含 `id`、`topic`、`agenda`、`transcript`、`attendees`、`pmSummary`、`decisionRequests`、`deliveryOrder`、`actionItems`、运行状态与错误。行动建议含 `id`、标题、角色、需求 ID、验收标准、来源、确认状态和已关联 `taskId`；服务端建立任务前再次验证需求状态和字段，不依赖前端 `eligible` 标记。

项目总览的任务数和进度直接由当前项目 `tasks[]` 计算：待评审与进行中都代表有交付项；角色数按这两种状态中的负责人去重。阻塞任务在侧栏状态中优先提示，其次显示待评审、进行中、待开始或全部完成。服务连接提示单独显示，不能以“没有进行中任务”推断项目尚未启动；评审任务不计入已完成比例。

需求记录含草稿 `id`、批准后的 `draftId`、`draftVersion`、正式 `baselineVersion`、标题/问题、合理性、`kano`、`confidence`、优先级、状态、负责人、分类理由、价值假设、证据来源等级、证据缺口、裁决历史和补充分析历史。确认动作将 DRAFT 迁移为 REQ v1；拒绝、修改后再议、挂起或补充分析不会生成交付任务。项目决策建议由 `projectDecisions[]` 保存提议内容、角色、关联需求、状态、`version`、审阅人/时间/理由和 `history[]`；待审阅条目可通过人工审阅 API 裁决，重复同一裁决幂等，冲突裁决返回 409。采纳不修改需求状态，也不创建任务。任务含 `id`、关联需求 `req`、负责人角色、状态、期限、验收条件、状态历史、验收结果和阻塞说明。文档包含 `id`、名称、类型、负责人、版本、状态、正文 `content`、章节、进度及 `lastChange`。字段目前没有数据库级 schema migration / 强类型校验覆盖所有 JSON 内容。

`facts` 中每条记忆最多保存 4,000 字符正文和 1,200 字符来源；Agent 上下文最多带最近 30 条非退役记忆，并截取正文 1,200 字符、来源 400 字符，控制长期记忆占用。每条记忆另存 `evidenceLevel`、AI `confidence`、`reviewedBy`、单调递增 `version`、不超过 200 字的 `changeSummary` 和 `history[]`。旧数据在服务启动时补迁移基线，缺少的来源等级按 E0 表示“未提供来源证据”，不推断旧内容的实际证据强度。`PUT /api/state` 会保留服务端事实库、需求候选/正式编号与裁决历史、已存在文档、自动拆解任务以及服务端已有的团队/角色 Runtime 配置，避免旧浏览器提交整份状态时覆盖这些服务端生成或专用接口管理的数据；新建文档仍可由整体状态提交追加。服务端当前已存在需求时以其完整列表为准，防止旧 DRAFT 快照覆盖正式 REQ 编号。

## 3. HTTP 接口

群组和角色会话、项目总览会议摘要/方向/必读摘要、文档卡片描述/正文/版本预览、需求列表与分析、项目记忆、决策建议、会议纪要/角色发言、路线图节点详情、追溯内容、验收/阻塞说明和事件摘要均调用前端同一 Markdown 子集渲染器：ATX / Setext 标题、段落、嵌套无序/有序列表、任务复选框、引用、分隔线、表格、反引号/波浪线围栏代码、行内代码、强调、删除线、链接、自动链接和图片；对象/数组消息在渲染前归一化为文本或 JSON 围栏。渲染器先转义原始 HTML，链接只允许 `http`、`https` 和 `mailto` 协议；图片仅允许 `http` / `https`。代码块和宽表格可横向滚动。不加载远程解析器，不执行输入 HTML。表单保持原始 Markdown 文本，方便后续编辑和导出。

| 方法和路径 | 用途 | 关键约束 |
|---|---|---|
| `GET /api/health` | 运行与 Agent 配置状态 | 不返回密钥 |
| `GET /api/projects` | 项目列表 | 返回项目元数据及需求/任务数量 |
| `POST /api/projects` | 新建项目 | 创建初始空需求、空任务、聊天空间和文档骨架 |
| `GET /api/projects/{project_id}/export` | 导出完整项目 JSON | 包含元数据、当前状态、所有审计事件与文档快照；不导出环境变量；当前不可导入恢复 |
| `DELETE /api/projects/{project_id}` | 删除非默认项目 | 必须提交完全匹配的项目名称；同时删除状态、会话、事件和文档版本；默认项目返回 409 |
| `GET /api/state?project_id=` | 读取项目状态 | 按项目 ID 隔离 |
| `GET /api/requirements/{id}/trace` | 查询需求追溯链 | 返回裁决、关联决策、显式关联记忆、任务、文档命中项及分析历史；无显式关联时不推断 |
| `GET /api/facts/{id}/trace` | 查询记忆的引用追溯 | 返回记忆的直接需求/任务/文档关系及显式引用它的需求、决策、任务、文档、会议；聊天不作为证据索引 |
| `PUT /api/state?project_id=` | 保存完整项目状态 | 服务端保留权威需求、事实、决策建议、会议、团队/角色 Runtime 配置、全部任务和已有文档；未覆盖的一般字段仍需考虑并发编辑 |
| `POST /api/chat` | 向角色或群组发消息 | 发送给所选 Agent；单聊结果也发简短群同步 |
| `POST /api/requirements/{id}/approve` | 用户批准需求 | 要求裁决理由；DRAFT 转 REQ 并建立 v1 基线；记录历史和 PM 拆解队列项 |
| `POST /api/requirements/{id}/revise` | 修改候选需求后再议 | 待裁决/挂起状态可操作；action ID 幂等；保留前后快照并重新入裁决队列；不建任务 |
| `POST /api/requirements/{id}/plan/retry` | 重试失败的 PM 拆解 | 仅已批准需求且没有排队/运行作业时可重试 |
| `POST /api/facts` | 记录用户提供的项目记忆 | 要求来源及 E0–E4 建议等级；可选关联需求/任务/文档；相同标题、内容、来源和关联对象去重 |
| `PATCH /api/facts/{id}` | 审阅 AI 记忆 | AI 条目可标记“用户确认”或“已取代”；记录审阅人、200 字内摘要与新版本历史 |
| `POST /api/requirements/{id}/decision` | 人工拒绝或挂起需求 | `decision` 为 `已拒绝`、`已搁置` 或 `挂起待证据`；理由必填；待裁决/挂起状态可操作；不创建任务 |
| `POST /api/requirements/{id}/clarification` | 退回补充需求分析 | 待裁决/挂起状态可用；要求补充说明；回到原需求并通知 PE |
| `POST /api/decisions/{id}/review` | 人工审阅 AI 项目决策建议 | 要求 3–2000 字理由；采纳/拒绝/搁置写入版本历史、群消息、事件和决策台账；不修改需求基线、不建任务 |
| `POST /api/tasks` | 创建关联任务 | 仅允许已确认需求；验收标准必填；记录需求追溯和交付文档 |
| `PATCH /api/tasks/{id}` | 推进任务状态 | 按迁移表校验；完成/阻塞需说明；同步群消息、事件和文档版本 |
| `PATCH /api/tasks/{id}/parallelism` | 配置单任务串/并行策略 | 正在并行的任务不能直接切换为串行；变更同步群组、文档与事件 |
| `PUT /api/roadmap/settings` | 保存项目最大并行数 | 1–32；不能低于当前进行中任务数；事件与项目群可见 |
| `PUT /api/team/reporting-lines` | 更新项目汇报树 | 所有角色都要有上级；拒绝自引用/循环；关系终止于 PDT |
| `GET /api/documents/{id}/versions` | 查看文档快照 | 返回该文档历史 |
| `POST /api/documents/{id}/restore` | 恢复版本 | 从旧快照复制并保存为新版本 |
| `PUT /api/meetings/config` | 配置会议开关与节奏 | 会议节奏最小/最大范围由 API schema 限制 |
| `POST /api/meetings/run` | 运行会议 | 接收主题和议程；需要配置模型和 Agent 依赖 |
| `POST /api/meetings/{meeting_id}/actions/{action_id}/task` | 确认会议行动项并建任务 | 只允许已确认需求、有效交付角色和验收标准；幂等 |
| `GET /api/events` | 查询项目事件 | 按项目筛选，可限制条数；返回事件 ID 供 SSE 历史合并 |
| `GET /api/events/stream` | SSE 订阅项目事件 | 按项目 ID 流式推送新事件；重连携带 `Last-Event-ID` 后从该事件继续补发 |
| `PUT /api/team/config` | 保存角色人数、专属提示词和角色 Runtime | 角色 ID 必须有效；人数为 1–12；提示词最多 4000 字；Runtime 为平台/Codex/Claude Code；外部 Runtime 必须提供可访问的绝对工作区目录 |

## 4. Agent 与工具边界

路线图由当前项目结构化状态派生：PDT 阶段为固定流程定义；需求节点来自 `requirements[]`；任务按负责人角色归入 PM、设计、开发、测试或运维阶段；文档节点读取 `docs[]`。不另存一份可能与需求/任务清单不一致的静态路线图。单个任务保存 `parallelAllowed`，项目状态保存 `maxParallelTasks`。任务通过 UI 或 Agent 从待开始进入进行中时，服务端统计当前执行项并校验上限；串行任务与任何并行执行互斥。

Agent 调用使用 Deep Agents/LangGraph 的异步图状态迭代，在每个已完成的图步骤写一条不含输出正文的 `agent.progress` 事件；任务工具和项目 API 另写领域事件。SSE 仅传项目事件，界面点击节点后按节点 ID 筛选历史和新事件。此处是图步骤级进度，不是 token 级生成；事件保存在 SQLite，UI 首次载入最近 40 条并以事件 ID 合并 SSE 实时项，EventSource 断线重连时由 `Last-Event-ID` 恢复事件游标。待用户裁决弹窗是导航提示，裁决仍由人工 API 完成。切换项目时清空并重新载入该项目事件，避免跨项目串流。

- `role_prompt` 为 PDT 与其他角色提供职责、证据区分、汇报和文档操作约束；可由项目 `rolePrompts` 覆盖。
- `build_agent` 读取角色人数和提示词构建配置；缓存键覆盖项目、角色、模型及配置摘要，以便团队配置变化触发重建。
- 继承的默认项目含原型期历史记录时，Agent 主角色和子角色提示词均附带来源核验约束；`read_project_context` 同时返回数据质量提示。新建项目从空白需求/任务空间初始化。
- PDT 为主 Agent；专业子 Agent 各自有角色描述。工具集合按角色受限。
- 核心工具：`read_project_context`、`record_project_fact`、`search_project_facts`、`read_project_document`、`create_requirement`、`update_requirement_analysis`、`create_development_task`、`update_development_task`、`update_project_document`、`propose_user_decision`、`send_project_update`。
- 项目事实库记录 `kind/title/content/source/status/confidence/evidenceLevel/version/changeSummary/reviewedBy/history` 与可选关联对象。人工直接录入保存为“用户提供”；Agent 只能创建“待核实”的假设/风险，不能自行写入事实、证据或决定。用户确认候选后角色上下文可读；标为“已取代”的记录保留在审计/导出中，但不返回为当前记忆。E0–E4 仅是项目建议的来源分级，不是通用标准、概率或真伪判定。
- 项目记忆页生成动态必读摘要：从确认范围、待裁决事项、任务进度、待决策、风险和文档目录拼装，并允许导出 Markdown。需求追溯路由只返回显式链接或正文命中编号的项目对象；记忆追溯路由提供直接关系、结构化 `factIds` 引用、正文中编号命中及单独标识的同需求间接关系，不扫描聊天/逐字发言以免把会话误当事实来源。
- `create_requirement` 总是新建“分析中”或“待你裁决”记录，不会批准。
- `update_requirement_analysis` 只允许 PDT/PE 修改分析中或待裁决的原需求，并将其重新提交人工裁决。
- `create_development_task` 检查需求状态必须是“已确认”。
- `update_development_task` 由任务负责人执行；PDT/PM/SE 可管理任意任务；状态变更需满足流程，已完成必须已有验收标准和验收说明。
- 用户裁决由人工 API 写入 `humanDecision`、`humanDecisionAt`、`decisionNote` 和 `decisionHistory[]`。批准时 DRAFT 转为 REQ 并写入 `draftId` / `baselineVersion=1`，随后创建 PM 拆解任务与 `requirementPlans[requirement_id]` 队列记录；拒绝、搁置、挂起及修改后再议均不建任务，修改保留前后快照并增加 `draftVersion`。
- 项目工作台在服务端不可用时可暂存裁决与修订 action；action ID 使修订重放幂等。离线确认仅显示为待同步，不本地创建正式编号或 PM 任务。
- 更新文档时，Agent 工具写完整正文（最多 24,000 字符）、变更摘要、状态和版本，然后保存快照/事件。
- 模型调用失败返回 API 错误；未配置模型时，消息保存但返回 runtime 未启用提示。

## 5. 文档快照逻辑

`ProjectStateStore.save(state, project_id, actor)` 读取先前状态，与新的 `docs` 按文档 ID 比较；新增或变化的文档会将完整文档对象序列化到 `document_versions`。其他项目状态不变时不会生成文档快照。启用快照支持时，旧项目已有文档会创建迁移基线。恢复操作复制快照内容，生成新版本号、恢复摘要并再次保存。

局限：当前比较的是完整字典相等；没有字段级差异、文档内锚点引用、跨进程并发乐观锁和保留清理策略。通用 `/api/state` 仍保存大部分前端项目状态；多人并行写入时仍需要冲突版本机制。

## 6. 浏览器离线与裁决补同步

服务端暂时不可达时，浏览器仍可在本地执行需求裁决与字段修订。确认/拒绝/搁置/挂起以 `requirement_decision`、修订以带 action ID 的 `requirement_revision` 写入当前项目 `pendingServerActions`，并同步保存在浏览器本地状态。客户端每 12 秒探测服务；恢复连接后先对照服务端需求状态，按顺序调用对应人工裁决 API，再重新读取服务端状态覆盖本地乐观状态，避免临时任务或旧项目副本覆盖服务端产生的任务、事件和文档。

为兼容早期客户端，重连时也会查找带有 `humanDecisionAt` 的本地人工裁决；若服务端对应需求仍待裁决，则补入重放队列。已批准草稿以 `draftId` 匹配服务端的新 REQ 编号；过期整状态 PUT 不得复活原草稿。若服务端已是同一裁决，队列项直接清理；若服务端已被裁决为不同状态或项目中已找不到该需求，则保留队列项，不覆盖服务端决定。成功处理的队列项会从本地状态移除，并提示补同步数量。

当前实现为单浏览器、本地存储队列；没有跨设备队列、服务端离线命令日志或冲突解决界面。离线需求裁决与草稿修订可排队，其他离线编辑不在该队列范围内。

## 7. 会议流程

本地异步 scheduler 检查已启用项目和 `nextAutoMeetingAt`；配置 API 同时持久化节奏和默认议程。手动会议通过 `/api/meetings/run` 提交主题与议程。会议运行通过进程内异步锁串行；系统先保存带 `MEET-*` 编号的会议开始状态，然后调用 PDT、活跃任务相关角色、SE、QA（最多八位）逐一汇报，逐条同步消息、出席人和 transcript，再调用 PM 请求 JSON 纪要、决策请求、交付顺序与行动建议。解析失败保留原始输出为纪要但不启用行动项。会议原始输出、结构化建议和摘要写入状态；项目群和 PRD/详细设计/测试/自测/决策文档追加摘要。每个行动项标为待确认，只有用户调用转任务 API 且服务器复验关联需求已确认、角色有效、验收标准非空后才创建；重复调用返回同一任务。Agent 调用错误会记录部分失败；外层异常清除 `meetingLive` 并写失败事件。

另一个本地异步调度循环检查所有项目的需求计划；`queued` / `running` 记录会在服务重启后重新接管。循环按单进程全局锁串行调用已批准需求对应的 PM Agent。每次尝试有递增编号，Agent 在同一尝试中按需求、角色和标题重复创建任务时会返回现有任务，降低重启续跑时的重复。PM 需要使用项目工具创建带验收标准的角色任务、更新适用 PRD/设计/测试文档并发布群组摘要。至少创建一项关联子任务后才标为完成；失败会把 PM 父任务转为阻塞、保存错误和群消息，用户可以通过 retry API 重新排队。没有配置模型时队列保持待运行。前端在线时每 5 秒刷新项目状态。

服务启动时会扫描项目中的进行中会议，将遗留会议标为失败并保留已写入的 transcript，清除 `meetingLive`，记录审计事件；若自动会议仍开启，则重排下一轮，避免重复触发同一个遗留运行标志。该恢复通过 FastAPI lifespan 的隔离测试。当前仍没有后台会议任务队列，因此不会续跑中断角色发言；调度依赖本地单进程持续运行，定时触发、浏览器交互与真实 Deep Agents 输出尚未验收。角色选择依赖活跃任务并补入 PDT/SE/QA，不能保证覆盖全团队。

## 8. 安全与数据处理

- API key 只从服务端环境读取；不要在前端构建、项目 JSON、聊天或文档写入密钥。
- Agent 的通用 shell 执行能力从 harness profile 排除。
- API 当前没有用户认证；只适用于受信任的本地环境，不应直接暴露公网。
- 未来联网部署需增加认证授权、CSRF/CORS 策略、速率限制、密钥轮换、加密备份和 Agent 工具审计。

## 9. 角色 Runtime 路由与 Markdown 呈现

项目状态以 `roleRuntimes[role_id] = {provider, workspacePath}` 保存角色配置。默认 provider 为 `platform`，无需额外字段即可兼容旧项目。`PUT /api/team/config` 接收 `role_runtimes`，服务端验证角色、provider、绝对路径、目录存在性后规范化保存；每角色配置写入项目状态和团队配置事件。健康接口只公布 `codex`/`claude` 可执行文件是否能从服务进程 PATH 检测到，不返回其完整路径或任何凭据；命令探测不表示其账号已登录。团队页与单角色配置窗口均显示当前 Runtime；团队页额外展示三种 Runtime 的环境状态和逐角色配置入口。

会议纪要若以 JSON 字符串保存，前端会将摘要、裁决请求、交付顺序和行动项转为 Markdown；对于以多行 JSON 对象保存的旧纪要，会逐行解析并合并结构化字段，避免把 JSON 包装直接展示给用户。

`invoke_role` 依据角色选择平台 Deep Agents 或外部 CLI。PDT 的专用 `delegate_to_role` 工具可调用目标角色所选 Runtime；PDT 构建 Deep Agents 时会排除已选外部 Runtime 的专业子 Agent，避免配置被忽略。用户直聊、会议发言及该委派工具最终走同一调用入口。外部完成后，角色回复写入其角色会话及项目群，并记录运行/进度事件。

外部调用使用 `asyncio.create_subprocess_exec`，不经过 shell；工作目录必须是用户配置并重新验证过的现存绝对路径。Codex 命令采用 `exec --json --ephemeral --sandbox workspace-write -C <workspace> -`；Claude Code 采用 `-p --output-format stream-json --verbose --permission-mode acceptEdits --max-turns 8`。子进程受 360 秒超时和输出上限约束。调用向子 Runtime 传入有限的项目元数据、最近角色消息及专属提示词，不传入平台数据库工具；服务端移除由 DeepSeek 配置注入的密钥环境变量，保留本机 CLI 自身所需的登录配置。外部 Runtime 可以编辑其工作区代码，但不会直接落库 DevTing 的结构化需求、任务、事实或文档；其报告必须先同步，再由平台角色调用项目工具写入结构化产物。非零退出、CLI 缺失、目录无效和超时均向用户返回明确错误，不做静默运行时回退。

外部 CLI 的本机账号、CLI 安装、权限提示行为和供应商计费由当前操作系统用户负责。该本地集成目前没有为外部 CLI 提供 DevTing MCP 工具桥接，也没有把 CLI 编辑自动映射到项目任务或验收状态。

项目结构化需求、任务、事实、决策和文档仍由平台 Agent/专用 API 持久化。外部角色 Runtime 返回带上下文的工作报告，可编辑所选代码目录；它不能直接调用 DevTing 项目工具。隔离测试通过本地假 CLI 进程覆盖角色 `/api/chat` 路由、参数、工作目录、报告/群组同步、进度事件和密钥边界；尚未把代码 diff 自动关联回任务，也未验证供应商 CLI 的真实登录、输出格式和编辑行为。PDT/PM 等依赖项目工具的工作应保留平台 Runtime 或由平台工作流完成持久化。

## 10. 变更记录

- 2026-09-30：依据当前源代码整理模块、数据、接口、Agent、文档快照和会议流程；明确未实现项。
- 2026-09-30：更新需求裁决接口与人工裁决的状态/文档记录细节。
- 2026-09-30：补齐任务状态迁移与验收留痕、同一需求的补充分析回路、可校验汇报关系、角色动态和导出路径。
- 2026-09-30：为继承的默认原型工作区增加前端提示和全角色 Agent 数据来源约束。
- 2026-10-08：离线需求裁决进入本地待同步队列；服务恢复后自动补同步，并兼容旧客户端裁决。
- 2026-10-08：项目群和角色会话消息增加安全 Markdown 子集渲染，原始 HTML 转义且链接协议受限。
- 2026-10-09：人工批准触发持久化 PM 拆解队列；新增重试接口、自动调度与 UI 状态刷新。该作业队列仍是进程内单 worker 调度，不是多进程可靠消息系统。
- 2026-10-09：加入项目事实/假设记忆结构、来源关联 API、Agent 待核实写入限制和人工审阅状态。
- 2026-10-09：增加 Agent 按关键词检索较早项目记忆的受限工具；结果保留状态、来源与关联对象，并限制返回条数。
- 2026-10-09：项目记忆增加来源等级、版本、审阅人、200 字内变更摘要和历史；旧记录在服务启动时迁移为 E0 审计基线。新增需求追溯接口及动态项目摘要导出。
- 2026-10-09：候选需求采用 DRAFT 编号，支持必填裁决理由、四种 HITL 结果、正式 REQ v1 基线转换、草稿修订幂等、裁决历史与过期整体状态写入保护。实现规格见 [需求草稿与裁决规格](requirement-draft-spec.md)。
- 2026-10-09：会议 API/UI 加入主题/议程、结构化 PM 纪要、行动建议用户确认门禁和幂等转任务；隔离 mock API 核验通过。
- 2026-10-09：项目决策建议增加人工审阅 API 和决策台账 UI；相同裁决重放幂等、冲突裁决拒绝、全状态写入不覆盖会议/决策/任务；隔离 API 核验通过。
- 2026-10-09：角色人数、专属提示词改为专用服务端配置 API；完整状态写入保护汇报关系及角色配置，避免旧客户端状态覆盖。
- 2026-10-09：增加完整 JSON 项目导出和项目删除接口；删除要求精确项目名称，默认项目保护；事件、状态、文档版本在一次数据库事务中清理。
- 2026-10-13：根据浏览器实况修正总览及团队的任务状态汇总；项目创建向导可见性已在浏览器核验，创建提交和完整用户流程仍需隔离浏览器端到端验收。
- 2026-10-13：增加派生式树形 Roadmap、PDT 七阶段展示、任务串/并行字段、项目并行上限 API 和服务端执行位校验；Agent 图步骤与领域事件通过 SSE 推送，支持按 `Last-Event-ID` 补发，并隔离项目事件。隔离 API 并行规则核验通过；浏览器检查了路线图、节点详情和连接状态，真实 Agent 流、断线恢复和跨项目切换仍待完整端到端验收。
- 2026-10-13：把安全 Markdown 子集接入文档正文、详情、纪要、记忆、决策和进度内容，并支持任务清单；角色配置新增平台/Codex/Claude Code Runtime 选择、工作区校验、受限子进程路由和 PDT 委派同步。语法检查通过；Claude Code CLI 不在当前环境中，Markdown 安全边界与外部 CLI 端到端调用未验收。
- 2026-10-13：补齐需求列表、必读摘要、路线图节点等漏掉的 Markdown 展示位置并支持嵌套列表；团队视图显示角色当前 Runtime，配置说明区分 CLI 命令探测与登录状态。Node Markdown 烟测与角色页浏览器核对通过；真实 CLI、全页面安全边界和项目工具桥接仍未验收。
- 2026-10-13：再补齐路线图实时事件、文档版本摘要及需求/记忆追溯来源说明的 Markdown 呈现；新增入口尚未做浏览器回归。
- 2026-10-13：新增隔离 SQLite API 集成验收，按确定性替身贯通需求批准、PM 拆解、任务、会议行动项、记忆审阅、文档快照和 JSON 导出；不触发真实模型。
- 2026-10-13：继续补齐需求、决策、任务、会议行动项、记忆追溯、会议/文档/项目名称和下拉选项等行内 Markdown 展示，并更新静态资源版本；角色 Runtime 隔离测试用假子进程验证路由、同步、密钥与错误边界，真实供应商 CLI 仍未执行。
- 2026-10-13：应用 lifespan 启动恢复遗留的进行中会议：标记失败、保留已持久化发言、清除 `meetingLive` 并重排下次自动会议；隔离启动恢复测试验证幂等性。服务崩溃后不恢复未完成模型调用，会议自动触发和多实例调度仍未实现。
- 2026-10-13：Markdown 增加图片、裸链接、Setext 标题、波浪线代码围栏与结构化消息载荷归一化；文档骨架避免 Markdown 容器嵌套段落；团队页显示 Runtime 可用状态和角色设置入口。新实现尚未重新执行浏览器/安全回归。
- 2026-10-13：隔离浏览器核验首页构想、愿景文档和群消息的常见 Markdown 元素，以及原始 HTML 转义和危险链接过滤；团队页逐角色显示平台/Codex/Claude 配置并按实际探测状态提示。图片/裸链接等新语法、其余视图和供应商 CLI 未覆盖，详见[核验报告](test-report-2026-10-13-markdown-runtime.md)。
