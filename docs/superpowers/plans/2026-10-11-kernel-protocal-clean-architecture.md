# Kernel and Protocal Clean Architecture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `src/core/kernel.py` the resident backtest use case and add `src/protocal.py` as the only backend protocol boundary, with strategies and external components remaining replaceable.

**Architecture:** The inner `core` layer owns immutable contracts, plug-in ports, and the fixed `Kernel` orchestration. `src/protocal.py` is an outer application boundary that depends inward on `core`, accepts a typed backtest request, invokes a resident `Kernel`, and returns a typed response. It imports neither CLI nor frontend code.

**Tech Stack:** Python 3.12+, `dataclasses`, `typing.Protocol`, `unittest`, existing `.venv/bin/python` test runner.

**Spec:** User request in the current conversation; supporting boundary notes in `docs/strategy-plugin-research.md` and `docs/ui-protocol-audit.md`.

## Global Constraints

- `src/core/kernel.py` is resident and non-pluggable; it owns orchestration only.
- Strategies, data sources, risk models, market adapters, execution models, accounts, and recorders remain plug-ins behind `src/core/interfaces.py`.
- `src/protocal.py` is the only new backend connection boundary; it must not import `frontend` or `cli`.
- Do not modify `frontend/ui-protocol.py` or any frontend adapter in this task.
- Do not add fallback paths, strategy-name branches, or a dependency-injection container.
- Preserve existing kernel order: execute pending orders, apply account, generate strategy decision, apply risk, plan orders, record result.
- Use explicit immutable request/response data and fail on invalid contracts.
- Preserve unrelated dirty work in the shared worktree. Do not reset, clean, commit, or publish.

## Review Focus

- A protocol call must route through the resident kernel and return the exact ordered `StepResult` tuple; test `tests/test_protocal.py`.
- A protocol request with a non-`DataSource` value must fail clearly instead of silently treating it as a fallback source; test request validation.
- The backend protocol must have no CLI or frontend import path; test module dependency surface.
- Existing plug-in order and closed-loop accounting must remain unchanged; run `tests/test_core_kernel.py` and `tests/test_closed_loop_cn_equity.py`.
- The protocol must not mutate or replace the kernel's plug-ins; test the same kernel instance and pending-order state.

---

### Task 1: Freeze the backend protocol boundary with failing tests

**Files:**
- Create: `tests/test_protocal.py`
- Read only: `src/core/kernel.py`, `src/core/interfaces.py`, `src/core/contracts.py`

**Interfaces:**
- Consumes: `Kernel.run(source: DataSource) -> tuple[StepResult, ...]`.
- Produces: Required public shape for `src/protocal.py`: `BacktestRequest(source: DataSource)`, `BacktestResponse(results: tuple[StepResult, ...])`, `BacktestProtocol.run(request: BacktestRequest) -> BacktestResponse`.

- [ ] **Step 1: Write the failing tests**

  Cover one real kernel run through the protocol, invalid source rejection, exact result order, and the absence of `frontend`/`cli` imports from the backend boundary.

- [ ] **Step 2: Run the new tests to verify they fail**

  Run: `.venv/bin/python -m unittest tests.test_protocal -v`

  Expected: import failure because `src/protocal.py` does not exist.

### Task 2: Implement the protocol boundary and tighten the kernel boundary

**Files:**
- Create: `src/protocal.py`
- Modify: `src/core/kernel.py` only if the contract tests identify a kernel boundary defect
- Modify: `pyproject.toml` only if packaging needs to expose the new top-level module

**Interfaces:**
- Consumes: `core.contracts.MarketFrame` and `StepResult`, `core.interfaces.DataSource`, and `core.kernel.Kernel`.
- Produces: `PROTOCOL_NAME = "lianghua.backtest"`, `PROTOCOL_VERSION = 1`, frozen `BacktestRequest`, frozen `BacktestResponse`, and concrete `BacktestProtocol`.

- [ ] **Step 1: Implement `BacktestRequest` and `BacktestResponse`**

  `BacktestRequest.source` accepts only an iterable that satisfies the data-source port and rejects `None` or non-iterable values. `BacktestResponse.results` is an immutable tuple of `StepResult` values.

- [ ] **Step 2: Implement `BacktestProtocol.run`**

  The constructor receives one resident `Kernel`. `run` calls only `kernel.run(request.source)` and returns `BacktestResponse`; it does not load modules, parse CLI arguments, import frontend code, or select a fallback.

- [ ] **Step 3: Run the targeted tests**

  Run: `.venv/bin/python -m unittest tests.test_protocal tests.test_core_kernel tests.test_closed_loop_cn_equity -v`

  Expected: all targeted tests pass.

### Task 3: Verify the architecture against the existing suite

**Files:**
- Verify: `src/core/kernel.py`
- Verify: `src/protocal.py`
- Verify: `tests/test_protocal.py`

**Interfaces:**
- Consumes: completed core/protocol implementation and existing test suite.
- Produces: fresh test evidence and a diff limited to the requested backend boundary plus its plan/tests.

- [ ] **Step 1: Run the full Python test suite**

  Run: `.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v`

  Expected: all tests pass; any pre-existing failure is reported by file and test name.

- [ ] **Step 2: Run static hygiene checks**

  Run: `git diff --check` and `rg -n "^(from|import) (frontend|cli)|from frontend|import frontend" src/protocal.py`

  Expected: `git diff --check` is clean and the import search returns no matches.

- [ ] **Step 3: Inspect the final diff and report exact scope**

  Confirm that frontend files were not modified and that the protocol does not make the kernel replaceable or connect it directly to CLI.
