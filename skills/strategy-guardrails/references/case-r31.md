# 案例 R31：双标准重跑——配置层压平不能保证 G2 PASS

> 日期 2026-11-11。首个用于**修订 G1 / G2 表述**的案例。
> 数据：`/tmp/r31_out/`（`stage1.json` / `stage2.json` / `results.csv` /
> `summary.json` / `index.html` / `r31_backtest.py`）。

## 1. 待解释的现象

用户要求按两套标准重新回测 Top5：
A = 原三段（haircut=0，P1/P2/P3 均跑赢纳指且夏普 > 1）；
B = 新护栏（G1 源码无净值反馈 + G2 三档资金不变性 PASS）。

R29 只在 `f1` 上验证过「关净值反馈 → 差距从 372.7pp 降到 −8.8pp」。
待验证的问题是：**这个结论能否外推到同族其他策略？**
候选解释：
1. 净值反馈是唯一主因 → 所有压平档位的策略都应 G2 PASS；
2. 整手取整 / 现金拖累是主因 → `below_one_lot` 次数与 G2 差距正相关；
3. 残余 `dilution` 经 `slots` / `start` 仍影响选股，且被窄股票域放大 →
   同族策略会出现分化。

## 2. 实验设计

- 5 个策略 × 2 变体 = 10 组三段回测；`nodil` 与 `base` 的**唯一差异**是
  `params.dilute_positions: 60 → 20`（等于 `max_positions`），逐字段 diff 校验通过。
- 三段口径：P1 2014-2017 / P2 2018-2021 / P3 2022-2026（统一截断 2026-10-13）、
  `delisting_haircut="0"`、纳指基准、`initial_cash=1000000`。
- 对 A 标准达标的 4 个组合，用连续单账户 2014-01-02 → 2026-11-09 跑
  10 万 / 100 万 / 1000 万三档，只改 `initial_cash` 与 `params.principal`。
- 合计 **22 组**，全部 `rc=0`，全部串行、只走 `lianghua --backtest`；
  三段单组约 6–7 分钟，连续三档单组约 10 分钟。
- 覆盖：4 个达标候选全部完成 G2 验收（阶段二曾限 3 个，已补跑第 4 个，
  `capped=False`）。1/3 段达标的 6 个组合按双标准直接淘汰，未做 G2，属设计内截断。

## 3. 结果

**标准 A（三段）**：4 个组合 3/3 达标，`h8/h9/h11` 六种配置均只有 1/3。

| 组合 | P1 | P2 | P3 | 链式总收益 |
|---|---:|---:|---:|---:|
| f1/base | 148.14% S1.221 | 152.08% S1.323 | 151.39% S1.068 | 1472.47% |
| f2/base | 156.61% S1.277 | 138.48% S1.254 | 152.73% S1.087 | 1446.65% |
| f1/nodil | 129.81% S1.073 | 162.63% S1.366 | 144.90% S1.052 | 1378.09% |
| f2/nodil | 139.55% S1.153 | 139.84% S1.262 | 149.83% S1.052 | 1335.36% |

纳指基准：P1 +66.41% / P2 +128.03% / P3 +72.09%（用 `benchmark.points` 末点
`normalized − 1` 反算，与 R25 已知值逐位一致）。

**标准 B**：10 组的 G1 **源码层全部 FAIL**（五个 `.py` 都读 `account.equity`
且保留 `_dilution()`）；配置层 `base` FAIL / `nodil` PASS。

| 组合 | 10 万 | 100 万 | 1000 万 | 10w↔100w | G2 |
|---|---:|---:|---:|---:|---|
| f1/base | 940.29% | 1313.00% | 1078.75% | **372.71pp** | FAIL |
| f2/base | 828.87% | 1156.96% | 1137.40% | **328.09pp** | FAIL |
| f1/nodil | 1202.05% | 1193.23% | 1333.62% | **8.82pp** | **PASS** |
| f2/nodil | 1021.89% | 1169.52% | 995.32% | **147.63pp** | FAIL |

`f1/base` 三段重跑与 R25 旧结果逐位一致（差 0.0000pp），
确认工作区 `src/market/reader.py` 的未提交改动不影响数值，重跑口径未漂。

**双标准全过：0 个。** 最接近达标：`f1/nodil`
（A 3/3 + 配置层 G1 PASS + G2 PASS，仅差源码层 G1）。

