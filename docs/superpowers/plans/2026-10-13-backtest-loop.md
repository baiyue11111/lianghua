# Backtest Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 固定原子回测循环，收拢公共计算，提供可选策略参数更新和独立验证。

**Architecture:** 协议查询数据及保存结果，Kernel 执行固定循环，Calculation 提供纯计算，策略和参数更新器只通过标准输入输出工作。原子回测是基本能力，搜索复用原子执行而非第二套实现。

**Tech Stack:** Python 3.12+、dataclass、typing.Protocol、unittest；现有 DataBus、策略加载与 Settlement。

**Spec:** `docs/superpowers/specs/2026-10-13-backtest-loop-design.md`

## Global Constraints

- `src/protocal.py` 是唯一后端公共入口和数据协议总线；核心不导入外层模块。
- 不修改 frontend/，不启动端口，不提交或推送未授权的既有改动。
- 不增加 fallback、兼容 wrapper、按策略名分支或自动失败重试。
- 保存当前策略规则、预热、None 无新决策、空目标清仓及固定 slots 分母。
- 固定参数单次执行不创建更新器；每次试验使用新的策略及结算状态。
- 首先写失败测试，记录 RED，再最小实现和 GREEN；报告具体命令及数量。
- 不提前实现未使用的优化算法，不重写数据/结算插件；共享计算只有 core/calculation.py 的 Calculation 公共类。

## Review Focus

- 评价输入含 NaN、bool、无序日期或非正权益：明确拒绝，不能静默传播。
- 稀疏选股保留固定 slots：两标的、八 slots 必须分别 0.125，不得自动改为 0.5。
- 未成交订单与账户状态不得串到下一试验；默认单次及搜索一候选结果相同。
- 更新器或后续试验失败：释放锁，保留已完成试验证据；不写成功搜索记录。
- 验证区间重叠或验证改变排序：拒绝重叠，验证结果永不反馈更新器。

## Task 1: 公共计算与原子回测评价

**Files:**
- Create: src/core/calculation.py、tests/test_calculation.py
- Modify: src/core/backtest.py、src/core/kernel.py、src/protocal.py、src/application.py、src/audit.py、src/history_data.py、pyproject.toml
- Modify: src/plugins/strategies/{moving_average_cross,quality_trend,era_leadership,trend_pullback,breadth_low_amount_reversal,_ranked_signal}.py
- Migrate tests: tests/test_evaluator.py、tests/test_core_plugins.py 及相关输入输出测试
- Remove redundant implementation after migrating callers: src/evaluator.py、src/plugins/allocators/equal_weight.py 及仅重导出它的包；更新打包清单，不留旧入口。

**Interfaces:**
- Calculation.sma(values, window: int) -> float；使用原有 sum/窗口公式以保持浮点行为。
- Calculation.equal_weights(symbols, slots: int, gross: float = 1.0) -> dict[str, float]；保留分母 slots，调用处显式处理空选中列表。
- Calculation.evaluate(points, *, periods: int = 252, baseline: float = 0.0) -> dict[str, float | int | None]；baseline 是逐期简单基准收益。Sharpe 用差额收益，波动率仍用原收益；默认结果与旧实现完全一致，包括负号回撤和零波动空指标。
- BacktestRequest 增加 evaluation_parameters 不可变映射；只允许 periods 和 baseline，严查 types/finite；默认 {}。
- BacktestResponse 增加 metrics 不可变映射；Kernel 构造器增加 keyword-only calculation=Calculation 可注入对象，run 调用它的 evaluate。
- 协议请求可选 evaluation 对象；_result_data 只读取 response.metrics；配置哈希记录有效评价口径。
- save_backtest 的 manifest 复用已序列化的 effective_config，不能漏掉 evaluation 和实际插件口径；不再在保存时重新拼一份不同配置。

