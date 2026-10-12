# Task1 report — price session data contract

## status

Task1 complete for independent review. Task2 and new strategy work were not started.

## findings

- `DataBus.publish` and `DataBus.snapshot` now share one manifest contract: schema, explicit scope, unique fields, file roles, relative paths, and SHA-256 bytes are checked in both directions.
- `HistoricalLake` reads the declared fixed-symbol scope. It does not require or read `universe_source`.
- `prepare` uses one `DataBus` for output writes and publish. It does not scan old runs. The real `history_holdout` rebuild succeeded with 50 file references and the 10 configured symbols.
- Price execution fields are always present: `open`, `high`, `low`, `close`, `volume`, `amount`; strategy fields are added after them. Preflight and execution preserve the same request fields, symbols, history, price basis, and benchmark declaration.
- `benchmark_id` is optional. Without it, account execution does not read a benchmark. With `hs300`, the result contains the actual-date price series, normalized values, returns, snapshot id, and hash. Search/validation code uses the interval-specific benchmark.

## evidence

### TDD and module tests

- RED recorded for missing DataBus publish/snapshot, old `universe_source` dependency, missing execution price fields, missing benchmark argument, and preflight contract fields.
- GREEN command:

  `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. .venv/bin/python -m unittest tests.test_data_protocol tests.test_history_data tests.test_prepare_history tests.test_workbench_benchmark tests.test_task3_protocol tests.test_search_protocol -q`

  Result: `Ran 58 tests ... OK`.

### Real prepare and reader

- Read-only source check: 14 daily parquet files, 7,270 rows, 10 fixed symbols, and calendar sessions matched.
- Production `prepare(root, configs/history-holdout.json)` return: `status=succeeded`, `dataset=history_holdout`, `files=50`, `scope.universe=fixed_symbols`, `source_bytes_unchanged=true`, `source_changed=[]`.
- Real reader after rebuild: 727 records for `600036`, `2023-01-03..2025-12-31`, requested interval `2024-01-02..2025-12-31`, fields `open/high/low/close/volume/amount/signal_close`, data hash `1de577a8a9e41306623d5aa16a3d5fbe116d816a36d66fa3b7a577a25a82a7ca`.

### Real UiRequest and Research

- `workbench.preflight` with `initial_cash="1000000"`, MA20/60, `symbols=["600036"]`, `history=60`, `benchmark_id="hs300"` returned `kind=document`, `ready=true`, `issues=[]`; both `effective_config.fields` and `requirements.fields` were exactly `open/high/low/close/volume/amount/signal_close`.
- `workbench.runs` returned the real Task1 run ids. `workbench.run` returned `task1-real-hs300` with `data_hash=1de577a8...a82a7ca` and effective benchmark id `hs300`.
- A first preflight using integer `initial_cash=1000000` returned the real contract error `invalid_input` (“initial_cash ... valid string”). It was corrected to the declared string contract. No permissive fallback was added.

### Real AB

Full interval `2024-01-02..2025-12-31`, one symbol `600036`:

- no benchmark: 485 steps, 0 fills, final cash `1000000`, final equity `1000000.000`;
- hs300: 485 steps, 0 fills, final cash `1000000`, final equity `1000000.000`, 485 benchmark points, snapshot/data hash `836005df16f2163fada8afd3ef9d2689be4f794a274894d7448f051594d43ae2`.

Short interval `2024-01-02..2024-03-29`:

- both variants completed with 58 steps, 0 fills, final cash `1000000`, final equity `1000000.000`;
- hs300 supplied 58 points for exactly the account dates.

Across full-interval AB, `positions`, `pending_orders`, `equity`, `trades`, `account_hash`, cash, equity, fills, and data hash were equal. Only benchmark-related effective configuration and config/execution identity differed.

### Known concern

`configs/history-holdout.json` names `research_cache_verified/account_policy.json`, but that file is absent. The production tool reads the existing holdout output `data/sources/history_holdout/account_policy.json`; it was present, hash-checked, and was not replaced with a default. This is recorded as a source/configuration gap, not hidden.

## next_action

Independent review of Task1. Only after review should Task2 be considered. The confirmed single-session-segment skip rule belongs to Task2 and was not implemented here.

## changedpaths

Implementation/tests changed for Task1:

- `src/protocal.py`
- `src/history_data.py`
- `tools/prepare_history.py`
- `src/application.py`
- `src/workbench/models.py`
- `src/workbench/benchmark.py`
- `src/core/backtest.py`
- `src/config.py`
- `tests/test_data_protocol.py`
- `tests/test_history_data.py`
- `tests/history_fixture.py`
- `tests/test_task3_protocol.py`
- `tests/test_workbench_benchmark.py`

Evidence written under `data/runs/` includes the Task1 full/short AB runs and Research-readable manifests/results. Existing unrelated dirty files were preserved. No frontend path was edited. No commit or push was made.
