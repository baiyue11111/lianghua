# 案例 R29：三个开关——净值反馈 / 容量约束 / 取整优化

> 首个入库案例，是 G1–G5 的直接证据来源。日期 2026-11-10 ~ 11-11。
> 交付报告：`/tmp/r29_out/index.html`；明细：`/tmp/r29_out/exp_sweep.csv`、
> `exp_summary.json`、`exp_curves.json`、`exp_equity.csv`、`exp_backtest.py`。

## 1. 待解释的现象

R28 用 5 个策略 × 4 档资金（10 万 / 50 万 / 100 万 / 1000 万）跑连续单账户回测，
发现 10 万档系统性更差（五策略均值 654.5% vs 100 万档 958.5%），且 1000 万与 100 万
**并非逐位一致**（f1 差 234pp）。用户质疑：真实量化通常只讲「大资金容量约束」，
为什么这里小资金也会变？

三种候选解释：
1. 策略层：`_dilution()` 读 `account.equity`，把已实现净值反馈进仓位档位；
2. 容量层：`limits.adv_fraction` 未启用，大资金没有成交量上限；
3. 执行层：`TradeRules.allocate()` 逐只 `floor` 到整手，10 万档频繁 `below_one_lot`。

## 2. 实验设计（单变量）

标的 `plugins.strategies.exp_qf_h8f1`，`delisting_haircut: "0"`，
连续单账户 2014-01-02 → 2026-11-09，`intervals` 完全一致。
九格 = 3 个基线 + 4 个配置实验（E1–E4）+ 2 个运行期补丁（P1/P2）。

| 组 | 唯一改动 | 配置关键字段 |
|---|---|---|
| 基线 10w / 100w / 1000w | 仅 `initial_cash` + `params.principal` | `dilute_positions=60`, `max_positions=20` |
| E1 关反馈 10w | `params.dilute_positions: 60 → 20` | 等于 `max_positions`，档位放大被抵消 |
| E2 关反馈 100w | 同上 | — |
| E3 容量 100w | 新增 `limits.adv_fraction: 0.1` | `dilute_positions` 保持 60 |
| E4 容量 1000w | 同上 | — |
| P1 / P2 改 allocator | 运行期替换 `core.calculation.TradeRules` | 最大余数补手 + 不交易带，**未改磁盘源码** |

配置示例（E1）：`initial_cash="100000"`, `params.principal=100000.0`,
`params.dilute_positions=20`, `params.max_positions=20`, `delisting_haircut="0"`。
E3：`limits={"adv_fraction": 0.1}`。
所有回测**串行**执行，单组约 10–11 分钟；P1/P2 用 `/tmp/exp_allocator.py`
在进程内替换类，不触碰项目文件。

## 3. 完整结果

| 场景 | 总收益 | CAGR | 夏普 | MDD | 现金占比 | 成交笔数 | 主要跳过原因 |
|---|---:|---:|---:|---:|---:|---:|---|
| 10 万 基线 | 940.29% | 19.99% | 1.095 | −36.53% | 12.04% | 39,484 | `below_one_lot`×2714 |
| 100 万 基线 | 1313.00% | 22.88% | 1.116 | −44.40% | 11.51% | 54,967 | `below_one_lot`×9 |
| 1000 万 基线 | 1078.75% | 21.16% | 1.043 | −46.21% | 15.12% | 57,433 | — |
| 10 万 关净值反馈 | 1202.05% | 22.10% | 1.100 | −42.85% | 11.95% | 26,889 | `below_one_lot`×86 |
| 100 万 关净值反馈 | 1193.23% | 22.04% | 1.058 | −46.27% | 11.52% | 27,933 | `below_one_lot`×1 |
| 100 万 容量 10% | 1207.69% | 22.15% | 1.089 | −44.40% | 17.45% | 54,232 | `order_above_adv_limit`×183; `below_one_lot`×8 |
| 1000 万 容量 10% | 514.04% | 15.17% | 1.016 | −38.80% | **72.86%** | 37,589 | `order_above_adv_limit`×6454 |
| 10 万 余数+不交易带 | 920.98% | 19.82% | **0.970** | −47.08% | **5.39%** | 47,932 | `inside_no_trade_band`×32172; `below_one_lot`×719 |
| 100 万 余数+不交易带 | 1096.66% | 21.31% | 1.024 | −45.77% | 9.13% | 53,653 | `inside_no_trade_band`×57591; `below_one_lot`×17 |