- [x] Step 1：先抓取改造前 synthetic history fixture 默认单次结果（权益、成交、指标）；测试冻结值，不只比较新实现自身。
- [x] Step 2：失败测试：sma([1,2,3],2)==2.5；equal_weights(('A','B'),8)=={'A':.125,'B':.125}；[100,120,90] 收益=-.1、回撤=-.25；拒绝 bool/NaN/短窗口/无序日期/零权益。
- [x] Step 3：实现公共类，迁移当前生产调用和对应测试，移除重复入口。保留策略缺失值处理和选股排序，不迁移策略判断。
- [x] Step 4：增加可注入评价及协议 evaluation 输入。一个可替换评价对象在 Kernel 单次被调用一次；更改 periods/baseline 改指标与 config_hash，权益与成交不变。默认请求不含 search 时仅创建一次策略及结算。
- [x] Step 5：运行模块测试与真实 fixture AB。运行命令：PYTHONPATH=src:. .venv/bin/python -m unittest tests.test_calculation tests.test_evaluator tests.test_core_plugins tests.test_core_backtest tests.test_strategy_port tests.test_moving_average_cross tests.test_task3_protocol tests.test_backtest_check -q。
- [x] Step 6：将 changed files、RED/GREEN、AB 核对及顾虑写入本任务报告；不提交整个脏工作区。

## Task 2: 可选更新器循环与协议接入

**Files:**
- Modify: src/core/backtest.py、src/core/interfaces.py、src/core/kernel.py、src/core/calculation.py、src/protocal.py、docs/backend-protocol.md
- Create: tests/test_kernel_search.py、tests/test_search_protocol.py

**Interfaces:**
- Trial：不可变 request: BacktestRequest、response: BacktestResponse、score: float。
- SearchResponse：trials: tuple[Trial,...]、best_index: int、validation: BacktestResponse | None。
- ParameterUpdater.__call__(trials: tuple[Trial,...]) -> Mapping[str, Scalar] | None；核心接口，不依赖协议/插件目录。
- Calculation.propose(trials, *, candidates) 返回 candidates[len(trials)] 的副本；耗尽返回 None；空列表非法。候选验证包含各项 mapping/scalar 类型，不能把非法空字符串或 bool 值悄悄数值化。这是显式候选遍历，不宣称梯度或贝叶斯优化。
- 协议更新器声明为 {module: 'core.calculation:Calculation', method: 'propose', params: {candidates: [...]}}；只允许本项目 core.calculation 或 plugins.* 下的实际本地模块，不允许任意系统/外部模块、私有模块或私有类。解析 module:Class、取公开 staticmethod/classmethod，signature.bind 检查 trials + params，再用 functools.partial 绑定参数。未知参数、不存在的方法、私有方法和非 callable 拒绝。不创建别的固定搜索适配器；另一插件类的相同方法签名可以直接替换，不更改协议或 Kernel。没有特殊的 candidates 输入路径。
- Kernel.search(request, updater, *, max_trials: int, metric: str, direction: str, validation: BacktestRequest | None = None) -> SearchResponse。
- run/search 共享私有 _run，不复制回测循环。run/search 都获取同一非阻塞锁，search 持锁包含 updater 与验证；finally 无条件释放。
- 每个试验有效参数 = 原始 request.parameters 与候选覆盖合并。identity 为原 run_id + '-trial-' + 从 1 起的四位序号；验证 identity 为原 run_id + '-validation'。不允许 updater 覆盖数据区间、现金或身份。
- max_trials 必须正 int 非 bool；metric 必须是评价返回的有限数值非 bool；direction 只 max/min；同分保留首个。None 在首次产生前返回属于失败。
- validation 必须同 strategy/dataset 和其他输入口径，仅时间区间不同，start.date() > 搜索 end.date()；自动使用 best 参数，不采纳验证请求自带的策略参数。不能反馈给 updater。
- 搜索异常携带 completed_trials（tuple）和原子失败上下文/试验请求，让协议统一保存已完成结果与失败证据。不继续执行其他候选或验证。

**Protocol payload:**

