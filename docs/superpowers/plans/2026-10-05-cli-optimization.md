# CLI Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make `lianghua.cli` a thin, discoverable command dispatcher while preserving the current `ingest`, `run`, and `report` behavior.

**Architecture:** Keep `cli.py` as the composition root for command-line concerns only. Extract argument construction, command handlers, and the `run` application workflow into small functions with explicit inputs and return codes. Do not move domain rules into the CLI or add a second configuration path.

**Tech Stack:** Python 3.12+, `argparse`, `unittest`, existing `RunConfig`, strategy loader, and backtest adapter interfaces.

**Spec:** [Command Line Interface Guidelines](https://clig.dev/)

## Global Constraints

- Keep `argparse`; do not replace it with Click, Typer, or a new CLI framework.
- Preserve the existing commands and their option names: `ingest`, `run`, and `report`.
- Keep machine-readable results on stdout and diagnostics on stderr.
- Do not add fallback adapters, silent parameter conversion, or strategy-name branches.
- Keep the CLI as an adapter; business rules stay in existing application and domain modules.
- Follow the repository rule: use simple names, explicit inputs/outputs, and meaningful focused tests.

## Review Focus

- `lianghua-backtest --help` and each subcommand help must be useful without reading source code.
- A successful `run` must keep its JSON result on stdout and return exit code 0.
- Invalid configuration or failed preflight must return non-zero without printing a fake result.
- Unexpected command failures must not produce a success code or mix diagnostics into JSON stdout.
- `report` must preserve the child builder's failure status and arguments.

### Task 1: Freeze current CLI behavior

**Files:**
- Create: `tests/test_cli.py`
- Read: `src/lianghua/cli.py`

**Interfaces:**
- Consumes: `main(argv: Sequence[str] | None) -> int`
- Produces: characterization tests for command parsing, validation failure, and output streams.

- [ ] **Step 1: Write tests for the current public commands**

  Cover `--help`, missing required arguments, `run` validation rejection, and a successful command using a stubbed application boundary. Assert exit code and stdout/stderr separately.

- [ ] **Step 2: Run the focused tests**

  Run: `python -m unittest tests.test_cli -v`

  Expected: the tests pass against the current behavior, or record the exact behavior that must be intentionally changed in Task 3.

### Task 2: Split parsing and command dispatch

**Files:**
- Modify: `src/lianghua/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Produces: `build_parser() -> argparse.ArgumentParser`, `cmd_ingest(args) -> int`, `cmd_run(args) -> int`, `cmd_report(args) -> int`, and `main(argv: Sequence[str] | None) -> int`.

- [ ] **Step 1: Add parser construction tests**

  Assert that `build_parser()` exposes exactly the three current subcommands and that each subcommand owns its own options.

- [ ] **Step 2: Implement `build_parser()`**

  Move only parser construction into this function. Add concise program description and one example per subcommand; retain existing option names and defaults.

- [ ] **Step 3: Implement command handlers**

  Move the three branches out of `main()`. `cmd_ingest` calls `ingest_exploratory`; `cmd_report` calls the existing Node builder; `cmd_run` delegates to a separate run workflow. Each handler returns an integer and does not call `sys.exit`.

- [ ] **Step 4: Make `main()` parse and dispatch only**

  `main()` should call `build_parser()`, parse arguments, and dispatch by a handler selected from the parsed command. It should not import the strategy, adapter, or JSON configuration modules inside the dispatch body.

- [ ] **Step 5: Run focused tests**

  Run: `python -m unittest tests.test_cli -v`

  Expected: all characterization and parser tests pass.

### Task 3: Isolate the `run` application workflow and CLI error policy

**Files:**
- Create: `src/lianghua/cli_run.py`
- Modify: `src/lianghua/cli.py`
- Test: `tests/test_cli.py`
- Verify: `tests/test_cli_validation_gate.py`

**Interfaces:**
- Produces: `run_backtest(config_path: Path, root: Path) -> dict` in `cli_run.py`.
- Consumes: `RunConfig`, `load_strategy`, the configured adapter, and the adapter's `check`, `context`, and `run` methods.

- [ ] **Step 1: Write application-workflow tests**

  Use a small fake adapter to assert the order `load config → build RunConfig → build adapter → load strategy → check → context → run`. Assert that a non-empty check result stops before `context` and `run`.

- [ ] **Step 2: Move the workflow into `run_backtest()`**

  Move JSON loading, adapter import, strategy loading, preflight checking, effective configuration creation, and run invocation into `cli_run.py`. Keep the existing adapter contract and generated run ID behavior.

- [ ] **Step 3: Define stream and exit behavior**

  Keep successful JSON results on stdout. Send human-readable errors to stderr. Convert expected input/configuration failures to a non-zero return from `cmd_run`; do not print a result after failure. Preserve `argparse` usage errors for malformed command syntax.

- [ ] **Step 4: Run the CLI regression suite**

  Run: `python -m unittest tests.test_cli tests.test_cli_validation_gate -v`

  Expected: all tests pass and the validation gate still blocks unaccepted backtests.

### Task 4: Verify discoverability and integration

**Files:**
- Modify: `README.md`
- Modify: `src/lianghua/cli.py` only if help text needs correction
- Test: `tests/test_cli.py`

- [ ] **Step 1: Add help-output assertions**

  Assert that top-level help names the three commands and that each subcommand help includes its purpose, required inputs, and one runnable example.

- [ ] **Step 2: Document the first-use path**

  Add a short CLI section to `README.md` showing `lianghua-backtest --help`, the three commands, and the distinction between CLI output and diagnostics.

- [ ] **Step 3: Run the full relevant checks**

  Run: `python -m unittest discover -s tests -v`

  Expected: the existing test suite passes; no data fetch or network access is required.

## Completion Criteria

- `main()` contains only parser creation, parsing, command lookup, and dispatch.
- The `run` workflow is independently readable and testable without invoking the CLI parser.
- Help output lets a new user discover the three commands and a first example.
- stdout remains safe for JSON pipelines; errors use stderr and non-zero exit codes.
- Existing backtest validation gates and command names remain unchanged.