**关键差值**

| 对照 | 10 万 ↔ 100 万收益差 |
|---|---:|
| 基线 | **+372.7pp** |
| 关净值反馈 | **−8.8pp** |
| 只改 allocator | **+175.7pp** |

容量效应（相对各自基线）：100 万 −105.3pp，1000 万 **−564.7pp**。

## 4. 机制链（为什么 373pp 不是「本金效应」）

```
A 股 100 股一手 + 逐只 floor
  → 10 万档目标权重被截断，2,714 次 below_one_lot（100 万档仅 9 次）
  → 实际权重与目标权重偏离，净值出现微小相对差
  → 策略 _dilution(day, equity) 用 account.equity / 净值轨道算 dilution
  → slots = round(base_slots + dilution × (60 − base_slots))，档位在 12~60 间分叉
  → 两套不同的选股分位起点 / 敞口 → 两套不同股票
  → 每周调仓反复放大，2014 年末归一化净值差 <0.02，2026 年累积成 372.7pp
```

代码位置：`src/plugins/strategies/exp_qf_h8f1.py`
- L406–411：`equity = float(account.equity)` → `self.nav_values.append(equity)` → `dilution = self._dilution(...)`
- L346–365：`ratio = (equity / past) / (1 + track_slope * span_years)`，超出轨道则加档
- L439–445：`slots` / `start` / `exposure` 三处都被 `dilution` 改写

关反馈后 `dilute_positions == max_positions`，`slots` 不再随净值变化，
两档走同一套股票，差距坍塌到 −8.8pp。**这证明主因在策略层，不在资金量、也不在取整本身。**

只改 allocator 把 10 万现金拖累从 12.04% 压到 5.39%，
但收益 940.29% → 920.98%、夏普 1.095 → 0.970，差距仍有 175.7pp：
执行层优化减小了跟踪误差，却没有切断净值反馈，因此无法消除路径依赖。

## 5. 结论与边界

**成立：**
- 策略读已实现净值做仓位缩放，会把执行噪声放大成完全不同的组合路径；
  这正是机构把 execution shortfall 留在 bps 层、不接入 alpha 决策的原因。
- `adv_fraction` 在 1000 万档造成 72.86% 现金与 6,454 次整单跳过，
  才是「大资金容量约束」对应的效应。
- 工程优先级：先去策略层业绩反馈 → 再补容量 / 参与率模型 → 最后做取整优化。

**不成立 / 不能外推：**
- 不能说「策略 f1 的真实收益是 1313%」——该数字含明显路径运气，
  四档离散度才是更稳健的预期参照。
- 不能把 1000 万档 514.04% 当真实容量预测：实现是整单跳过，
  无部分成交、跨日拆单、冲击成本，只是保守上界。
- P1/P2 把 `lot_optimizer` 与 `no_trade_band` 绑在一个补丁里，
  无法单独归因夏普下降来自哪一个；需补单变量实验。
- 九格只覆盖一个策略、一条 2014–2026 路径，未做多策略 / 多起点稳健性检验。
- 该实验**没有**证明「所有净值反馈都应该禁」；它证明的是在本引擎整手 +
  周度调仓 + 无成本模型的条件下，反馈会破坏资金规模不变性。

## 6. 复现要点与踩坑

- 官方 CLI：`.venv/bin/lianghua --backtest /tmp/cfg_exp_<name>.json > /tmp/bt_exp_<name>.json`，
  串行；P1/P2 用 `/tmp/exp_allocator.py <config> <output>` 在进程内换类。
- 官方输出结构：`{"run_id": ..., "intervals": [{"result": {"metrics": {...},
  "final_cash", "final_equity", "positions", "equity": [...], "position_unit": ...}}]}`；
  `metrics.total_return` 是小数（12.02 = 1202%）。
- **事件表口径**：`run_events.allocations` 里 `run_id` 带 `-interval-0001` 后缀，
  必须用 `run_id LIKE substr(run_id,1,12) || '%'`，exact match 查不到任何跳过原因。
- 运行期补丁初版把 `requested` 写成 `delta // lot`，订单股数少 100 倍，
  离线验证只投入 0.9%；正确写法 `delta // lot * lot`。
- 报告数据文件 `exp_summary.json` 的 `gaps_pp` 字段直接给出上述三个差值。

---

⚠️ 以上内容由 AI 基于公开信息整理生成，仅供参考，不构成任何投资建议或个股推荐。投资有风险，决策需谨慎。