```json
{
  "module": "plugins.strategies.moving_average_cross",
  "params": {"fast_window": 2, "slow_window": 3},
  "dataset_id": "fixture", "start": "2020-01-31", "end": "2020-02-03",
  "initial_cash": "100000",
  "search": {
    "updater": {"module": "core.calculation:Calculation", "method": "propose",
      "params": {"candidates": [{"fast_window": 1}, {"fast_window": 2}]}},
    "max_trials": 2, "metric": "total_return", "direction": "max",
    "validation": {"start": "2020-02-04", "end": "2020-02-04"}
  }
}
```

search 缺省为单次；显式 null 非法。search 只允许上面字段，validation 可省略；内置更新器 candidates 必须非空参数字典列表，scalar 值由 BacktestRequest 及策略 Params 再校验。未知字段拒绝。复用原 UiMessage resource 信封，数据含 search: {metric,direction,best_run_id,trials,validation}，顶层保留最佳回测的已有字段，但 id/run_id 使用基础请求身份。完整试验结果使用 Task 1 的 _result_data，逐个 save_backtest 到唯一 DataBus；失败也保存 completed_trials 并返回 problem。基础身份保存搜索 summary，有效参数使用最佳参数，effective_config 包含完整 search 声明及基础参数，重新计算配置 hash；不能覆盖最佳试验自己的记录。

- [x] Step 1：先 RED：默认单次次数=1、updater 次数=0；两候选明确输出两个独立试验；一候选与原子回测权益/成交/指标一致。
- [x] Step 2：试验分数 1/3/2 时 max 选择第二项、min 选择第一项；同分保留首项；改为另一个策略和任意 scalar 参数也能工作，不按 MA 字段分支；使用第二种 updater 类方法经完整协议运行，证明替换不改协议。
- [x] Step 3：实现标准数据结构、共享执行循环、候选更新方法。测试 updater 第二次只能看到第一个已完成试验；stateful 策略和 settlement 每次实例不同；更改历史窗口重新 query。
- [x] Step 4：验证只能在最后执行一次；updater 收不到验证分数。重叠区间、空候选、bool 次数、非法参数/指标、NaN/None 分数及未知字段明确失败。
- [x] Step 5：经真实 frontend.ui-protocol Request 调用 Protocol；单次输出仍可读取；两候选记录出现在 data/runs 并能经 workbench.runs/run 查询。复用现有协议与 store，不另开入口或改前端。
- [x] Step 6：在 updater 阻塞时 atomic/search 都 busy；在 updater、候选和验证三个失败阶段均释放锁。后续候选失败必须保留前面成功记录并保存失败证据；异常后另一次原子回测成功。
- [x] Step 7：GREEN：PYTHONPATH=src:. .venv/bin/python -m unittest tests.test_kernel_search tests.test_search_protocol tests.test_core_backtest tests.test_task3_protocol tests.test_moving_average_cross tests.test_ui_protocol -q；然后运行受修改影响的 Python 测试，compileall 和 git diff --check；仅只读检查 frontend 协议，不构建不相关 UI。
- [x] Step 8：文档给单次、搜索与独立验证示例，准确标明当前内置只是候选遍历；将所有验证与改动清单写入报告，主代理复核和独立行为验证。

## Final verification — 2026-10-13

主代理修复后重跑：Python 完整测试 484 PASS；前端协议检查及 39 项 runtime/protocol 测试 PASS；compileall 与 git diff --check exit 0；frontend diff 为空。
独立代码审查 spec/quality PASS；独立真实协议行为及入口文档审查 PASS。第二种本地 updater 使用测试期注册的公开 staticmethod，不增加生产算法。默认新旧工程样本 AB 完全一致；单候选搜索与原子回测一致。工程验证不代表策略经济有效性。
本轮没有提交、推送或启动端口。既有未提交改动保留；任务报告及 patch 证据保存在 .superpowers/sdd/2026-10-13-backtest-loop/。后续按用户清理要求已删除重复源码快照。

后续真实项目检查：生产协议调用 history_holdout 返回 run_failed，数据清单缺 universe_source；tools/prepare_history.py 未生成该字段。以上工程验收不等于当前生产数据链路已通过。
