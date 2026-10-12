# Price Query and Trading Session Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkboxes.

**Goal:** 修通真实价格数据链路、可调日期/证券、可选基准及交易日买卖策略。

**Architecture:** protocal.py 定义数据合同、装配一个 DataBus 并分发；Kernel 固定驱动；数据准备/历史读取共享合同；结算插件实现通用时机；独立策略文件仅输出目标。

**Tech Stack:** Python、dataclass、unittest、现有 JSON/Parquet 数据与账户插件。

**Spec:** docs/superpowers/specs/2026-10-13-price-session-design.md

## Global Constraints

- 不改 frontend/、不提交推送、不占用端口、不覆盖无关脏改动。
- 唯一公共协议 src/protocal.py，数据经共享 DataBus/DataPort；禁止第二个存储实例和 fallback。
- 不按 strategy_id 分支；不伪造来源、指数、成交和历史可知性。
- 股票价格必需；本金使用 initial_cash；查询 symbols=None 指数据集完整范围。
- 新策略使用正式 plugins.strategies 目录；无需迁移旧策略接口。
- 模块 TDD、真实协议验收与 AB；只测试修改模块及其边界，不以全项目测试代替实际验证。

## Review Focus

- 非交易日起止和区间截断不能制造首末区段，单日区段按用户选择跳过并记录。
- 开盘/收盘前决策不得看到尚未发生的日线值；不按当前close倒填成交。
- 同日不同阶段的目标、订单与重放键不能冲突；税务会话/公司行动每日只结算一次，收盘成交后再确定日权益。
- benchmark 缺失/重复/错日期应失败，不能改变股票账户结果。
- 用户子集不能被读回完整范围，不能替代策略要求的全市场范围。
- 来源/哈希失败或准备失败不发布成功清单；生产实测失败不能改 fixture 伪装成功。

## Task 1: 数据合同、范围与可选基准

**Files:** Modify src/protocal.py, src/history_data.py, tools/prepare_history.py, src/application.py, src/workbench/models.py, src/workbench/benchmark.py; adjust src/core/backtest.py only if necessary for typed benchmark declaration; tests/test_data_protocol.py, tests/test_history_data.py, tests/test_prepare_history.py, tests/test_workbench_benchmark.py, tests/test_task3_protocol.py; docs/backend-protocol.md.

**Interfaces:** 复用 DataQuery/ Dataset/ DataBus。增加 DataBus.snapshot(dataset)->Mapping 和 publish(dataset,manifest)->DataRef：共同校验现有数据清单的 schema、显式scope、唯一字段及文件引用/哈希。范围本身已声明时不再强制独立 universe_source 文件；禁止读取旧字段作为fallback。prepare(root,config,*,data=None) 在组合根创建或复用一个 build_data(root)；活动输出 write/path/publish 均走同一实例。工具源文件是显式输入，不能扫描旧 runs。benchmark_id 是 workbench.backtest 可选输入，复用现有 Benchmarks，数据和比较进入结果/effective_config/hash，原子和搜索都保留；同区间按账户实际日期对齐。

- [ ] Step 1: RED 测试生产准备->协议snapshot->真实读取共享合同；明确股票子集与日期改变，缺价格/空证券/未知证券/哈希错误拒绝。
- [ ] Step 2: 实现共享清单规则、prepare通过DataBus及HistoricalLake消费。移除单独universe_source强制契约，原字段不保留兼容路径；更新相关fixture合同。保留市场范围、公司行动/费用与 as_of 校验。
- [ ] Step 3: 贯通预检和执行的 symbols、价格必需字段；字段需求集合包含价格与策略附加需求，不让调用者删除成交所需价格。
- [ ] Step 4: RED/GREEN 可选 benchmark_id：复用共享已注入的Store/Benchmarks；无基准不读取，有基准缺失/重复/日期不全失败，返回同区间原序列/标准化序列/收益，价格指数口径明确。搜索试验/验证基准必须按各自区间，不复用训练序列。
- [ ] Step 5: 用已有真实来源重建 history_holdout（保持原始响应字节不变，准备失败不发布成功manifest），经 build_backend+UiRequest 跑旧双均线 MA20/60 2024-01-02..2025-12-31；股票子集1股、不同区间、无基准/hs300做AB，从workbench.runs/run查询，结果存在data/runs。记录实际日期、证券、现金、净值、成交数、哈希及不支持项。
- [ ] Step 6: 模块GREEN命令 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. .venv/bin/python -m unittest tests.test_data_protocol tests.test_history_data tests.test_prepare_history tests.test_workbench_benchmark tests.test_task3_protocol tests.test_search_protocol -q。给出真实链路报告及精确changedpaths，不提交。

