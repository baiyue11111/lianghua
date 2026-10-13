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
`Kernel` 通过构造参数接收策略加载器、`DataBus` 查询器和 `Settlement` 类型。
每个原子运行和每个搜索试验都会新建策略与结算状态。独立的 `Run` 步进接口仍使用
`core.interfaces.py` 的 `Components`，但当前回测协议不通过它装配账户、执行器和记录器。
协议层不认识这些插件的内部类。

## 操作目录

`export_operations()` 是操作目录的唯一来源。每项包含操作名、读写模式、输入
与输出 schema、消息类型和所属模块端口。前端不应复制一份操作清单。

当前端口分组如下：

| 端口 | 负责内容 |
| --- | --- |
| `backtests` | 策略目录、预检、回测 |
| `research` | runs、benchmarks |
| `sources` | 数据源、同步、调度、快照、证据和 review |
| `lake` | 研究数据、闭环报告、查询、K 线、抓取、合并和 doctor |
| `commands` | CLI run、ingest、report；run 再转交 `backtests` |

## 错误规则

请求必须是 `UiRequest`，操作名和输入 schema 必须在目录中。未知操作、错误
schema、模块输入错误和模块失败都返回 `problem`，并保留 `request_id` 作为
`correlation_id`。不提供隐式 fallback，也不把异常传给前端传输层。

搜索声明在执行开始前就被拒绝时，不创建 `data/runs`，不写 `failed.json`，也不返回
`x-evidence`。只有已经开始执行的搜索失败，才保存已完成试验和失败证据；`busy` 只返回
`busy`，不写任何失败记录。

## 当前边界

`frontend/web.py` 只保留 `POST /api/protocol`。浏览器适配器和 CLI 都只认识
`BackendPort`。业务模块已经移到 `src/workbench/`、`src/collect/` 和其他
`src/` 文件，并由 `build_backend()` 一次装配。

`build_backend(root)` 一次装配 `DataBus`、注册的数据 reader、策略加载器和
Settlement 插件。`DataQuery` 是唯一的历史数据入口；`HistoricalLake` 只准备
包含 `HistoryDay` 事件、政策和 provenance 的 `Dataset`，不执行账户。

## 最小使用例子

从项目根目录并在 `PYTHONPATH=src:.` 环境运行此示例。当前 production 配置只注册
`history_holdout`；示例要求该数据集已注册，并且查询区间通过数据覆盖校验。

```python
import asyncio
from importlib import import_module
from pathlib import Path
from protocal import build_backend

ui = import_module("frontend.ui-protocol")
backend = build_backend(Path.cwd())

request = ui.UiRequest(
    "request-1",
    "workbench.backtest",
    "urn:lianghua:ui:input:workbench.backtest:v1",
    {
        "module": "plugins.strategies.moving_average_cross",
        "params": {"fast_window": 2, "slow_window": 3},
        "dataset_id": "history_holdout",
        "start": "2024-01-02",
        "end": "2024-03-29",
        "initial_cash": "100000",
    },
)
messages = asyncio.run(backend.send(request))
message = messages[0]
assert message.kind == "resource"
assert message.correlation_id == request.request_id
print(message.data["run_id"])
```

调用者不直接创建 `DataBus` 或 `DataQuery`。`build_backend(root)` 在内部装配
`DataBus`、历史 reader、策略 loader 和 Settlement；公共调用只经过
`UiRequest -> Protocol.send -> UiMessage`。

## 回测与候选搜索

`workbench.backtest` 默认只执行一次。请求不含 `search` 时，返回值和原子回测保持相同。

```json
{
  "module": "plugins.strategies.moving_average_cross",
  "params": {"fast_window": 2, "slow_window": 3},
  "dataset_id": "fixture",
  "start": "2020-01-31",
  "end": "2020-02-03",
  "initial_cash": "100000"
}
```

搜索仍使用同一个 `workbench.backtest` 操作。更新器声明只允许本地 `core.calculation`
或 `plugins.*` 模块中的公开 `staticmethod` / `classmethod`。当前内置更新器只按给定
候选顺序遍历，不是梯度优化，也不是贝叶斯优化。

下面的 search JSON 使用 `fixture` 工程测试数据，仅用于测试示例。实际使用时，替换为
已注册的 dataset 及其真实覆盖区间；不能把 `fixture` 当作 production 数据集。

```json
{
  "module": "plugins.strategies.moving_average_cross",
  "params": {"fast_window": 2, "slow_window": 3},
  "dataset_id": "fixture",
  "start": "2020-01-31",
  "end": "2020-02-03",
  "initial_cash": "100000",
  "search": {
    "updater": {
      "module": "core.calculation:Calculation",
      "method": "propose",
      "params": {"candidates": [
        {"fast_window": 1, "slow_window": 3},
        {"fast_window": 2, "slow_window": 3}
      ]}
    },
    "max_trials": 2,
    "metric": "total_return",
    "direction": "max",
    "validation": {"start": "2020-02-04", "end": "2020-02-04"}
  }
}
```

