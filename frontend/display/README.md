# 交互层核心

入口把用户动作交给这里。这里检查动作、保存状态、调用注入的后端端口，再把状态交回入口。

- `src/core.ts`：纯状态转换 `update`，以及执行器 `createDisplay`。管理请求、检查身份、重复写入、轮询、事件和会话关闭。
- `src/features.ts`：现有功能的操作注册、输入检查和结果整理。没有 HTTP 路由。
- `src/research/`、`src/lib/`：参数校验、分组、曲线及格式计算。
- `src/protocol.schema.json`、`src/protocol.d.ts`：从 `frontend/ui-protocol.py` 生成。公共模型和端口签名都从 Python 定义生成。不要手改。
- `runtime/cli.mjs`：随 Python 包发布的 Node 产物。不要手改。

核心不导入 React、DOM、Python 后端模块，不读取文件，也不启动服务器。构建工具和 CLI 适配器的进程操作在核心之外。

```sh
npm ci --prefix frontend/display
npm test --prefix frontend/display
npm run generate --prefix frontend/display
npm run check:runtime --prefix frontend/display
```

`npm test` 检查生成文件、独立 TypeScript 类型和行为。生成协议需要项目的 Python 3.12+ 虚拟环境。运行 CLI 需要 Node 22+；CLI 不在运行时编译或下载。

每个 `createDisplay` 是独立会话。Web 用 `useSyncExternalStore` 订阅它；Python CLI 用 JSON Lines 驱动同一份代码。

后端通过 `BackendPort.send(UiRequest)` 返回标准消息。主动消息经 `receive(UiMessage)` 接收，消息类型必须在会话的 `events` 注册表中绑定目标。自定义类型还必须注册处理器。

生产入口没有注入真实后端时返回“未连接”。测试替身只在 `tests/` 和 Web 测试脚本中。
