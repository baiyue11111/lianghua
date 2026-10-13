# Remove Old Contracts and Legacy Execution Paths Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 PR 当前版本确认后，删除旧 contracts、旧 Kernel/Run 链、旧策略入口和旧 research runner，只保留 `src/protocal.py` 的公共结构与总线、固定 Kernel、正式插件策略和现有财税/公司行动计算。

**Architecture:** `src/protocal.py` 同时拥有唯一公共数据结构、请求校验、DataBus 和装配入口。`core/kernel.py` 只保留固定回测协调，不再暴露 `Run` 或 `Components`。Settlement 保留真实成交、账户、税费和公司行动流程；仅把历史输入/结果记录改成模块私有结构。正式策略唯一位于 `src/plugins/strategies/`。

**Tech Stack:** Python 3.12+、dataclass、typing.Protocol、unittest、现有 DataBus/HistoricalLake/Settlement。

**Spec:** 用户本轮要求；执行时同时遵守 `docs/superpowers/specs/2026-10-13-backtest-loop-design.md` 和 `docs/superpowers/specs/2026-10-13-price-session-design.md`。

## Global Constraints

### Main review rulings

These rulings override conflicting draft details below (including private cross-module copies, permanent baseline fixtures and full-suite gating).

- Cleanup is directly authorized by the user after the current PR is published; no second approval gate is needed.
- Do not rename duplicate public order/fill models to private models and keep the same double contract. Keep one production-used order, fill and market-bar structure in protocal.py, preserving actual accounting field meanings. Delete unused Run-only OrderRequest/FillEvent/ExecutionResult/StepResult structures rather than promoting dead APIs to the canonical contract.
- Records crossing HistoricalLake -> Kernel -> Settlement are boundary data and must have a single protocol definition, not private duplicates shared across modules. Only state owned and consumed within one module is private.
- Check obsolete plugins/accounts, plugins/markets and plugins/risk along with Run. Remove implementations used only by the retired engine. plugins/recorders/jsonl is currently imported for _json_value by protocol/application: move the used serialization function once to protocol and delete the retired recorder if no live caller remains.
- Preserve tests of money, risk, corporate actions and strategy rules; migrate their assertions to the live path before deleting old harnesses. Removing tests solely to make a green count is prohibited.
- Do not create permanent real-data before/after copies in tests/fixtures. Freeze one cleanup-managed temporary full baseline, compare and remove it after acceptance; retain only a concise receipt and small general-purpose module fixtures.
- Scope verification to the changed modules and boundary callers. Do not make a full-project suite a prerequisite; the user explicitly requests module-scoped AB checks.

- 必须先完成 PR，再执行本计划；本计划不是实施授权。
- 不修改 `frontend/`，不新增 contracts shim，不保留兼容 wrapper、fallback、第二引擎或按策略名分支。
- 唯一公共结构/总线是 `src/protocal.py`；内部计算和结算记录可以是模块私有类。
- 不删除有效策略规则、预热、固定 slots、T+1、费用、税费、分红、送转股、代码变更和公司行动计算。
- 保留标准“收盘决策、下一交易日开盘成交”；保留计划中的显式开盘/收盘阶段能力，不从策略名推断阶段。
- `src/protocal.py` 顶层不得 import `core.kernel`、`application` 或 `frontend.ui-protocol`。这些必须在装配或消息边界函数内后置 import；不使用 `TYPE_CHECKING` 掩盖运行时循环。
- 不用 0 成交作为 AB 验收。真实 AB 必须有非零成交，并比较完整账户输出和哈希。
- 保留工作树其他未提交改动；实施阶段也不提交本清理改动，除非另行授权。

## Current Status

- 本轮已完成源码调用图核对；本文件是唯一计划交付物。
- 当前工作树已有大量未提交迁移改动。主代理必须以 PR 当前版本为基线重新确认文件存在和删除范围。
- 用户提供的模块级 58 tests PASS 只证明独立模块，不证明整体清理完成。
- 用户提供的真实非零成交基线：MA20/60、`exposure=.4`、`600036`、`2024-01-02..2025-12-31`、本金 `1000000`、485 steps、14 fills、`final_cash=final_equity=997778.07562`、空持仓、`total_return=-0.0022219243800000177`、`max_drawdown=-0.10756380215132233`。

## Findings and Evidence

