# Task 2 brief: Adapter and Settlement in plugins/data.py

Read Requirements in 2026-10-12-kernel-data-plugin.md. Do not touch frontend, protocal.py, core/kernel.py, history_data.py or unrelated dirty files during this task. Allowed: src/plugins/data.py, src/account_history.py, tests/test_data_plugin.py and a new focused settlement test.

## Settlement

Extract the stateful accounting portion of account_history.run_account_history into a reusable Settlement class in the same plugin file as Scaler. Preserve calculation and event order exactly, including failed-run evidence and weekend cash/share/tax dates.

Constructor takes initial_cash, fees, tax_policy and explicit limits; use existing AllocationLimits defaults only where existing runner already did. No storage, providers or strategy constructors. Methods:

- apply(record, following, trade_start=None) validates the next chronological HistoryDay, advances corporate action and tax dates, executes prior orders, marks equity, registers entitlements, returns the current frame and AccountSnapshot. Keep complete trace state. It must not generate a strategy target.
- plan(target, strategy_id) validates target identity/time, preserves None versus empty target semantics, applies target through the existing execution session, and appends the step trace. Refuse double apply / missing plan and stale state.
- result() returns current HistoryResult; never fabricates success or truncates failed trace.

Make account_history.run_account_history a thin chronological loop: validate declared calendar; create Settlement; for each record call apply, generate strategy target, plan; check exact count; wrap failures with existing HistoryRunError and partial trace. Preserve the public offline function signature; it is an algorithm driver used by existing tests, not a factory or legacy protocol fallback. Use lazy imports inside methods or move neutral dataclasses if needed to avoid account_history↔plugins.data import cycles. Avoid duplicate copies of the original accounting body.

## Adapter

Pure class methods, no storage: validate MarketFrame sequence identity/order/finite numeric values and explicit missing handling; align to supplied ordered calendar without creating trades or interpolated prices; adjust price and volume with explicit basis and known factors (raw/split-adjusted/adjusted), anchor and as_of. Use clear input structures and document factor direction. Reject unsupported/unknown events, dates and invalid factors. No price adjustments derived from future events. Prefer existing corporate_actions validated algorithms where they match; do not recompute supplier factors using a mismatching formula. No machine-learning scaler on cash/price execution fields by default.

## Verification

Run .venv/bin/python -m unittest tests.test_data_plugin tests.test_account_history tests.test_account_distribution tests.test_tax_ledger tests.test_corporate_actions -q. Compare original reference at tests/fixtures/kernel-history-ab.json after unchanged HistoricalLake.run; every trace key hash and metrics/equity must match. Add an independent synthetic split example preserving shares×price and a nonempty dividend/share settlement comparison using tests.test_account_history.AccountHistoryTests fixture, with explicit expected cash, shares, liabilities from pre-existing tests. Do not claim empty-action fixture proves tax equivalence. Use project interpreter only.

Return DONE / DONE_WITH_CONCERNS / BLOCKED, files, exact commands/results, identified risks. Do not commit or spawn agents. Controller handles reviews and subsequent kernel task.
