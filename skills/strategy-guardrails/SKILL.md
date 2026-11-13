---
name: strategy-guardrails
description: lianghua 项目的策略工程护栏知识库。在新增或修改 src/plugins/strategies/*.py 与根目录策略 config JSON 之前、设计 10万/50万/100万/1000万 资金规模对比之前，以及遇到策略读取 account.equity 或已实现净值做仓位缩放（_dilution、dilute_positions、NAV track）、跨资金档收益不一致、limits.adv_fraction 容量约束、整手取整 below_one_lot、净值路径依赖、现金占比异常等场景时，应加载本 skill。它强制执行禁止净值反馈、三档资金规模不变性验收、执行噪声不进入 alpha、容量约束只属大资金、单变量实验，以及只用 lianghua --backtest 的操作边界。
agent_created: true
---

# 策略工程护栏（lianghua）

## 目的

把用真实回测代价换来的策略工程教训固化为可复用规则，避免同一个坑反复付出十几个小时的
回测机时。本 skill 是**持续迭代的知识库**：遇到新反例时追加案例、修订规则、升版本，
不删旧证据、不覆盖旧结论。首版证据来自 R29「三个开关」实验，见 `references/case-r29.md`。

## 何时加载

- 新增或修改 `src/plugins/strategies/*.py`、根目录策略 config JSON **之前**。
- 设计任何实验来解释策略优劣，或比较不同 `initial_cash` 档位之前。
- 回测结果出现以下症状时：同策略不同本金收益差 > 50pp、现金占比异常、
  大量 `below_one_lot` / `order_above_adv_limit` / `inside_no_trade_band`、
  净值曲线在早期几乎重合但后期大幅分叉。
- 打算在策略里用「净值轨道 / 当前回撤 / 已实现收益」调整持股档数或敞口时。
- 打算改内核、allocator、数据仓或直接读写数据库之前（先读 G6）。

## 铁律（硬约束，违反即判策略不合格）

### G1 禁止净值反馈

策略 `generate()` 不得把 `account.equity`、`account.cash`、累计收益、当前回撤、
持仓浮盈，或策略自身维护的 NAV 序列，用作选股、权重、持股档数、敞口的输入。
只使用 `MarketFrame` 的横截面 / 时序信息与 `params`。唯一允许的例外是
`isfinite(equity)` 一类纯数值校验，且不得影响输出。

依据：R29 实测 `_dilution()` 读 `account.equity` 在 12→60 档之间切换，把整手取整的
微小权重偏差放大成分叉的组合路径，10 万 vs 100 万收益差 **372.7pp**；关掉反馈后
为 **−8.8pp**。这也是机构不把已实现净值接入 alpha 决策的原因：执行噪声只允许用
跟踪误差 / implementation shortfall 在 bps 层衡量，不允许回流成信号。

**配置层压平档位不能替代源码层移除。** 把 `dilute_positions` 压到 `max_positions`
后，`_dilution()` 仍通过 `slots` 与 `start = dilution × dilute_offset × max_start`
影响选股。R31 中同样压平，`f1` 的 G2 差距收敛到 8.82pp，`f2` 仍有 **147.63pp**。
判定 G1 时必须检查 `.py` 源码，不能只看 config。

### G2 资金规模不变性验收

每个新策略必须用 `initial_cash` = 10 万 / 100 万 / 1000 万三档各跑一次；
配置只允许改 `initial_cash` 与 `params.principal`，其余字段逐字段 diff 一致。
用 `scripts/check_scale_invariance.py` 判定：
**10 万 ↔ 100 万总收益差 > 50pp 或夏普差 > 0.05 → FAIL**。

FAIL 的策略不得进入排名、不得用于给出持仓结论，也不得把差距解释成
「路径运气」「本金效应」「小资金天然劣势」。差距是 bug 或设计缺陷的信号，
不是策略特征。

**PASS 必须逐策略实测，禁止由同族配置推断。** R31 中 `f1` 与 `f2` 仅差一个
`require_profit` 参数、同样压平档位，结果一个 PASS（8.82pp）一个 FAIL（147.63pp）。
每次换策略、换过滤条件、换参数都要重新跑三档。

### G3 执行噪声只在 bps 层衡量

整手取整、`below_one_lot`、不交易带、订单跳过属于执行层噪声，用现金拖累、
跟踪误差、implementation shortfall（bps）衡量。禁止把它变成 alpha，也禁止用它
解释策略优劣。R29 中改 allocator 把 10 万现金拖累 12.0% → 5.4%，
收益反而 940.3% → 921.0%、夏普 1.095 → 0.970：减小跟踪误差不等于提高收益。

### G4 容量约束只针对大资金

`limits.adv_fraction` 一类容量开关只在 1000 万档及以上评估。100 万档的 −105pp
与 1000 万档的 −565pp 是两种不同现象，不可混为一谈。当前实现是**整单跳过**，
没有部分成交、跨日拆单、冲击成本模型，因此结果只是保守上界，不是真实容量预测。
判读时必须同时看 `order_above_adv_limit` 次数与现金占比
（R29：6454 次 / 72.9% 现金）。

### G5 单变量实验

分离因素时一次只改一个变量：关反馈、开容量、改 allocator 各自独立跑。
禁止把 `lot_optimizer` 与 `no_trade_band` 绑进同一个补丁。
框架层修改必须先在配置层或运行期验证效果，再考虑落地；落地前先征求用户同意。

### G6 操作边界

只用 `.venv/bin/lianghua --backtest <config.json>` 跑回测，且**串行**执行。
禁止：并行多进程（DuckDB `catalog.duckdb` 单写者锁）、复制 catalog、创建影子项目、
软链 `data/parquet` 或 `data/source`、直接读写数据库、修改框架源码。
需要突破边界时先向用户请示。

依据：R28 曾为绕开单写锁创建影子项目并软链真实 parquet，触发
`storage._reconcile()` 把 manifest 未声明的 49 个日线分片当 stray 删除，
造成 1,221 只股票、6 个交易日行情丢失，需分批用官方 CLI 回填。

### G7 数据口径

详见 `references/engine-contract.md`。最低限度记住：
`close` 是原始未复权价（面值 / 价格过滤必须用它）；
`result.positions` 是**复权单位**，市值 = units × raw_close × backward_factor；
`run_trades.symbol` 是裸 6 位代码；`run_events.allocations` 的 `run_id` 带
`-interval-0001` 后缀，查询必须用 `LIKE rid[:12]||'%'`；
统计「有效持仓」按 ≥ 0.5% 净值过滤，碎股不计入。

## 标准工作流

1. **写前检查**：读 `references/checklist.md`，对将要复用的同族策略运行
   `python skills/strategy-guardrails/scripts/check_strategy.py <file.py>`，确认无 G1 违规。
2. **实现**：`generate(MarketFrame, AccountSnapshot) -> TargetSet` 只输出权重；
   权重有限、非负、合计 ≤ 1；`as_of` 早于 `next_session` 且单调递增；不输出股数 / 订单。
3. **跑基线**：由同一份配置派生三档，逐字段 diff，串行调用官方 CLI。
   固定 `delisting_haircut: "0"` 与统一的 `intervals`，避免口径漂移。
4. **判定**：`python skills/strategy-guardrails/scripts/check_scale_invariance.py \
   --case 10w=<bt_10w.json> --case 100w=<bt_100w.json> --case 1000w=<bt_1000w.json>`。
5. **FAIL 决策树**：
   - 先 grep G1 违规 → 在**配置层**把 `dilute_positions` 设为 `max_positions`（关反馈）重跑。
   - 差距消失 → 判定为策略层问题，修策略，不修内核。
   - 差距仍存在 → 查 `run_events.allocations` 的跳过原因，分离取整与容量。
   - 只有 1000 万档显著下滑 → 按 G4 判为容量效应，另做参与率 / 冲击模型。
6. **归档**：把新案例写入 `references/case-*.md`，按 `references/iteration.md`
   更新规则与 `CHANGELOG.md`。每一次「我以为 / 其实」都要留下可复现的 run_id 与配置哈希。

## 资源索引

| 文件 | 何时读 |
|---|---|
| `references/case-r29.md` | 需要引用原始证据、九格数据、机制链、配置片段时 |
| `references/case-r31.md` | 判断「配置层压平能否替代源码改造」、同族策略 G2 分化、双标准验收时 |
| `references/checklist.md` | 写策略前、提交前、验收时的逐项检查表与 grep 模式 |
| `references/engine-contract.md` | 需要内核 schema、整手规则、事件表口径、复权换算时 |
| `references/iteration.md` | 追加新案例 / 修订规则 / 升版本时 |
| `CHANGELOG.md` | 想知道某条规则来自哪次实验、何时加入 |
| `scripts/check_strategy.py` | 静态扫描策略源码 / config 里的净值反馈与危险参数 |
| `scripts/check_scale_invariance.py` | 直接读官方 `--backtest` 输出判定三档不变性 |
| `scripts/selftest.py` | 修改本 skill 或引擎 schema 后，用合成样例回归验证两个脚本 |

## 反模式速查

| 反模式 | 正确做法 |
|---|---|
| 用 `account.equity` 相对轨道决定持股档数 | 档数只由横截面 / 波动率等市场状态决定 |
| 「10 万差是本金小、买不到一手」 | 先跑关反馈对照，量化归因后再下结论 |
| 一上来改 allocator / 内核 | 先在配置层关反馈、单变量分离 |
| 把 1000 万下滑当策略失效 | 分开报容量效应与策略效应 |
| 并行跑多档节省时间 | 串行；单写者锁冲突会损坏数据仓 |
| 用 `positions × close` 算市值 | 按 G7 用复权因子还原 |

## 合规

本 skill 记录的全部收益、夏普、回撤均为模型回测结果，仅用于工程归因，不构成交易信号。

⚠️ 以上内容由 AI 基于公开信息整理生成，仅供参考，不构成任何投资建议或个股推荐。投资有风险，决策需谨慎。
