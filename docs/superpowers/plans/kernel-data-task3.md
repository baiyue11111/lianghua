# Task 3 brief: one dataset protocol, resident kernel

Start only after controller approval of Task 2. Read the total plan Requirements and acceptance. Use the project interpreter, preserve unrelated changes, do not commit, spawn helpers or edit frontend.

## Boundary

Define the standard DataQuery and Dataset contracts in src/protocal.py. A query declares dataset, dates, fields, history, basis, as_of and optional symbols. None symbols means the registered dataset universe, not the entire stock market. Reject unknown selections, insufficient history and incompatible strategy scope. Never infer policies or silently repair data.

DataBus.query is the single dataset entry point. Configure its historical reader explicitly once at composition. Existing byte read/write/list/path/delete remain the persistence entry. Avoid circular runtime imports: core can consume injected structural ports and query values without initializing the UI boundary. Do not invent another factory or hidden output directory.

Preflight/context and offline application callers must also obtain datasets through this standard query boundary; do not leave application/workbench calling HistoricalLake._load via another public execution path. Centralize query construction from checked request and strategy requirements without duplicate assemblies.

HistoricalLake becomes data preparation only: extract its run records generator and policy/provenance into query output. Keep pre-start warmup, next-session sentinel, raw execution bars, adjusted signal history, liquidity, registrations and source-bound fee/tax policies. No strategy execution or result persistence inside this reader. Do not discard HistoryDay events merely to return MarketFrames.

## Execution

Kernel remains a resident object with explicit busy handling. It asks the injected standard data port for its dataset, then drives the fresh strategy and adjustable data plugin through apply -> generate -> plan. Settlement state is fresh per request. Query and strategy/plugin assembly must be explicit ordinary composition, not RunFactory under a new name. Remove RunFactory definition, imports and all production use.

Adapter and Scaler are explicit selectable transformations, not automatic normalization of execution prices or cash. Preserve raw execution values and dividend accounting. Parameterize plugins through declared configuration/request data; reject unsupported parameters. Avoid extending scalar MarketFrame.context with complex account events.

The protocol build_backend(root) must work without a caller-supplied factory. KernelBacktestPort uses the registered strategy loader, standard DataQuery and configured plugin class. Keep existing UI envelopes and public frontend request fields working. Additional dataset selection options may be backend-only until frontend chooses them; do not change frontend.

Move the former HistoricalLake.run result formatting to the application/protocol side. Return complete trace, metrics, equity and provenance; save complete run records through the same DataBus. Never leave two independent account execution engines in the production path. Remove the old historical run entry; adjust its tests to query -> kernel rather than compatibility wrappers.

## Tests and review

Update factory-based fixtures to the new contracts. Retain meaningful repeated-request isolation, concurrent busy, invalid input, source failure and failed-run evidence tests rather than simply delete them. Old Components/Run offline step tests may remain only when genuinely independent and still used; do not keep them as an alternate backend route.

Add protocol full/custom queries, unknown selection rejection, build_backend without factory, production backtest saved/queryable run integration. Verify exact accounting against tests/fixtures/kernel-history-ab.json and the nonempty corporate-action reference from Task 2. Source/code identity hashes may change; account trace hashes, metrics and equity must not. Use temporary fixtures, no network or resident ports.

Run relevant Python tests, protocol boundary tests and static checks for RunFactory and hidden data paths. Report exact commands, counts and any remaining limitations. Controller performs independent review before completion.
