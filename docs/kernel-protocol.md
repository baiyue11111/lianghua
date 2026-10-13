# 内核与统一协议

当前后端协议的完整说明见[后端协议入口](backend-protocol.md)。

核心边界保持不变：

| 文件 | 责任 |
| --- | --- |
| `src/core/contracts.py` | 行情、目标、订单、成交、账户和单步记录 |
| `src/core/backtest.py` | 回测输入、结果和稳定业务错误 |
| `src/core/interfaces.py` | 策略、数据源、账户、执行器和记录器端口 |
| `src/core/kernel.py` | 常驻内核、单次运行状态、校验和固定执行顺序 |
| `src/protocal.py` | 唯一后端入口、操作目录、模块分发和 UI 消息转换 |
| `frontend/ui-protocol.py` | 用户维护的前后端公共消息契约 |

`src/protocal.py` 通过 `Modules` 接收五个模块端口。只有
`KernelBacktestPort` 持有 `Kernel`。模块内部不需要知道 UI、HTTP 或 CLI。

## 运行关系

```python
from protocal import Modules, Protocol, build_protocol

backend = build_protocol(modules)
messages = await backend.send(ui_request)
```

输入是 `frontend/ui-protocol.py` 的 `UiRequest`。输出始终是 `UiMessage` 列表。
操作名、输入 schema、输出 schema、读写模式和所属端口由
`export_operations()` 统一产生。未知操作、错误 schema 和模块异常都变成
`problem` 消息，并保留请求的 `correlation_id`。

Kernel 仍然负责固定的执行顺序和运行隔离。它不负责策略名单、插件加载、UI、
存储、HTTP 或页面渲染。历史账户核算代码仍保留为证据与迁移输入，但已经没有
公开执行入口。Kernel 通过 `DataBus.query(DataQuery)` 取得包含完整
`HistoryDay` 事件的 Dataset，再按 `Settlement.apply -> strategy.generate ->
Settlement.plan` 执行。不存在 `RunFactory` 或 `HistoricalLake.run` 兼容入口。

React 通过 `frontend/web/src/backend.ts` 连接唯一的 `/api/protocol`。HTTP 适配器
只做信封转换；CLI 也只调用注入的 `BackendPort`。两者都不导入业务模块。
