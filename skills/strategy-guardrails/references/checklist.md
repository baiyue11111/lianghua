# 检查清单：写策略前 / 跑之前 / 验收时

按顺序逐项打勾。任何一项 FAIL 都不得进入策略排名或持仓结论。

## A. 写策略之前

- [ ] 已读 `SKILL.md` 的 G1–G7，并读过至少一个 `references/case-*.md`。
- [ ] 明确本次改的是策略层还是框架层；框架层改动先请示用户（G6）。
- [ ] 若复用同族策略，先跑静态扫描：
      `python skills/strategy-guardrails/scripts/check_strategy.py src/plugins/strategies/<base>.py`
      同族基类里的 `account.equity` 会被继承，不要只看自己新增的行。
- [ ] 已确认数据口径：面值过滤用 `close`（未复权），不用 `signal_close`。

## B. 实现阶段

- [ ] `generate(frame, account) -> TargetSet | None`；只输出权重，不输出股数 / 订单 / 价格。
- [ ] 权重全部有限、非负、合计 ≤ 1；无杠杆、无做空。
- [ ] `as_of` 早于 `next_session` 且单调递增；不使用未来数据、不识别区间边界。
- [ ] **没有任何**净值 / 回撤 / 已实现收益 / 浮盈进入选股、权重、档数、敞口。
- [ ] 不使用退市日期、指数成分未来快照等事后信息。
- [ ] 一 `.py` 对一 `.json`；`initial_cash` 与 `params.principal` 是同一数值。

**危险模式 grep（在项目根执行）**

```bash
# G1：净值反馈（macOS grep 偶尔把源码判为二进制，改用编辑器全局搜索兜底）
grep -Rn "account\.equity\|account\.cash\|nav_values\|nav_dates\|_dilution" src/plugins/strategies/
# 档位放大开关
grep -Rn "dilute_positions\|track_slope\|track_band\|min_exposure" src/plugins/strategies/
# 配置里是否悄悄留着放大档位
grep -Rn '"dilute_positions"' *.json
```

命中不等于一定违规：仅用于 `isfinite` 校验、或 `dilute_positions == max_positions`
（等价于关闭）是允许的；其余一律按 G1 处理。

## C. 三档回测之前

- [ ] 三份配置由同一份派生，只改 `initial_cash` 与 `params.principal`；
      用 `diff` 或脚本逐字段核对，确认 `delisting_haircut`、`intervals`、
      `limits`、全部 `params` 一致。
- [ ] `delisting_haircut: "0"` 口径固定，P1/P2/P3 截止日统一，避免样本漂移。
- [ ] **串行**执行，不并行、不后台并发跑多个 `--backtest`（DuckDB 单写锁 / G6）。
- [ ] 输出重定向到独立 JSON，保留 run_id 与 config hash。

```bash
.venv/bin/lianghua --backtest cfg_10w.json   > bt_10w.json
.venv/bin/lianghua --backtest cfg_100w.json  > bt_100w.json
.venv/bin/lianghua --backtest cfg_1000w.json > bt_1000w.json
```

## D. 验收（强制）

- [ ] 跑 `python skills/strategy-guardrails/scripts/check_scale_invariance.py \
      --case 10w=bt_10w.json --case 100w=bt_100w.json --case 1000w=bt_1000w.json`，
      结果为 PASS。
- [ ] 人工复核首尾各 5 条成交、成交笔数、现金占比，确认不是数据缺失造成的假象。
- [ ] 查 `run_events.allocations` 跳过原因分布；`run_id` 用 `LIKE` 前缀匹配（G7）。
- [ ] 若 1000 万档单独下滑，按 G4 判为容量效应，另起容量实验，不并入策略评分。
- [ ] 若 FAIL，按 SKILL.md 的「FAIL 决策树」处理，并把过程写入新案例。

**默认阈值**（可被用户显式覆盖，覆盖时须记录理由）

| 指标 | 阈值 | 含义 |
|---|---:|---|
| 10w ↔ 100w 总收益差 | ≤ 50pp | 超过即违反资金规模不变性 |
| 10w ↔ 100w 夏普差 | ≤ 0.05 | 噪声带上限 |
| 1000w 现金占比 | 记录并解释 | 超 30% 视为容量主导，需单独标注 |
| 1000w `order_above_adv_limit` | 记录并解释 | 整单跳过次数，非真实容量预测 |

## E. 结论措辞

- [ ] 用「在本回测中该策略产生 X 结果」而非「应该买 / 卖」。
- [ ] 不把单档最优收益当策略真实预期；报告跨档中位数与离散度。
- [ ] 明确列出局限：单策略 / 单路径、退市证据覆盖不全、无滑点与冲击成本、
      `adv_fraction` 为保守上界。
- [ ] 所有交付带免责声明。

## F. 收尾

- [ ] 把「我以为 / 其实」写进 `references/case-rNN.md`，附可复现命令与 run_id。
- [ ] 更新 `CHANGELOG.md` 与 `references/iteration.md` 规定的版本。
- [ ] 项目源码零改动的实验也要记录：运行期补丁 ≠ 可上线方案。