1. `src/protocal.py:24-31` 当前顶层 import `core.backtest`、`core.kernel`、`application` 和 `frontend.ui-protocol`；类型集中后会形成反向循环，必须后置装配 imports。
2. `src/core/kernel.py:35-125` 仍暴露旧 `Run`；`src/core/interfaces.py:67-83` 仍暴露 `Components`。测试也直接引用它们。生产主路径已经是 `Kernel._run`，因此应删除旧资源包和 `Run`，不是再包装一个新名字。
3. `src/contracts.py` 有旧 `Bar/Order/Fill/Candidate/StrategyFrame/TargetPortfolio`；`src/core/contracts.py` 有另一套 `DataRequest/MarketFrame/TargetSet/OrderRequest/...`；`src/core/backtest.py` 还有公共 request/response/search 类型。两套不能简单搬到同一个文件保留。
4. 正式 `src/plugins/strategies/*.py` 只依赖 `core.contracts`、`core.calculation`、`strategy` 和特征函数；未发现对 `src/strategies` 或根 `research.py` 的导入。它们应迁移到 `protocal.py` 的规范类型，规则不改。
5. `src/account_history.py:27-116` 仍持有 `HistoryDay/RegistrationAction/HistoryResult` 及 `_validate_day/_register`；`src/history_data.py:21-25,399` 创建历史记录，`src/plugins/data.py:402-460,528-548` 由 Settlement 调用。不能直接删文件；应先把结算所需记录和校验变成私有结构/方法，再移除旧循环 `run_account_history:119-151`。
6. `src/research.py` 的 `ExploratoryMarket/BacktestResult` 仍被 `src/audit.py`、旧测试和 `strategy_v1.py` 使用；正式插件不依赖它。`tools/run_backtest.py:1-7,166-167,524-527` 已 retired，但仍保留旧计算和 `ROOT/runs/backtest`，应整文件删除。
7. `tools/prepare_history.py:34-37` 会先创建输出子目录，`:78` 从已有输出读取旧 `account_policy.json`，`:80` 直接对输出路径 digest。现有 fixture `tests/test_prepare_history.py:20-31` 预先创建 policy，掩盖空输出失败；必须改成显式 `DataRef` 输入和先验证后发布。
8. `src/protocal.py:1001-1005` 将 `effective_config.history` 写成请求值；当请求为 0 时应写实际 `dataset.history`，并进入同一个 config hash。

## Review Focus

- 清理后直接 `import protocal` 不加载 Kernel、application 或 frontend；对应 Task 2 的 import-surface test。
- 旧 `Run/Components/contracts/research` 符号和路径不能从生产入口可达；对应 Task 3/4 的 negative import tests。
- 空输出目录缺显式 policy 必须失败且不发布 manifest；对应 Task 5 的 negative test。
- 请求 `history=0` 必须在结果配置中记录实际 dataset history；对应 Task 5 的 literal test。
- 非零成交的完整账户 trace、成交、税费、公司行动和 hash 必须前后一致；对应 Task 1/6 的 AB test。

---

### Task 1: Freeze the real non-zero baseline before cleanup

**Files:**
- Create during implementation: `tests/fixtures/protocol-ma20-600036-baseline.json`
- Create during implementation: `tests/fixtures/protocol-ma20-600036-baseline.sha256`
- Test: `tests/test_remove_old_contracts_ab.py`

**Interfaces:**
- Consumes: `build_backend`/`KernelBacktestPort`、`DataBus`、`HistoricalLake`、正式 `moving_average_cross`。
- Produces: canonical JSON of the full `BacktestResponse` plus account result, dataset provenance, orders, fills, cash events, holdings, distributions, registrations, tax payments/liabilities/pending/lots, and one SHA-256.

- [ ] **Step 1: Run the exact supplied request before source changes.**

  Use `MA20/60`, `exposure=.4`, `600036`, `2024-01-02..2025-12-31`, initial cash `1000000`, default settlement limits. Do not change risk or accounting defaults to increase fills.

- [ ] **Step 2: Freeze literal response assertions.**

  Assert `steps == 485`, `fills == 14`, `final_cash == Decimal("997778.07562")`, `final_equity == Decimal("997778.07562")`, `positions == {}`, `total_return == -0.0022219243800000177`, and `max_drawdown == -0.10756380215132233`.

- [ ] **Step 3: Freeze the complete trace and hash.**

  Serialize Decimal values as strings, preserve tuple order, sort only mapping keys, and hash the canonical bytes. The test must reject an empty or zero-fill baseline.

- [ ] **Step 4: Record the current risk observation separately.**

  Keep the supplied observation that `exposure=1.0` on one stock was blocked by the 40% risk cap seven times as a negative/diagnostic case. Do not treat it as a reason to alter the rule.

