# 后端协议入口

后端对外只有一个入口：`src/protocal.py`。

它接收 `frontend/ui-protocol.py` 的 `UiRequest`，按操作目录选择一个模块端口，
再返回 `UiMessage`。传输方式不属于协议层。HTTP、CLI、React 和后台任务都应
实现 `BackendPort.send`，不能直接调用 `Kernel` 或业务模块。

## 固定关系

```text
frontend/ui-protocol.py
          │ UiRequest / UiMessage
          ▼
src/protocal.py
          │ Modules
 ┌────────┼─────────┬────────┬─────────┐
 ▼        ▼         ▼        ▼         ▼
backtests research sources  lake    commands
          │
          ▼
     core.kernel.Kernel
```

`Kernel` 是常驻的固定业务内核。只有 `KernelBacktestPort` 可以把它接入协议。
策略、数据源、账户、执行器和记录器仍由 `core.interfaces.py` 的插件端口装配。
协议层不认识这些插件的内部类。

## 操作目录

`export_operations()` 是操作目录的唯一来源。每项包含操作名、读写模式、输入
与输出 schema、消息类型和所属模块端口。前端不应复制一份操作清单。

当前端口分组如下：

| 端口 | 负责内容 |
| --- | --- |
| `backtests` | 策略目录、预检、回测、CLI run |
| `research` | runs、benchmarks、研究数据和闭环报告 |
| `sources` | 数据源、同步、调度、快照、证据和 review |
| `lake` | 查询、K 线、抓取、合并、持续抓取和 doctor |
| `commands` | CLI ingest、report |

## 错误规则

请求必须是 `UiRequest`，操作名和输入 schema 必须在目录中。未知操作、错误
schema、模块输入错误和模块失败都返回 `problem`，并保留 `request_id` 作为
`correlation_id`。不提供隐式 fallback，也不把异常传给前端传输层。

## 迁移边界

本轮没有修改用户维护的 `frontend/`。现有 `frontend/web.py` 和
`frontend/api/` 仍是旧 HTTP 实现，不能作为新的协议实现继续扩展。前端切换时，
只需让唯一的 `BackendPort` 调用 `Protocol.send`，并移除旧 `/api/*` 路由；不需要
为每个业务特性新建一个前后端协议文件。

历史账户核算仍在 `src/history_data.py`，本轮只规定它以后通过模块端口接入，
不改变现有核算结果。
