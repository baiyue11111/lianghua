# 交易规则：统一入口与验证

实现：`src/core/calculation.py::TradeRules`。协议入口：`src/protocal.py::validate_execution`。

同一动作只有一个公共方法，算法由 execution 参数选择。Kernel、Settlement 和历史数据装配不按价格或数量模型分支。

## 支持的方法

| 动作 | 方法 | 可选模型或参数 |
|---|---|---|
| 目标数量分配 | allocate | 权重、风险限额、当前持仓 |
| 成交价格 | price | ratio：比例滑点；tick：跳数滑点；volume_share：成交量占比平方冲击 |
| 成交数量 | quantity | capacity：现金/可卖持仓；participation：同时限制累计参考容量 |
| 费用 | fees | dated：日期费率；none 或 half_up 舍入 |
| 税务 | tax | fifo：日终净买卖、取得批次和自然持有期；区分预览、应付、已付 |
| 交易检查 | check | 数量/价格单位、T+1、开盘风险、数据要求、明确价格上下界 |

模型内部实际完成计算，不转调旧价格、数量或费用模型。旧 ExecutionEngine、PositionBook、全局交易算法入口和无用逐笔 profile 缓存已删除。账户与税务事实账本保存状态，不另选模型。

价格比例/跳数和参与率限制参考 [RQAlpha 官方文档](https://www.ricequant.com/doc/rqalpha-plus/api/config)；平方冲击形式参考 [LEAN 官方文档](https://www.quantconnect.com/docs/v2/writing-algorithms/reality-modeling/slippage/supported-models)。本项目的 reference volume 是成交前20个有效交易日的原始成交股数均值，是日线估计，不是盘口成交量。

FIFO、日终净股份变化和自然持有期依据 [财税2012-85操作规则](https://www.chinatax.gov.cn/chinatax/n810341/n810765/n812151/201211/c1082457/content.html)，当前个人A股税务政策依据 [财税2015-101](https://www.chinatax.gov.cn/chinatax/n810341/n810765/n1465977/n1466017/c1967339/content.html)。这些政策不是所有投资者、所有证券的统一税率。

## 协议调用示例

在 workbench.backtest 或 workbench.preflight 的 UiRequest.data 中发送完整请求。下面改变价格与数量模型，其他字段保留明确选择：

```json
{
  "module": "plugins.strategies.moving_average_cross",
  "params": {"fast_window": 20, "slow_window": 60, "exposure": 0.4},
  "dataset_id": "history_holdout",
  "start": "2024-01-02",
  "end": "2025-12-31",
  "initial_cash": "1000000",
  "symbols": ["600036"],
  "execution": {
    "price": {
      "model": "volume_share",
      "params": {"impact": "0.1", "tick": "0.01", "capacity_basis": "prior20"}
    },
    "quantity": {
      "model": "participation",
      "params": {"participation": "0.0001", "capacity_basis": "prior20"}
    },
    "fees": {"model": "dated", "params": {"rounding": "half_up"}},
    "tax": {"model": "fifo", "params": {}},
    "check": {"model": "cn_a_share", "params": {"tick": "0.01", "min_qty": 100, "step": 100}}
  }
}
```

ratio 参数为 rate/tick；tick 参数为 ticks/tick；volume_share 参数为 impact/tick/capacity_basis。capacity 不需要参数。价格 tick 与交易检查 tick 必须相同。非法模型、未知参数、缺字段、无效数值和所需历史量缺失都明确拒绝。

公共配置在项目配置中明确指定。显式发送 execution 时必须完整，不补旧参数、不把未知模型替换为默认模型。

## 实际验证

入口为真实 `frontend/ui-protocol.py::UiRequest → protocal.py → Kernel → TradeRules → DataPort`，没有修改前端文件，也没有启动常驻服务。

数据为本地真实 history_holdout 快照；600036；2024-01-02 至 2025-12-31；485个交易日；MA20/60；仓位参数0.4；本金100万元。比例滑点 rate=0.001、跳数 ticks=2、平方冲击 impact=0.1、参与率 participation=0.0001。

| 价格 | 数量 | 成交笔数 | 不舍入最终权益 | 分币四舍五入最终权益 |
|---|---|---:|---:|---:|
| ratio | capacity | 14 | 997778.07562 | 997778.060 |
| ratio | participation | 30 | 999723.430177187500800 | 999723.405117187500800 |
| tick | capacity | 14 | 1000812.04402 | 1000812.060 |
| tick | participation | 30 | 1002621.164017187500800 | 1002621.155117187500800 |
| volume_share | capacity | 14 | 1003606.10374 | 1003606.090 |
| volume_share | participation | 30 | 1005342.571237187500800 | 1005342.555117187500800 |

十二组均通过：模型/费用切换确实改变成交数据；每组预检查与运行有效配置及配置哈希相同；十二个配置哈希与执行哈希分别不同；执行证据绑定实际选项，不再标成固定默认算法。

ratio + capacity + none 与改造前的 metrics、equity、trades、完整 trace、positions、final_cash、final_equity、pending_orders 逐项完全一致。结果用于证明实现和参数切换生效，不用于挑选收益最高的模型。

边界测试包括最低数量与递增步长、零股、T+1、成交价与资金联合求解、全部费用、过户费仓位边界、同日累计容量、重放、现金保留额变化、税预览失败回滚、停牌与复牌历史均值。测试不保留额外永久真实样本。

## 明确限制

- 不还原盘口、排队、逐笔成交或券商实际扣款时刻。
- 日线参考容量/平方冲击是估计；参数需要校准，不能视为行业统一常数。
- 快照没有完整的每日官方涨跌停价与全部板块规则。只有提供明确上下界及来源时才检查；没有猜测ST、IPO或除权日价格。
- 默认 min_qty=100、step=100 是所选普通A股规则输入，不代表所有证券。通用数学的其他网格由测试验证，不等于已实测全部板块。
- 未实现前端新的模型选择控件；现有 UiRequest 协议可以传参。
- 这是工程验证，不是策略资格认定、实盘验证或盈利证明。

## 交付状态

实现与真实模型矩阵验证完成。主代理最终独立全量测试：496项，0失败、0错误；19项独立规则验收通过。最终源码版本的十二组真实运行再次通过。独立代码与异常路径审查通过。

验证命令：`PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. .venv/bin/python -m unittest discover -s tests -q`。pytest未使用。

本轮临时回测、失败证据、基线与矩阵JSON通过DataPort清理，只保留本报告和可重跑测试。frontend/未修改，没有新建模型目录或独立适配层。未提交PR。