### Task 2: Consolidate the public contract and remove the import cycle

**Files:**
- Modify: `src/protocal.py`
- Modify: `src/application.py`, `src/strategy.py`, `src/history_data.py`, all valid accounting/plugin modules and affected tests
- Delete after migration: `src/contracts.py`, `src/core/contracts.py`, `src/core/backtest.py`

**Interfaces:**
- Produces one canonical public set in `src/protocal.py`: request/response/search types, `DataRequest`, `HistoryBar`, `MarketFrame`, `TargetSet`, `OrderRequest`, `FillEvent`, `ExecutionResult`, `AccountSnapshot`, `StepResult`, `Side`, `Scalar`, `DataQuery`, `Dataset`, `DataRef` and protocol port types.
- Does not produce public copies of old `Candidate/StrategyFrame/TargetPortfolio`; those old exploratory contracts are removed with the old strategy path.

- [ ] **Step 1: Add RED import-surface tests.**

  Import `protocal`, `core.kernel`, `strategy`, and a formal plugin in fresh subprocesses. Assert importing the canonical types does not load `core.kernel`, `application`, or `frontend.ui-protocol` as a side effect.

- [ ] **Step 2: Move and normalize contracts once.**

  Put one implementation of each canonical type in `protocal.py`. Move private validation helpers with the types. Update callers to import that implementation directly. Do not create `contracts.py` or re-export shims.

- [ ] **Step 3: Make assembly imports explicitly late.**

  Import `Kernel` and save functions inside `build_backend`/`KernelBacktestPort` assembly. Import UI types/constants only inside the protocol/message boundary. Keep protocol name/version values stable without importing frontend during type import. `application.py` may import canonical protocol types at runtime because `protocal.py` no longer imports it at module load.

- [ ] **Step 4: Migrate accounting-only old models to owners.**

  Do not move the old contract module wholesale. Convert `Bar/Order/Fill` used by the real settlement ledger into underscored types owned by the execution/accounting module; convert the allocation target internally at the Settlement boundary from canonical `TargetSet`. Remove old exploratory `Candidate/StrategyFrame/TargetPortfolio` users rather than preserving them.

- [ ] **Step 5: Run targeted import and protocol tests.**

  Expected: canonical imports pass; old module imports fail; no frontend edit; no `TYPE_CHECKING` import is being used as a runtime-cycle workaround.

### Task 3: Remove the old Kernel `Run/Components` chain

**Files:**
- Modify: `src/core/kernel.py`, `src/core/__init__.py`, affected tests
- Delete after migration: `src/core/interfaces.py`

- [ ] **Step 1: Add RED tests proving the fixed Kernel path.**

  Use the protocol/query path for one successful run, one invalid request, timestamp/order rejection, busy lock, failed-run evidence, and fresh settlement state. Assert there is no `Run` or `Components` export.

- [ ] **Step 2: Delete `Run` and `Components`.**

  Keep `Kernel.run/search/_run` as the only orchestration. Keep one execution loop, one lock, fresh strategy and settlement per trial. Remove the resource-bundle API and all tests that instantiate it.

- [ ] **Step 3: Preserve phase and next-open semantics.**

  Keep the following-session sentinel and pending-order execution at the next open. Carry the explicit session/phase contract needed by the price-session plan; do not branch on strategy name or route to a second engine.

### Task 4: Move Settlement-owned history work and remove `run_account_history`

**Files:**
- Modify: `src/plugins/data.py`, `src/history_data.py`, `src/protocal.py`, affected settlement/history tests
- Delete after migration: `src/account_history.py`

- [ ] **Step 1: Add RED settlement tests for the preserved rules.**

  Cover ordered calendar and following-session sentinel, current/future liquidity rejection, invalid registration evidence, share-credit-date validation, actual fills, T+1 sellability, dividend/stock delivery, FIFO tax liability/payment, partial failure evidence, and result serialization.

- [ ] **Step 2: Move only required records to private owners.**

  `HistoricalLake` may create private `_HistoryDay`/`_RegistrationAction` input records. `Settlement` owns private result/registration records, `_validate_day`, and `_register`. Keep the existing field meanings and accounting formulas; do not expose them as a second public contract family.

- [ ] **Step 3: Remove the old loop.**

  Delete `run_account_history` and its `HistoryRunError` path. Kernel remains the caller of `Settlement.apply -> strategy.generate -> Settlement.plan -> Settlement.result`; failed partial results still go through the protocol failure record.

