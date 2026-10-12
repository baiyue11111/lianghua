# SDD ledger — plan: docs/superpowers/plans/2026-10-13-price-session.md

Preflight: feature checkout codex-unified-backend-protocol, HEAD 00f5f6bc110d67452e398c24ffb0294c5b498ca8, many prior user/uncommitted changes. No new worktree or commit; a clean HEAD checkout would omit the current implementation. Module baseline 32 tests PASS. Planner Luna completed read-only; no frontend stage (user-maintained, not requested).

| Task/pair | Producer -> consumer | Check |
|---|---|---|
| 1 | prepare -> DataBus.publish/snapshot -> HistoricalLake | Same scope/file contract, explicit subset, no forced universe evidence file |
| 1 | optional Benchmarks -> protocol result/persistence | No change to account, dates/hashes bound |
| 2 | clock+phase -> strategy -> settlement | Available-only views, no name branch, genuine open/close execution |
| 1/2 | queried Dataset/calendar -> phase Kernel | Shared protocol data and new immutable clock, unchanged account contract |
| 1/2 | atom/search -> benchmark/date selection | Each trial/validation gets its own interval, no train benchmark leakage |

Planner suggested first/last of requested interval; rejected because user wants holiday-delimited blocks, not arbitrary request-edge liquidation. Spec explicitly uses original trading calendar gaps. User confirmed skip single-session blocks with reason. Weekend inclusion remains declared assumption unless user corrects.
Ruling: new file uses current formal plugins.strategies directory, not old src/strategies — user's location was conditional on formal directory; avoids second loader — cost if wrong: later folder migration.

Task 1: complete for independent review. RED/GREEN recorded; final Task1 module command ran 58 tests and passed. Real production prepare/read, UiRequest preflight/backtest, Research queries, one-symbol full/short interval AB, and optional hs300 benchmark completed. No frontend change, commit, or push.
Task 2: pending independent Task1 review; not started.

User redirected: first publish current code as PR, then remove the old implementation completely. PR owner Luna/Feynman; read-only cleanup planner Luna/Hubble. No cleanup implementation before PR publication.

Independent main verification: Task1 module command 58 tests PASS; git diff --check PASS. Actual nonzero baseline via DataBus query and Kernel: MA20/60, exposure=.4, 600036, 2024-01-02..2025-12-31, initial_cash=1000000; 485 steps, 14 fills, final_cash=final_equity=997778.07562, no positions. This is a single engineering check, not profitability validation or benchmark AB.

Known defects: prepare requires existing output account_policy; direct path/hash IO remains; effective_config.history reports request default 0 rather than actual dataset history. User-facing old policy source configuration claim corrected: config does not explicitly specify a policy input.

GitHub repository verified using attached prior PRs and get_repo: baiyue11111/lianghua, private, main default, push allowed. PR2 merged. No token request needed. Current local remote config empty; must preserve remote changes and use verified main base.