## Task 2: 通用成交阶段、独立策略与实际验收

**Files:** Modify src/core/contracts.py, src/core/interfaces.py, src/core/kernel.py, src/plugins/data.py, src/account_execution.py, src/execution.py and src/contracts.py only where explicit order phase requires; src/history_data.py for time-safe views; modify existing production strategy requirements only to declare default close/next-open behavior if contract needs it. Create src/plugins/strategies/trading_day_window.py and tests/test_trading_day_window.py; extend tests/test_settlement.py/tests/test_account_execution.py/tests/test_core_backtest.py. Documentation in docs/backend-protocol.md.

**Interfaces:** SessionClock(trade_date,previous,next,phase,block_length) immutable standard data, MarketFrame exposes clock; phases represent pre-open/pre-close decisions. DataRequest explicitly declares decision mode close or session (existing close mode contract default is not fallback). Session-mode Kernel reuses same records/lock/fresh strategy loop, orchestrates phase actions via settlement, not a strategy-name branch. Targets/orders explicitly declare current_open/current_close/next_open as needed; no inference from strategy identifier. Settlement phase implementation shares existing tax/account/execution components; do not duplicate portfolio or silently route close through open bars. Existing apply/plan behavior migrates to shared primitives if needed, rather than a second execution engine.

- [ ] Step 1: RED timing tests with literal calendar Jan5/8..12/15 (first Jan8 open buy,last Jan12 close sell), holiday Oct7/8..11/14, partial Tue..Thu no invented boundary; min_sessions=2 skips one-day block and records reason; params exposure finite0..1,min_sessions positiveint.
- [ ] Step 2: Implement typed clock and generic phase dispatch. Open/pre-close frames expose only available observations, history stops at prior completed close; calendar is schedule not future market signal. Missing phase/unsupported execution rejects; no special strategy branch.
- [ ] Step 3: Implement shared current-open and current-close execution in settlement/account components: close filled at genuine close, costs/T+1/tradable constraints retained, end-day equity marked after fills, corporate-action registration and tax processed once. Old MA fixture before/after equity/fills/metrics exact AB.
- [ ] Step 4: Implement single trading_day_window.py with NAME='交易日买卖策略', Params(exposure=1.0,min_sessions=2), standard requirements/generate. First block session open target explicit equal weights across queried stocks; block last pre-close target empty; otherwise None. New strategy auto-discovered without loader changes. Principal comes only from BacktestRequest.initial_cash.
- [ ] Step 5: Real production protocol runs Jan8..Feb8 2024 on 600036, cash1000000, explicit limits allowing chosen exposure, benchmark hs300 optional. Check actual every buy/sell date against real calendar gaps and price against real raw open/close; at least two complete blocks, no weekend trade, query-truncated block not forced liquidation. Additional multi-stock and holiday window; one-dayblock recorded skip. Independently recompute final cash/NAV from fills/fees/cash events and compare final metrics. One-candidate search versus atomic and benchmark-on/off account AB must match.
  成交价格以对应 raw open/close 为基准，加现有显式成交模型的滑点/价格刻度；不可误要求“成交价等于无滑点行情价”。独立复算使用字面费用公式而非调用被测执行函数。净损益从 initial_cash 计算；现有评价指标若以首个权益点为起点，报告其口径，不冒称等同于本金收益。指数标准化明确首个收盘价为基点，非总收益。
- [ ] Step 6: ModuleGREEN test_trading_day_window,test_settlement,test_account_execution,test_core_backtest,test_task3_protocol,test_kernel_search,test_search_protocol; read-only frontend protocol check. Real request and response readable through existing UiRequest/send; no frontend controls promised. Write evidence into actual data/runs via protocol; keep one concise report in task workspace; no temporary data outside cleanup-managed directories.
- [ ] Step 7: Independent spec/quality code review, readable API/docs review and actual protocol behavior verification; route findings back to implementation owner. Remove new temporary before-image copies/caches after review, retain results and reports. Mark complete only with actual successful real-data output, not synthetic tests alone.