- [ ] **Step 4: Prove the production path no longer imports `account_history`.**

  Search and subprocess tests must show `HistoricalLake` and Settlement use their private owners, not the deleted module. Keep `src/workbench/research.py` and `src/research_registry.py`; they are DataBus read/registry paths, not the deleted exploratory engine.

### Task 5: Fix source preparation and effective configuration

**Files:**
- Modify: `tools/prepare_history.py`, `tests/test_prepare_history.py`, `src/protocal.py`, affected protocol tests

- [ ] **Step 1: Add empty-output RED cases.**

  Start with a completely empty output root. Omit the account-policy `DataRef`; expect a clear failure before any output directory or manifest is created. Provide all inputs as explicit `DataRef` identities and verify success does not depend on an old output policy.

- [ ] **Step 2: Route input reads through DataBus.**

  Replace direct source `Path.read_bytes`, output-path digesting, and pre-created output subdirectories with validated `DataRef` reads. Verify identity, role, size and SHA-256 before deriving files. Use DataBus writes and one final publish; no old policy lookup and no hidden source fallback.

- [ ] **Step 3: Test tampering and missing inputs.**

  Reject a changed source hash, wrong identity/role, missing calendar sentinel, missing policy source, and incomplete price session. Assert no successful manifest is published.

- [ ] **Step 4: Fix `effective_config.history`.**

  When request history is `0`, write `result.dataset.history`, not `request.history`, and include that value in the same config hash. Add a literal test with a strategy requirement greater than zero.

### Task 6: Delete old research/runner entry points and migrate only valid tests

**Files:**
- Delete: `src/research.py`, `src/strategies/__init__.py`, `src/strategies/era_leadership.py`, `src/strategies/quality_trend.py`, `src/strategies/trend_pullback.py`, `strategy_v1.py`, `src/audit.py`, `tools/run_backtest.py`
- Delete tests serving only those paths: `tests/legacy_breadth.py`, `tests/legacy_research.py`, `tests/test_strategies.py`, `tests/test_research_frame_features.py`, `tests/test_research_order_lifecycle.py`, `tests/test_audit.py`, `tests/test_backtest_input_gate.py`
- Retain and verify: `src/plugins/strategies/`, `src/workbench/research.py`, `src/research_registry.py`, `tools/research_daily.py`, `src/collect/lake_display.py`

- [ ] **Step 1: Add negative import tests before deletion.**

  Assert formal strategy loading never imports `research` or `strategies`, and protocol backtests never use `tools.run_backtest` or root `runs/`.

- [ ] **Step 2: Remove the old source and old-only tests.**

  Do not port `ExploratoryMarket`, its frame calculator, factor-ratio runner, force liquidation, or old audit writer. Their valid accounting intent is already covered by Settlement/Kernel tests and the real baseline.

- [ ] **Step 3: Re-home audit assertions if still required.**

  If a current test needs audit evidence, test the protocol/DataBus `data/runs` result and `BacktestResponse` instead of recreating `BacktestResult` or writing `root/runs`.

### Task 7: Final AB, negative matrix, and cleanup gate

**Files:**
- Modify/create: affected tests and the frozen fixture from Task 1
- Verify: all files in the delete/retain lists and `pyproject.toml` packaging entries

**AB matrix:**

| Case | Input | Literal expected | Acceptance |
|---|---|---|---|
| Real non-zero AB | MA20/60, `.4`, `600036`, 2024-01-02..2025-12-31, 1,000,000 | 485 steps, 14 fills, final cash/equity `997778.07562`, empty positions, return `-0.0022219243800000177`, drawdown `-0.10756380215132233` | Full trace and canonical hash equal before/after; `fills > 0` required |
| Fixed slots | two selected symbols, 8 slots | each weight `0.125` | exact weights; no denominator rewrite |
| Parameter effect | same data, changed valid strategy parameter | strategy output or fills change | proves the formal plugin is active, not a zero-trade pass |
| History resolution | request `history=0`, strategy needs 60 | `effective_config.history == 60` | hash contains 60 |
| Missing policy | empty output, no policy `DataRef` | explicit failure, no manifest | no output-only fallback |
| Tampered source | changed bytes with old hash | explicit hash failure | no success manifest |
| Invalid request | bad dates, bool/NaN parameter, unknown field | explicit `invalid_request`/`invalid_query` | no partial success |
| Risk boundary | one stock at 100% | current 40% cap rejection behavior | diagnostic only; never tune rule and never use zero fills as AB |
| Phase timing | close decision followed by next-session open; planned explicit open/close phase | order timestamp and fill session exact | no future high/low/close leakage; no second engine |

