# Single Protocol Entry Implementation Plan

**Goal:** Make `frontend/ui-protocol.py` and `src/protocal.py` the only frontend/backend boundary and remove every live business route that bypasses it.

## Constraints

- Do not change `frontend/ui-protocol.py`.
- `src/core/kernel.py` remains the only backtest orchestrator and is imported only by `src/protocal.py`.
- Browser and CLI code may translate transport data only; they cannot call business services.
- Every public operation is declared in `src/protocal.py`; no compatibility route or fallback remains.
- Preserve unrelated dirty work.

## Tasks

1. Add failing boundary tests for one HTTP protocol route, one browser backend port, and no frontend business imports.
2. Replace the legacy FastAPI routes with one `UiRequest -> Protocol.send -> UiMessage[]` transport.
3. Connect the React and CLI adapters to the same backend port without changing the UI contract.
4. Move backend operation implementations behind the module ports declared by `src/protocal.py`; remove old direct router/application entry paths.
5. Verify targeted and full suites, then regenerate a source-backed Archify diagram from the final code.
