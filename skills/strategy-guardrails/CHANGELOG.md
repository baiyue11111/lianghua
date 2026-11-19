# CHANGELOG · strategy-guardrails

格式：`v版本 | 日期 | 类型 | 改动 | 证据`

---

## v0.1.0 | 2026-11-11 | 初始建库

首个版本，从 R29「三个开关」实验抽取。知识库骨架按标准 skill 结构建立：
`SKILL.md` + `references/` + `scripts/`，并在项目根目录 `skills/` 下作为单一事实源，
通过 `.workbuddy/skills/` 软链供 WorkBuddy 发现。

**新增铁律**

| 编号 | 内容 | 证据 |
|---|---|---|
| G1 | 禁止策略读 `account.equity` / 已实现净值做仓位缩放 | 关反馈后 10w↔100w 差距 372.7pp → −8.8pp |
| G2 | 三档资金规模不变性验收（>50pp 或夏普 >0.05 判 FAIL） | R28 四档扫描 + R29 九格 |
| G3 | 执行噪声只在 bps 层衡量，不进 alpha | 改 allocator 现金 12.04%→5.39%，夏普 1.095→0.970 |
| G4 | 容量约束只属大资金，整单跳过是保守上界 | 1000w 开容量 72.86% 现金 / 6454 次跳过 |
| G5 | 单变量实验，框架改动先验证再落地 | R29 三组开关 + P1/P2 补丁未拆变量 |
| G6 | 只用 `lianghua --backtest`，串行，不动数据仓 | R28 影子项目误删 49 个 parquet 分片 |
| G7 | `close` / 复权单位 / 裸码 / `run_id LIKE` / 碎股口径 | R27 持仓还原、R28 碎股误读、R29 事件查询 |

**新增资源**

- `references/case-r29.md`：九格完整数据、机制链、结论边界、复现要点。
- `references/checklist.md`：写前 / 实现 / 跑前 / 验收 / 措辞 / 收尾六段清单与阈值。
- `references/engine-contract.md`：策略 schema、配置字段、allocator 行为、数据口径、输出 JSON。
- `references/iteration.md`：案例命名、修订与推翻规则、版本号、待补案例。
- `scripts/check_strategy.py`：静态扫描策略源码 / config 里的净值反馈与危险参数。
- `scripts/check_scale_invariance.py`：读官方回测输出判定三档不变性。
- `scripts/selftest.py`：合成样例回归自检（5 项，全部通过）。

**脚本验证（真实 R29 数据）**：基线 10w/100w/1000w → FAIL（372.71pp）；
关反馈 10w/100w → PASS（8.82pp / 夏普差 0.0417）；
`exp_qf_h8f1.py` → FAIL（15 high / 4 medium）；`exp_qf_s1.py` → PASS；
E1 配置 → PASS、E3 配置 → FAIL（`dilute_positions=60 > max_positions=20`）。

**已知空白（待后续案例补齐）**：真实容量模型、`lot_optimizer` / `no_trade_band`
单变量拆分、多策略多起点验证、有成本模型下的 G1 适用性、退市证据覆盖影响。

---

## v0.2.0 | 2026-11-11 | 规则修订（新增案例）

证据：`references/case-r31.md`（Top5 双标准重跑，22 组 `lianghua --backtest`）。
交付：`/tmp/r31_out/index.html` + `results.csv` + `summary.json` + `r31_backtest.py`。

**修订（非推翻）**

| 规则 | 改动 |
|---|---|
| G1 | 补充「配置层压平 `dilute_positions` 不能替代源码层移除」；判定必须检查 `.py`，不能只看 config |
| G2 | 补充「PASS 必须逐策略实测，禁止由同族配置推断」 |
| 资源索引 | 新增 `case-r31.md` 触发条件 |

**触发修订的证据**

- 4 个组合原三段标准 3/3 达标：`f1/base` 1472.47%、`f2/base` 1446.65%、
  `f1/nodil` 1378.09%、`f2/nodil` 1335.36%；`h8/h9/h11` 六种配置均 1/3。
- G2：`f1/base` FAIL 372.71pp、`f2/base` FAIL 328.09pp、
  **`f1/nodil` PASS 8.82pp**、`f2/nodil` **FAIL 147.63pp**。
- 同样压平档位、只差 `require_profit=1`，结果分化；排除现金拖累（12.24% vs 11.60%）
  与整手取整（10 万档 `below_one_lot` 仅 46 次）。
- 双标准全过 0 个；最接近 `f1/nodil`（仅差源码层 G1）。

**新增待补案例**：`slots` / `start` 对 `dilution` 的依赖分别置零，
做单变量归因；验证「窄股票域放大残余净值反馈」的推断。

**自检**：`quick_validate.py` 通过；`selftest.py` 5/5；报告经无头浏览器截图校验，
表格与 KPI 一致、无乱码。

---

## v0.2.0 | 2026-11-17 | 新增检查项（minor）

跨领域核实后把两个经典问题变体的成熟方案落进护栏：数据清洗 = Point-in-Time /
双时态 + shift-left；静态守卫 = 污点分析（source→sink）+ defense in depth。

**新增文件 / 能力**

| 项 | 内容 | 证据 |
|---|---|---|
| `references/external-scanners.md` | lookahead-lint LA001–LA007、leakguard-mcp LG001–LG010 完整规则清单；直接复用 vs 需改造对照；四层放置模型 | 两个工具的官方仓库 README + leakguard benchmark（49 片段，precision 90.4% / recall 100%，作者标注语料自编只作回归夹具） |
| `check_strategy.py` | 新增 AST taint 引擎：追踪 `account.equity/cash` 及 `account` 别名 → 局部变量 → `TargetSet`/权重 sink；新增 `--no-ast` 做 A/B 对照；解析失败报 medium 不静默跳过 | 跨领域 taint analysis / information flow control；`--strict` 与退出码语义保持向后兼容 |
| `selftest.py` | 5 → 8 项：别名泄漏 AST 必抓、regex-only 漏报（A/B 证明）、isfinite 纯校验不误报 | 8/8 通过 |

**关键设计决策：只加不减**

AST 命中只新增 high，**不把 regex 的 high 降级**。实证反例 `exp_track_a2.py`：
`account.equity → nav → self.state["returns"] → exposure → weights` 走列表/字典成员，
taint 引擎不追踪容器而漏报，但 regex 抓到那一行。曾实现「AST 证明清洁则降级」，
实测发现会把该真违规降为 medium，已删除。凡静态引擎的「未发现」≠「证明安全」。

**边界**：不追踪容器成员、跨函数状态、跨文件继承；AST PASS 不是 G1 合规证明；
唯一行为级判定仍是 G2 三档不变性。详见 `external-scanners.md` 第 5 节。

**实测**：全策略目录 125 文件，regex+AST 223 high / 85 medium（旧 regex-only 180 high / 74 medium），
新增 high 均为 `G1-TAINT-*`，抽查 `exp_alpha_b1` / `exp_qf_h8f1` 确为净值派生权重，真阳性。

## v0.2.1 | 2026-11-20 | 修正文案

失败决策树改用源码移除反馈做对照。依据 `references/case-r31.md` 中压平档位后仍存在的反馈路径。