- [ ] **Step 1: Run module tests for protocol, Kernel, Settlement, formal strategies, history preparation and DataBus.**
- [ ] **Step 2: Run the real non-zero AB against the frozen complete output and hash.**
- [ ] **Step 3: Run negative import/path scans.**

  Expected no production imports of `contracts`, `core.contracts`, `core.backtest`, `core.interfaces`, `account_history`, `research`, `strategies`, or `tools.run_backtest`; no root `runs` writer; no contracts shim.

- [ ] **Step 4: Run the full Python suite and static checks.**

  Use the project interpreter, `compileall`, and `git diff --check`. Report exact counts and any pre-existing failure separately. Do not claim frontend or browser validation because frontend is out of scope.

## Concrete Final Delete List

Delete only after callers and tests are migrated:

- `src/contracts.py` — duplicate legacy domain contracts; accounting users move to owner-private types and formal plugins use canonical protocol types.
- `src/core/contracts.py` — duplicate kernel contract family; canonical types live once in `src/protocal.py`.
- `src/core/backtest.py` — public request/response/search family moves to `src/protocal.py`.
- `src/core/interfaces.py` — `Components` and old public port bundle disappear with `Run`; required structural ports become private to their owner.
- `src/account_history.py` — delete only after its settlement records, validation, registration and result construction are owned by `HistoricalLake`/Settlement; do not delete the accounting formulas.
- `src/research.py` — obsolete exploratory market/frame engine and `BacktestResult` path.
- `src/strategies/` — old strategy interface and duplicate implementations; formal plugins remain.
- `strategy_v1.py` — old direct exploratory entry.
- `src/audit.py` — old `BacktestResult`/filesystem audit writer with no current production caller; current run evidence remains in DataBus.
- `tools/run_backtest.py` — retired factor-ratio engine, old direct files and root `runs` path.
- `tests/legacy_breadth.py`, `tests/legacy_research.py`, `tests/test_strategies.py`, `tests/test_research_frame_features.py`, `tests/test_research_order_lifecycle.py`, `tests/test_audit.py`, `tests/test_backtest_input_gate.py` — tests for deleted interfaces or invalid retired runner only.

## Retain List and Reasons

- `src/protocal.py` — sole public contract/bus/assembly boundary.
- `src/core/kernel.py` — fixed resident orchestration; no `Run`/`Components` API.
- `src/core/calculation.py` — one shared pure calculation implementation.
- `src/plugins/strategies/` — formal strategy directory; preserve every existing rule.
- `src/plugins/data.py` — Settlement, adjustment and accounting integration; owns private settlement records after migration.
- `src/history_data.py` — registered HistoricalLake source adapter; it must construct private records through DataBus data.
- `src/account_allocation.py`, `src/account_execution.py`, `src/account_ledger.py`, `src/execution.py`, `src/distribution_ledger.py`, `src/dividend_tax.py`, `src/tax_ledger.py`, `src/corporate_actions.py`, `src/fees.py`, `src/execution_risk.py` — valid money, fills, tax and company-action rules; refactor imports/types only.
- `src/application.py` — internal run persistence/preparation implementation; imported by protocol only after module initialization.
- `src/workbench/research.py`, `src/research_registry.py`, `tools/research_daily.py`, `src/collect/lake_display.py` — current DataBus-backed research/registry/dashboard paths, not the deleted exploratory engine.
- `frontend/` — untouched.

## Unresolved Items for the Main Agent

- Confirm the exact PR revision before applying deletes; the current shared worktree is dirty and contains both deleted package files and untracked migrated files.
- Confirm whether any current PR artifact still intentionally consumes `src/audit.py`; the source scan found no current production import, but the PR may add one.
- Confirm the final private-record owner (`plugins/data.py` versus the nearest accounting module) before implementation. The invariant is one owner, underscored names, no public re-export, and no duplicate accounting algorithm.
- Confirm the serialization fields included in the complete baseline hash. The plan requires all account and provenance fields, but the exact JSON key order must be frozen once by the main agent.

## Next Action

主代理先在 PR 当前版本核对本计划的删除/保留 scope，并确认未决项；确认后按 Task 1 冻结完整非零成交基线，再实施 Task 2 起的清理。当前计划阶段不改源码、不删文件、不提交、不启动端口。
