# Single Data Root Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Keep `src/protocal.py` as the only backend bus and route all active persistence through one data port rooted at `data/`.

**Architecture:** `protocal.py` composes one `DataStore`, injects it into backend services, and exposes only business operations. The kernel stays I/O-free; application orchestration persists completed runs. Legacy top-level `runs*` paths are not supported.

**Tech Stack:** Python 3.12, JSON, CSV, Parquet, unittest.

## Global Constraints

- Do not modify frontend protocol behavior.
- Do not add compatibility readers or fallback paths.
- Keep multiple physical formats under one `data/` root and one `DataPort`.
- Preserve only `runs/acceptance/core_cn_equity_golden_20260930` as current validation evidence.

## Tasks

1. Add and test the single `DataPort` and `DataStore` rooted at `data/`.
2. Inject that one store from `protocal.py` into Workbench, historical input, and runtime services.
3. Move run persistence out of `HistoricalLake`; persist kernel results through the application service.
4. Replace `FileResearch` directory scanning with canonical `data/runs` access.
5. Move the retained validation evidence, remove legacy path configuration, and delete all top-level `runs*` directories.
6. Run targeted tests, AB comparison, full Python tests, frontend protocol tests, and frontend build.

## Acceptance

- No top-level `runs*` directory remains.
- Runtime code contains no legacy run-root scanner or configurable output root.
- One backtest creates only `data/runs/<run_id>/` and is queryable through `workbench.runs` and `workbench.run`.
- Existing frontend protocol request and response shapes remain unchanged.
