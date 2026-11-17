# 外部静态扫描器：可复用规则清单

跨领域核实的产物。两类现成工具都把「静态分析是 heuristic 不是 proof」写进设计，
与本项目 G1/G2 的判定哲学一致：报告可疑点，不当场证明意图。

规范路径 `skills/strategy-guardrails/references/external-scanners.md`（`.workbuddy/` 下是软链）。

## 1. lookahead-lint（PyPI，MIT，零依赖）

仓库 `github.com/bmouler/lookahead-lint`，v1.0.1。纯 `ast` 解析，不导入不执行；
单遍遍历，规则码稳定不重编号，JSON 输出带 `schema_version`。

| 码 | 名称 | 命中 | 故意沉默 |
|---|---|---|---|
| LA001 | negative-shift | `.shift(-n)` / `.shift(periods=-n)` | `shift(1)`、变量 n（猜不出正负就不报） |
| LA002 | backward-fill | `.bfill()`、`fillna(method='backfill')`、`interpolate(limit_direction='both')` | `.ffill()`、前向 method |
| LA003 | centered-window | `rolling(..., center=True)` | `center=False`、变量 |
| LA004 | fit-before-split | 同一 scope 内 `.fit(` 出现在首个 `train_test_split(` 之前 | split 之后 fit、无 split、嵌套函数内 fit |
| LA005 | shuffled-split | `train_test_split(...)` 未钉死 `shuffle=False` | 显式 `shuffle=False`、转发 `**kwargs` |
| LA006 | future-row-index | 循环 `for i` 内出现 `x[i + k]` | `x[i]`、`x[i-1]`、非循环变量 |
| LA007 | forward-asof-merge | `merge_asof(direction='forward'/'nearest')` | `backward`（默认） |

抑制用行尾注释 `# lookahead-lint: ignore[LA001]`，靠 `tokenize` 读，字符串/docstring 里的同款文本不生效。
支持 `--format json|github`、`.ipynb` 逐 cell 定位、`[tool.lookahead_lint]` 配置。
退出码 0 无发现 / 1 有发现 / 2 用法或解析错误（解析失败不静默跳过——覆盖率的正确默认值）。

对本项目：**时序错位类规则 LA001–LA003、LA006–LA007 与 G1 正交**，本项目策略直接吃
`MarketFrame.history`，不在这层拼 pandas；但对 `tools/` 下的迁移脚本、`data/source` 的
研究 notebook 有直接价值。LA004/LA005 只在自建 ML 流程时启用。

## 2. leakguard-mcp（PyPI，MIT，libcst）

仓库 `github.com/doazvjettu/leakguard-mcp`，本地 stdio MCP + CLI，代码不出机器。

| 码 | 严重度 | 命中 | 修复 |
|---|---|---|---|
| LG001 | 🔴 | `shift(-n)` / `diff(-n)` / `pct_change(-n)` | 改 lag：`shift(n)` |
| LG002 | 🔴 | `rolling(center=True)` | 去 center |
| LG003 | 🔴 | 全样本 `StandardScaler().fit` / 手写 mean-std | split 后 fit，或 `expanding()` |
| LG004 | 🔴 | `train_test_split` 默认 / `KFold` / `cross_val_score(cv=n)` | `shuffle=False` |
| LG005 | 🔴 | 未来派生标签列被复用为特征 | 分离标签与特征 |
| LG006 | 🟡 | 全历史 `.max()` / `.mean()` 当特征 | 滚动窗口 |
| LG007 | 🔴 | `bfill()` / `fillna(method='bfill')` | 前向填充 |
| LG008 | 🔴 | `merge_asof(direction='forward'/'nearest')` | `backward` |
| LG009 | 🟡 | bar 时间戳 resample 的 label/closed 错配 | 对齐到完成时点 |
| LG010 | 🟡 | `groupby().transform()/agg()` 跨 train/test | 在窗口内聚合 |

自带 benchmark：49 片段，综合 precision 90.4% / recall 100%；作者诚实标注语料由自己编写，
只能当回归夹具（regression fixtures），不是独立验证。已知 FP：负 shift 只作标签、helper 内 fit、
非时序数据 shuffle、绘图用 resample。已知 FN：`df.loc`/`assign` 传播的污点、`cv` 省略默认值、
**所有跨函数/跨文件的数据流**（与本项目 G1 检测的天花板是同一个）。

对本项目：LG001/LG003/LG007/LG008 与 LA001/LA003/LA002/LA007 高度重叠；
LG005/LG006/LG009/LG010 是 lookahead-lint 没有的补充。**先装 lookahead-lint**：零依赖、
MIT、输出稳定、抑制语义可审计；需要 LG005/009/010 再叠加 leakguard。两者都不该进 `pyproject.toml`
运行时依赖，只做开发期工具。

## 3. 直接复用 vs 需要改造

| 能力 | 直接复用 | 需改造 | 本项目现状 |
|---|---|---|---|
| 未来函数 / 时序错位 | LA001–LA007、LG001–LG010 | 无 | G1 不涉及（策略层无 pandas 特征工程） |
| 净值反馈（G1） | 无现成规则 | source→sink 定制 | `check_strategy.py` regex → 本次升级为 regex + AST taint（只加不减） |
| 资金规模不变性（G2） | 无现成规则 | 本项目的独有实验 | `check_scale_invariance.py` 已有 |
| 数据清洗时点对齐 | 无静态工具可做 | 数据层 bitemporal / as-of 视图 | `data/source` 只含现役股，待补 |

**边界**：外部工具检测的是「代码里写了什么」，G2 检测的是「跑出来怎么样」。
两者不可互相替代——正则/AST 一律无法证明 scale invariance，三档回测也无法定位是哪一行的净值回流。

## 4. 放置层次（对应 SKILL.md 标准工作流）

1. 编辑期：外部扫描器 + AST 守卫，并行、秒级、无需回测机时。
2. 加载期：`generate()` 前 hook，对本批要跑的策略做静态门禁，FAIL 直接不排队。
3. 运行期：Design-by-Contract 断言（weights 有限/非负/≤1、`as_of` 单调）——静态证明不了的部分。
4. 回测后：`check_scale_invariance.py`，行为级验收，唯一能判 G2 的手段。

每层失败都写进 `case-rNN.md`；不要把「静态 PASS」当成「策略合格」。

## 5. 本项目 AST taint 引擎的已知边界

与 leakguard v1 的诚实边界同构，必须写进判定措辞：

- **只加不减**：AST 命中只会新增 high，**永不**把 regex 的 high 降级。
  实证反例 `exp_track_a2.py`：`account.equity → nav → self.state["returns"] →
  exposure → weights` 走的是列表/字典成员，taint 引擎不追踪容器成员而漏报，
  但 regex 抓到了那一行。若允许 AST「证明清白」就会掩盖真违规。
- **不追踪**：`list.append`、`dict[key]=`、跨函数状态、闭包、跨文件继承
  （checklist 已提示同族基类的 `account.equity` 要单独扫基类）。
- **不证明**：AST PASS 只表示「没发现可达污点」，不是 G1 合规证明；
  唯一的行为级判定仍是 G2 三档不变性。
- 解析失败报 medium 而非静默跳过（与 lookahead-lint 的覆盖率策略一致）。
