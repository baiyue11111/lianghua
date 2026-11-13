# 引擎契约与数据口径（写策略必须知道的部分）

本文件是项目内核的只读摘要，用于避免重复翻源码。若与源码不一致，**以源码为准**并
按 `iteration.md` 更新本文件。相关源码：`src/protocol.py`、`src/core/calculation.py`、
`src/plugins/data.py`、`src/storage.py`、`src/market/reader.py`。

## 1. 策略 I/O schema

```python
@dataclass(frozen=True)
class MarketFrame:
    at: datetime
    venue: str
    fields: Mapping[str, Mapping[str, Scalar]]
    context: Mapping[str, Scalar]
    history: Mapping[str, tuple[HistoryBar, ...]]

@dataclass(frozen=True)
class AccountSnapshot:
    as_of: datetime
    cash: Decimal
    equity: Decimal
    positions: Mapping[str, Decimal]
    sellable: Mapping[str, Decimal]

@dataclass(frozen=True)
class TargetSet:
    source_id: str
    as_of: datetime
    weights: Mapping[str, float]
    reasons: Mapping[str, str]
```

- 策略入口 `generate(frame: MarketFrame, account: AccountSnapshot) -> TargetSet | None`。
- `TargetSet` 约束：`weights` 有限、非负、合计 ≤ 1；`as_of` 早于 `next_session`
  且时间戳单调递增；不允许杠杆或做空。
- 策略**不能**直接输出股数、订单类型、价格，也不能指定成交方式。
- `AccountSnapshot.equity` 存在，不代表允许用于 alpha 决策（见 G1）。
  它是账本给策略的状态视图，不是信号源。

## 2. 配置与结算字段

| 字段 | 含义 / 注意 |
|---|---|
| `initial_cash` | 字符串金额，必须与 `params.principal` 一致 |
| `params.principal` | 策略侧本金，改资金档时必须同步改 |
| `delisting_haircut` | `0` = 退市残值核销为 0；`1` = 按账面全额回收。全项目固定 `0` |
| `limits.stock_weight` | 单股权重上限 |
| `limits.adv_fraction` | 单笔订单金额相对 20 日均成交额的上限；**整单跳过**，无部分成交 |
| `execution.check.params.step` | A 股整手 100；`min_qty=100` |
| `intervals` | 分段回测区间；连续单账户口径用一个大区间，不与分段口径混用 |

`Settlement.resolve()` 把 `limits` 转成 `AllocationLimits(lot_size=step, ...)`；
`_limits()` 只接受 `{"stock_weight", "adv_fraction"}` 两个键。

## 3. 目标权重 → 订单

```python
class AllocationLimits:
    lot_size: int = 100
    stock_weight: Decimal | None = None
    adv_fraction: Decimal | None = None

# TradeRules.allocate() 的关键行为
desired = int(equity * weight / price // lot_size) * lot_size   # 逐只 floor，无最大余数补手
requested = delta // lot * lot if delta > 0 else abs(delta)
```

当前实现的**缺失**（解释结果时务必声明）：
- 无最大余数补手、无不交易带、无部分成交、无跨日拆单、无冲击成本模型；
- `adv_fraction` 超限时整单跳过，不是按比例缩量；
- 因此小资金现金拖累偏大、大资金容量结果偏保守。

`run_events.allocations` 的 `reason` 枚举：
`below_one_lot`、`order_above_adv_limit`、`inside_no_trade_band`（后者仅运行期补丁产生）。

## 4. 数据口径（踩过坑的都在这）

| 项 | 正确做法 |
|---|---|
| `close` | 原始未复权价；**面值 / 价格过滤必须用它** |
| `signal_close` | `raw_close × backward_factor`（后复权），用于信号与收益计算 |
| `fact_adjust` | 用 Baostock `backward` 稳定因子；`forward` 会漂移 |
| `result.positions` | **复权单位**（`position_unit=adjusted_units`）；市值 = `units × raw_close × backward_factor`，核对式：合计 ≈ `final_equity − final_cash` |
| `run_trades.symbol` | 裸 6 位代码如 `603189`，**不要**加 `sh.` / `sz.` 前缀 |
| `run_events` 的 `run_id` | 带 `-interval-0001` 后缀，查询用 `run_id LIKE substr(run_id,1,12) \|\| '%'` |
| 有效持仓统计 | 按 ≥ 0.5% 净值过滤；`positions` 里 200+ 只是碎股，不是 300 只持仓 |
| 调仓日 | 周内最后交易日决策、下一交易日执行；`frame.at.isocalendar()[:2] != following.isocalendar()[:2]` |

## 5. 官方 `--backtest` 输出 JSON

```json
{
  "run_id": "<32 hex>",
  "intervals": [{
    "index": 1, "name": "...", "run_id": "<run_id>-interval-0001",
    "result": {
      "metrics": {"initial_equity": 100000, "final_equity": 1302046.11,
                  "total_return": 12.02046, "cagr": 0.22104,
                  "annualized_volatility": 0.20826, "sharpe": 1.09958,
                  "max_drawdown": -0.42846, "observations": 3123},
      "final_cash": "155551.08...", "final_equity": "1302046.11...",
      "positions": {"300002": "0.00378...", ...},
      "equity": [{"at": "2014-01-02", "equity": 100000}, ...],
      "fills": 26889, "config_hash": "...", "position_unit": "adjusted_units"
    }
  }]
}
```

`metrics.total_return` 是**小数**（12.02 = 1202%），不是百分数。
`scripts/check_scale_invariance.py` 直接消费该结构。

## 6. 操作边界的技术原因

- `data/catalog.duckdb` 是 DuckDB **单写入者**库：并行 `--backtest` 会触发
  `Conflicting lock is held ...`。
- `storage.DataStore._reconcile()` 信任 `_files` manifest：打开时会
  `stray.unlink()` 删除 manifest 未声明的 `data/parquet/<table>/part-*.parquet`。
  因此复制 catalog 后软链真实 `data/parquet`（R28 事故）会让旧 manifest
  把真实分片当 stray 删除。
- 历史事故：49 个日线分片 / 7,320 行 / 1,221 只股票（2026-11-02~11-09）被删，
  最终用官方 `--collect "沪深:range" --data daily_sina --codes ...` 分 9 批回填。
- 结论：所有回测串行、只走官方 CLI；任何绕过动作先请示用户（G6）。
