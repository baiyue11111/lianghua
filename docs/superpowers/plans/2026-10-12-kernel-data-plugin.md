# Kernel data protocol and plugin plan

## Requirements

- User approved a single plugin file containing classes for adaptation, scaling and settlement, and explicitly requested GPT-5.6 Luna implementation supervised by the main agent.
- src/protocal.py owns standard data input/output contracts. All kernel dataset queries enter this boundary. Full scope and custom symbols/fields/time ranges share one query interface.
- Remove RunFactory rather than rename it. Kernel remains resident and drives execution. Mutable strategies/accounts/plugin state must be fresh per run.
- Keep frontend/ui-protocol.py unchanged. Preserve unrelated dirty worktree files and existing historical accounting algorithms; do not create fallback, inferred policies or guessed market defaults.
- Corporate action facts and fee/tax policies remain source-bound and effective-date bound. No double dividend accounting, future-data leakage, synthetic traded observations or silent missing-data repair.
- Core must not initialize the outer UI/protocol module as an import side effect. Use injected structural ports and type-only imports where needed.

## Task 1: Feature scaler

Implement Scaler in src/plugins/data.py with fit and transform, explicit standard/minmax/robust modes and parameters. Use training-only statistics, stable feature shape, finite input validation, unfitted errors and documented constant-feature behavior. No automatic clipping or mode fallback. Add independent numerical reference and train/validation separation tests. Do not add another module or external dependency.

## Task 2: Data adapter and settlement classes

Extend the same file with Adapter and Settlement classes. Adapter validates normalized data, aligns explicit calendars and adjusts prices under explicit basis/as_of. Reuse existing validated corporate-action algorithms. Settlement applies existing fees/dividend/share/tax algorithms to explicit account state/events, not price-only tax estimates. Define exactly which standard methods consume what. Keep acquisition/storage outside the plugin. Test split value preservation, cash dividend events, dated fee policies and tax holding boundaries against independent expected values and the existing implementation.

## Task 3: Protocol queries and Kernel

Add standard dataset query/result contracts to src/protocal.py; expose selected dataset, symbols, fields, interval, history, price basis, as_of and provenance. Full means the declared universe of the requested dataset, not a claim of full market coverage. Route storage access via the shared DataBus. Refactor historical loading into data preparation, preserving pre-start warmup, calendar sentinel and corporate action/account policy evidence.

Remove RunFactory from core and all production callers. Separate explicit component assembly from dataset queries. Kernel requests its source through the injected standard protocol and drives plugins. Ensure the production build_backend composition works without a supplied RunFactory. Preserve cash/share/tax/event behavior; adapting only MarketFrame and discarding HistoryDay/account events is forbidden. Update meaningful affected tests rather than preserve the removed factory API.

## Acceptance

- Independent plugin numerical tests and invalid-input cases pass.
- Standard query supports full and custom scope, rejects undeclared symbols/fields and insufficient history explicitly.
- Kernel has no RunFactory and no physical storage/provider imports or hidden substitute factory.
- Repeated requests do not share mutable plugin state; busy/error/resource behavior stays explicit.
- Same frozen historical inputs produce identical equity, fills, company actions and taxes before/after. Hash differences caused solely by implementation identity must be stated, not falsely described as accounting changes.
- Protocol-to-kernel integration and relevant existing tests pass, with exact commands and counts reported. No resident server or port needed.

## Supervision ledger

Base local commit: 00f5f6b. Existing source files under src/core and many migrated src modules are untracked in this checkout; their current content is the baseline, not a permission to overwrite them.
Task 1: fix round 2, agent 01a12f00-7af8-72e3-acb7-0932f9dbbe79, requested model gpt-5.6-luna. First review: move Scaler to single plugin file and preserve validation changes for constant training columns. Independent reviewer 01a12f04-4e6d-75f0-b358-6a47299d1cad then found fit/transform overflow and mixed boolean coercion; fixes requested.
Task 1 scoped rereview: passed overflow, boolean rejection and failed refit state preservation; 10 focused tests passed with project interpreter. Extreme inputs that overflow calculations are rejected explicitly.
Task 2: running with the same Luna agent; concrete brief in kernel-data-task2.md. Requires independent review before kernel integration.
Task 2 review round 1: normal-path frozen AB and independent original-source dividend/weekend-tax comparisons match. Review found missing declared-calendar check, resumable failed settlement, missing following-session continuity, unvalidated history values/times and input-basis/as_of relabeling. Fixes assigned; Task 3 remains gated.
Task 2 scoped rereview: passed all five blockers, 33 local tests and 13 rejection probes. Controller reran 85 plugin/account/history tests successfully; Arrow CPU sandbox warnings were nonfatal.
Task 3: running with Luna, concrete brief in kernel-data-task3.md. Requires full protocol-to-historical-accounting integration and independent review.
Task 3 review round 1: frozen accounting and nonempty event comparisons match. Blockers: truncated interval acceptance, request lowering strategy universe, unbound universe-source for dataset scope, frontend result identity/archived JSON lookup mismatch, incomplete failure evidence, and unproven as_of available-time semantics. Explicit Adapter/Scaler integration and migrated negative-path tests also required. Fixes assigned; not complete.
Task 3 review round 2: prior blockers fixed except PIT only binds factor availability, config identity omits settlement/signal transformations, and signal plugins are hardcoded or discard results. Final scoped fixes assigned. Scaler training must precede backtest; independent validation leakage probes passed. Controller full Python suite 444 passed after restoring independent Run contracts; frontend protocol check and 39 existing frontend tests passed without frontend edits.
Task 3 review round 3: config identity and replaceable plugins fixed; controller full suite 445 passed. Independent adversarial probes found NumPy output incompatible with real strategy scalar contracts, unchecked custom scaler shape/finite values, unbound calendar file in PIT and adapter history coverage/value conflicts. Four focused fixes assigned before final release.
Final Task 3 scoped review: all four fixes passed independent tests/probes; real breadth strategy with pre-start fitted Scaler produced three evaluated points. Controller final verification: `.venv/bin/python -m unittest discover -s tests -q` passed 448 tests; `.venv/bin/python -m compileall -q src` and `git diff --check` passed. Frontend `npm run check:protocol` and `node --test tests/*.test.mjs` passed 39 tests; frontend diff is empty. Frozen historical metrics/equity/all trace hashes and original nonempty dividend/weekend-tax traces matched. No commit, PR, frontend edits or resident server were created. Existing snapshots without availability evidence support snapshot replay only; explicit PIT rejects incomplete evidence. Query reader currently supports daily adjusted basis; unsupported bases fail explicitly.
Review packages will compare current source snapshots; local HEAD alone omits existing untracked core files.
Pre-change source snapshot: /private/tmp/lianghua-kernel-baseline.uEvdOe/.
Pre-change targeted tests: 47 passed, .venv/bin/python -m unittest tests.test_core_backtest tests.test_protocal tests.test_history_data tests.test_tax_ledger tests.test_account_history -q.
Frozen historical AB: tests/fixtures/kernel-history-ab.json, generated before accounting changes from tests.history_fixture.make_snapshot. It has real synthetic fills but no distribution/tax payments, so nonempty corporate-action tests are additionally mandatory.