每个成功试验、验证结果和基础搜索 summary 都经 `DataBus` 保存到 `data/runs`。
试验身份形如 `run-trial-0001`，验证身份形如 `run-validation`，基础请求身份仍是
`run`。验证只使用最佳参数，不参与排序，也不会反馈给更新器。搜索失败时，已完成试验
和失败证据仍经同一个 `DataBus` 保存；忙请求只返回 `busy`，不会写失败记录。

成功响应的主要结构是：

```json
{
  "run_id": "search",
  "parameters": {"fast_window": 2, "slow_window": 3},
  "search": {
    "metric": "total_return",
    "direction": "max",
    "best_run_id": "search-trial-0002",
    "best_index": 1,
    "trials": [{"run_id": "search-trial-0001", "score": 0.01}, {"run_id": "search-trial-0002", "score": 0.02}],
    "validation": {"run_id": "search-validation", "final_equity": "101000"}
  }
}
```

研究读取也使用同一个 `BackendPort`：

```python
async def read_search():
    runs = await backend.send(ui.UiRequest(
        "runs-1", "workbench.runs", "urn:lianghua:ui:input:workbench.runs:v1", {}
    ))
    detail = await backend.send(ui.UiRequest(
        "run-1", "workbench.run", "urn:lianghua:ui:input:workbench.run:v1",
        {"id": "search"},
    ))
    assert runs[0].kind == "collection"
    assert detail[0].kind == "resource"
    return runs[0].data, detail[0].data

runs_data, detail_data = asyncio.run(read_search())
print(runs_data["items"], detail_data["parameters"], detail_data["evidence"])
```

`symbols=None` 表示注册数据集的完整声明范围，不表示全市场。自定义 `symbols` 或
`universe` 必须与数据集和策略要求一致，否则 `DataBus.query` 拒绝。

`as_of=None` 是明确的 snapshot replay，不承诺当时可知。传入 `as_of` 后，价格、
日历、股票池、公司行动和政策的每个 source 文件都必须有与文件哈希绑定、且不晚于
cutoff 的 `available_at`；缺证据或未来证据都会拒绝，不把 effective date 当作
available date。

信号插件必须显式声明模块、参数和训练窗口：

```json
{
  "adapter": {"module": "plugins.data:Adapter", "method": "validate",
               "fields": ["signal_close"], "params": {}},
  "scaler": {"module": "plugins.data:Scaler", "fields": ["signal_close"],
              "fit_start": "2023-01-03", "fit_end": "2023-12-29",
              "params": {"method": "standard"}}
}
```

训练窗口必须完整落在回测 `start` 之前。插件只改变策略信号字段，不改变 raw bars
或账户执行数据。失败运行不写成功 manifest；可用
`DataBus.read("runs", run_id, "failed.json")` 查询 partial evidence。

## 数据和采集

`DataPort`、`DataRef`、`DataError`、`DataBus`、`DataQuery` 和 `Dataset` 均在 `src/protocal.py`。`build_backend()`
只创建一个 `DataBus(DataStore)` 和一个共享 `Store`。活动文件仍放在 `data/`，
保留 JSON、Parquet 等格式，不另建 SQL 数据库。

数据协议有 `read`、`write`、`list`、`path`、`delete`。最后一项用于受控的缓存清理。
缺失、非法身份、物理读写失败分别报告 `missing`、`invalid_identity`、`io_failed`。
业务模块不得新建 `DataStore` 或决定活动数据根目录。

所有外部采集程序集中在 `src/collect/`。公共交互入口是 `CollectPort.handle`，
只由协议分发调用。`display.py` 处理参数、手动采集、调度、任务和证据交互；
`core.py` 管任务流程；`data.py` 管共享证据保存；`quotes.py` 管报价供应商。
数据湖的正式输出也通过同一总线；合并过程允许使用会自动清理的临时 Parquet。

历史报价输入支持 `codes`、`start`、`end`、`frequency`、`adjustment`、`fields`。
分钟频率需要 `time` 字段。最新报价可选择 `name`、`price`、`change_pct`、`volume`、
`amount`，不支持历史频率和复权。错误参数不使用替代值。

前端现有采集页支持代码、日期、间隔和开关。频率、复权和字段的后端协议已实现，
但现有页面没有对应控件；本次未修改前端源代码。其他后台读写模块及旧 `runs*`
清理不因这次报价试点而被宣告完成。
