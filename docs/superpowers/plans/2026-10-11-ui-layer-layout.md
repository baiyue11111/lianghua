# UI Layer Layout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把交互层核心集中到 `frontend/display/`，把 React Web 应用集中到 `frontend/web/`，让两者通过明确的边界协作。

**Architecture:** `frontend/display/` 只保存不依赖 React、浏览器或 HTTP 的交互模型、端口和数据变换。`frontend/web/` 保存 React、Vite、浏览器 HTTP 适配器和 Web 页面。`frontend/ui-protocol.py` 继续留在 `frontend/` 根目录，作为两套标准协议的共享契约；`frontend/api/` 和 `frontend/web.py` 继续作为后端 HTTP 适配实现，不混入显示核心。

**Tech Stack:** TypeScript、React、Vite、Python、unittest。

**Spec:** `docs/interaction-entrypoints.md`、`docs/ui-protocol-audit.md` 以及当前对话中确定的目录边界。

## Global Constraints

- `frontend/display/` 不得依赖 React、JSX、浏览器 API、Vite 或具体 HTTP 路径。
- `frontend/web/` 承载 React、页面组件、浏览器适配器和 Web 构建配置。
- `frontend/ui-protocol.py` 保留在 `frontend/` 根目录，不复制协议定义。
- `frontend/api/` 与 `frontend/web.py` 不被伪装成交互层核心。
- 不保留同一份核心类型或数据变换的副本。
- 不改变后端业务行为，只改变目录边界、导入路径和文档。

## Review Focus

- Web 工程从新目录启动、构建和测试时，不能再隐式依赖旧的 `frontend/display` React 工程。
- `frontend/display/` 中不能残留 `.tsx`、React 导入、CSS、Vite 配置或 HTTP 请求实现。
- 纯交互模型从 Web 目录移动后，React 页面仍使用同一份类型和数据变换代码。
- CLI、Python HTTP 服务和协议文件的现有导入路径不能被目录移动误伤。
- 开发启动器、真实 UI 脚本和文档中的路径必须与新结构一致。

### Task 1: Add the layout contract test

**Files:**
- Create: `tests/test_frontend_layout.py`

**Interfaces:**
- Consumes: filesystem paths under `frontend/display/` and `frontend/web/`.
- Produces: a repeatable guard for the new directory boundary.

- [x] **Step 1: Write the failing test**

  Assert that `frontend/web/package.json` and `frontend/web/src/main.tsx` exist, that `frontend/display/src/workbench/port.ts` exists, and that `frontend/display` contains no React or browser files.

- [x] **Step 2: Run the test and verify it fails**

  Run: `.venv/bin/python -m unittest tests.test_frontend_layout`
  Expected: FAIL because the React application is still under `frontend/display/`.

### Task 2: Move the React application to `frontend/web/`

**Files:**
- Move: `frontend/display/` React project files to `frontend/web/`.
- Create: `frontend/web/` as the React application root.
- Keep: `frontend/display/` as a source-only interaction-core directory.

**Interfaces:**
- Consumes: existing React/Vite project and pure TypeScript workbench contracts.
- Produces: a runnable `frontend/web` React application and a React-free `frontend/display` core.

- [x] **Step 1: Move the complete React project to `frontend/web/`**

  Preserve `package.json`, lockfile, Vite/TypeScript configuration, public assets, scripts and all React source files. Move local ignored dependencies with the project when present.

- [x] **Step 2: Move framework-independent interaction modules back into `frontend/display/src/`**

  Move `workbench/port.ts`, `curves.ts`, `format.ts`, `locale.ts`, `research-model.ts`, `strategy-model.ts` and `lib/closed-loop.ts`. Leave React components, JSX, CSS, hooks, HTTP clients and Vite files in `frontend/web/`.

- [x] **Step 3: Update Web imports to consume the display core**

  Add one `@display/*` TypeScript/Vite alias and update React files to import the moved modules through that alias. Keep one source for each type and transformation.

### Task 3: Update scripts, documentation and structural checks

**Files:**
- Modify: `frontend/web/scripts/dev.mjs`
- Modify: `frontend/web/scripts/strategy-real-ui.mjs`
- Modify: `frontend/web/scripts/strategy-ui.mjs`
- Modify: `frontend/web/README.md`
- Modify: `docs/interaction-entrypoints.md`
- Modify: `docs/ui-protocol-audit.md`
- Modify: `tests/test_frontend_layout.py`

**Interfaces:**
- Consumes: new `frontend/web` project root and `frontend/display` core paths.
- Produces: path-correct development, real UI, documentation and test commands.

- [x] **Step 1: Update scripts and docs to use `frontend/web/` for React**

  Keep Python API imports pointed at `frontend.web:app`; only React project paths change.

- [x] **Step 2: Extend the layout test to reject React leakage**

  Check for `.tsx`, React imports, CSS, Vite files and `/api/workbench` request code under `frontend/display/`.

- [x] **Step 3: Run the focused Python layout test**

  Run: `.venv/bin/python -m unittest tests.test_frontend_layout`
  Expected: PASS.

### Task 4: Verify the moved application

**Files:**
- No new production files.

**Interfaces:**
- Consumes: the completed directory layout.
- Produces: evidence that the React build, UI scripts and existing protocol tests still work.

- [x] **Step 1: Build the React application**

  Run: `npm --prefix frontend/web run build`
  Expected: PASS.

- [x] **Step 2: Run the Web adapter tests**

  Run: `npm --prefix frontend/web run test:dev`
  Expected: PASS.

- [x] **Step 3: Run targeted Python protocol and CLI tests**

  Run: `.venv/bin/python -m unittest tests.test_ui_protocol tests.test_protocal tests.test_cli tests.test_cli_run`
  Expected: PASS or only pre-existing failures unrelated to this move.

- [x] **Step 4: Check the diff**

  Run: `git diff --check`
  Expected: no whitespace errors.

## Rulings

- The current checkout is `main` with unrelated uncommitted migration work. This task edits the current workspace because the user explicitly asked to reorganize the current project; unrelated files will not be staged or committed.
- `frontend/ui-protocol.py` remains at the frontend root because it is a shared contract, not an implementation owned by either `display` or `web`.
- `frontend/api/` remains outside `display` because it contains HTTP/backend adapters, not framework-independent interaction-core code.
