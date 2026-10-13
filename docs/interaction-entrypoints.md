# 交互核心与两个协议面

项目有两个用户入口：Python CLI 和 React Web。它们使用同一份 TypeScript 交互核心。各入口保留各自会话和现有功能菜单。

[打开中文框架图](interaction-core.html)

| 位置 | 职责 |
|---|---|
| `frontend/cli.py` | 参数、帮助、动作转换、stdout/stderr、退出码、进程回收 |
| `frontend/cli.mjs` | JSON Lines 与公共前端端口之间的 stdio 适配 |
| `frontend/web/src/interaction/` | React 事件转动作，订阅状态；不调用业务 HTTP |
| `frontend/display/src/core.ts` | 动作校验、状态转换、执行与错误、检查身份、乱序保护、请求锁、轮询和关闭 |
| `frontend/display/src/features.ts` | 注册现有功能的操作、schema、检查及呈现状态转换 |
| `frontend/ui-protocol.py` | 唯一手工维护的公共契约；分别定义前端面和后端面 |

## 前端面

`UiAction` 描述用户意图：`edit / select / query / invoke / reset / dismiss`。`target` 表示会话内的目标，`operation` 表示注册的功能操作。

`FrontendPort` 只有三项：`dispatch(action)`、`snapshot()`、`subscribe(listener)`。提交动作是异步操作；订阅回调用于通知，入口随后读取快照。

`UiState` 保存各目标的输入、选择、查询、消息、问题和操作状态。`revision` 是输入身份；状态根版本随更新递增。每个操作有 `idle / pending / succeeded / failed`，以及核心计算的 `available`。检查批准绑定输入版本和准备后的完整输入，不能通过换请求正文绕过检查。

刷新相同输入时保留已发布内容和焦点；请求状态单独变为 `pending`。更换查询时，旧响应不会覆盖新查询。发生错误时显示问题，停止自动轮询。显示的旧内容不代表刷新成功。

## 后端面

`UiRequest` 包含请求标识、操作标识、输入 schema 标识和 JSON 数据。后端发送端口是 `send(request)`，返回 `UiMessage[]`。响应的 `correlation_id` 必须匹配请求标识。

`UiMessage` 保留八种消息：资源、集合、序列、文档、操作、事件、问题和自定义。语义提示、关系链接和命名空间扩展保留。金额及精确数量以十进制字符串传输，核心不转成浮点数。

主动消息使用 `receive(message)`。事件类型的目标绑定和自定义处理器由装配时显式注册。协议不保存路由、策略名单或业务算法。

操作注册声明输入 schema 和允许的响应 schema。核心验证公共信封、schema 允许列表和注册的正文 schema；现有功能的业务输出格式保留，未在本次迁移中增加全字段业务校验。

## 当前接线状态

**浏览器和 Python CLI 都只接受 `BackendPort`。后端只有 `Protocol.send` 一个业务入口。**

浏览器适配器把 `UiRequest` 发送到唯一 HTTP 路由 `POST /api/protocol`。
`frontend/web.py` 只解析信封并调用注入的 `BackendPort`。CLI 使用同一个端口，
不再拥有独立回测函数。旧 `/api/*` 业务路由已删除。

`src/protocal.py` 统一维护操作目录、模块分发、错误消息和运行时装配。
`workbench`、数据湖和命令实现已经移到 `src/`。只有协议层可以接入
`core.kernel.Kernel`。运行时通过标准 `DataQuery` 注入 Dataset 和 Settlement；协议层
不会伪造历史数据，也不会回退到旧的 `HistoricalLake.run`。

## 阅读和验证

先读 `ui-protocol.py` 的 `UiAction / UiState / UiRequest`，再读 `core.ts` 的 `update / dispatch / receive`，最后读 `features.ts` 与两种入口适配器。

```sh
npm ci --prefix frontend/display
npm ci --prefix frontend/web
npm test --prefix frontend/display
npm run test:dev --prefix frontend/web
npm run build --prefix frontend/web
.venv/bin/python -m unittest tests.test_ui_protocol tests.test_cli tests.test_cli_validation_gate tests.test_frontend_layout
```

`npm run generate --prefix frontend/display` 更新生成文件；`check:protocol` 和 `check:runtime` 分别检查契约与发布产物是否过期。Python 包通过 `package-data` 包含已编译 CLI 核心，不在运行时安装 Node 依赖。

Web 的三个 mounted 测试脚本通过内存页面验证交互，不启动监听端口：`strategy-ui.mjs`、`research-ui.mjs`、`audit-ui.mjs`。测试命令需要提供已安装的 Playwright 路径。

## 设计依据

- Fowler：[Presentation Model](https://martinfowler.com/eaaDev/PresentationModel.html)。交互行为与状态独立于控件。
- Cockburn：[Hexagonal Architecture](https://alistair.cockburn.us/hexagonal-architecture)。用户入口与后端分别接不同端口。
- Martin：《Clean Architecture》第 22 章[官方节选](https://www.informit.com/articles/article.aspx?p=2832399&seqNum=2)。跨边界传普通数据。
- Evans：[DDD Reference](https://www.domainlanguage.com/wp-content/uploads/2016/05/DDD_Reference_2015-03.pdf)。领域规则留在后端。
- [Elm Architecture](https://guide.elm-lang.org/architecture/)。动作与状态转换分离于外部执行。

目录、语言桥接和具体字段是本项目的选择。以上资料支持职责边界。