## 4. 关键发现：R29 的结论不能无条件外推

**同样压平档位，f1 PASS 8.82pp，f2 仍 FAIL 147.63pp。**
f2 与 f1 的配置唯一差异是 `require_profit=1`（叠加净利润 / ROE 过滤）。

排除掉两个直觉解释：

| 候选解释 | 证据 | 判定 |
|---|---|---|
| 现金拖累 | f2/nodil 三档现金 12.24% / 11.60% / 11.55%，几乎一致 | 排除 |
| 整手取整 | 10 万档 `below_one_lot` 仅 46 次（f1/nodil 反而有 86 次），100 万档 0 次 | 排除 |

事件表查询口径：`run_events.allocations` 的 `value` 是 JSON，
跳过原因在 `value.decisions[].reason` + `status != 'ordered'`；
`run_id` 需 `LIKE substr(run_id,1,12) || '%'`。

**当前最佳解释（未证实）**：压平 `dilute_positions` 后 `_dilution()` 仍通过
两处影响输出：

```python
slots = round(base_slots + dilution * (20 - base_slots))     # base_slots 由波动率动态决定，常 < 20
start = round(dilution * dilute_offset * max_start)          # dilute_offset = 0.78
```

`dilution` 来自 `account.equity` 相对净值轨道，不同资金档净值路径略有差异 →
`start` 位移 → 换股。f2 的可交易域更窄，同样的 `start` 位移换掉更大比例持仓，
于是放大成 147.63pp 的分叉。f1 域宽，位移被稀释，恰好落在 8.82pp。
**该推断尚未做单变量实验**（需把 `slots` 与 `start` 对 `dilution` 的依赖分别置零）。

## 5. 结论与边界

**成立：**
- 达标不依赖净值反馈：压平档位后 f1 −94pp、f2 −111pp，但三段仍 3/3。
- 配置层压平**不能保证** G2 PASS；G2 必须逐策略实测，不能由「同族 + 同配置」
  推断。
- G1 定为**源码层硬约束**是对的：只靠配置绕过，会留下 f2 这类隐藏的路径依赖。
- 新护栏能把「收益好看但资金规模一变就崩」（f1/base +1472% → FAIL 372pp）
  与「收益略低但稳健」（f1/nodil +1378% → PASS 8.8pp）区分开。

**不成立 / 不能外推：**
- 不能说「f1/nodil 是可用策略」：它源码层 G1 FAIL，且只跑了一条 2014–2026 路径。
- 不能说「窄股票域导致路径依赖」已证实；`slots` / `start` 的归因需补实验。
- 1000 万档与 100 万档的差（f1/nodil 140.39pp、f2/nodil 174.2pp）属容量效应，
  按 G4 单独判读，未计入 G2 的 FAIL。
- 只覆盖 5 个策略、22 组回测；`h8/h9/h11` 未做三档验收（A 标准已淘汰）。
- 三段是独立注资 / 实例重置，G2 是连续单账户，两种口径不可混算。

## 6. 复现

```bash
.venv/bin/python /tmp/r31_pipeline.py      # 10 组三段 + 前 3 个候选三档
.venv/bin/python /tmp/r31_stage2b.py       # 补跑第 4 个候选 f2/nodil
.venv/bin/python /tmp/r31_report.py        # 汇总 CSV / JSON / index.html
```

- 配置由根目录 `exp_qf_h8f1.json` 等 5 份派生，落在 `/tmp/r31_out/cfg_*.json`。
- 三档判定用 `scripts/check_scale_invariance.py --json`，
  阈值 50pp / 夏普差 0.05。
- 踩坑：WorkBuddy 的 shim 对已存在的 `/tmp` 目录执行 `Path.mkdir(exist_ok=True)`
  仍会抛 `FileExistsError`，必须写成 `if not p.exists(): p.mkdir(parents=True)`。

## 7. 对规则的修订

| 规则 | 修订 |
|---|---|
| G1 | 补充「配置层压平不等于源码合规」，并引用本案例说明同族策略会分化 |
| G2 | 补充「PASS 必须逐策略实测，禁止由同族配置推断」 |
| 待补清单 | 新增 `slots` / `start` 单变量归因实验；移除已完成的「多策略验证 G1 普适性」 |

---

⚠️ 以上内容由 AI 基于公开信息整理生成，仅供参考，不构成任何投资建议或个股推荐。投资有风险，决策需谨慎。
