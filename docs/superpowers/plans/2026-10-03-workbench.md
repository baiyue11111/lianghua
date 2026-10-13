# 真实量化工作台 Implementation Plan

**Goal:** 将 OpenDesign 工作台迁入 React，真实读取运行文件并提供可审核的数据同步。
**Architecture:** UI 只调用 WorkbenchPort；HTTP 是该端口的一个实现。服务只依赖 SourceAdapter.collect(request) 和 ResearchAdapter.list/detail，供应商实现通过配置导入。原始响应、任务、快照、审核记录分别持久化，不自动换源。
**Tech Stack:** React/TypeScript/Vite、FastAPI/Pydantic、Python 标准库、现有 baostock。
**Spec:** 用户已授权真实接入、实测、双方可插拔。OpenDesign project-d590 为视觉参考。

## Global Constraints
- 不修改策略规则，不把工程金样当历史收益；旧探索运行指标标注未验收。
- 公告原文链接与原始响应保留，财报目录不冒充财务报表数值。
- 数据截至时间、采集时间、报告期、公开时间分开。
- 来源错误明确报错；同步失败保留此前快照并标为旧快照。
- 实时和日线分开；当前范围和记录数公开，不称为全市场覆盖。

## Review Focus
- 供应商超时、拒绝访问或缺字段：任务失败且旧快照不变化。
- 文件被修改：哈希校验失败，不能展示为通过。
- 分页总数变化、重复记录：拒绝发布新快照。
- 同一来源同时同步：409，防止旧任务覆盖新结果。
- 未验收或未知口径：不生成已验证收益或最新时间。

## Tasks
- [x] 1. 新增 `web/api/workbench/{models,store,providers,research,router}.py`，独立端口与工厂注入；配置 `configs/workbench.json`。
- [x] 2. 先写 `web/api/test_workbench.py`：适配器替换、保存重读、审核、损坏、失败保留、分页与并发；运行失败后实现。
- [x] 3. 新增前端 `web/src/workbench/{port,http,Workbench,workbench.css}`；App 注入 HTTP 实现，策略、审核、同步三个区，原行情终端保留入口。真实接口无演示回退。
- [x] 4. 导出 `/api/workbench` OpenAPI 并验证请求/响应；模块测试、TypeScript build、浏览器操作。
- [x] 5. 分别执行真实来源同步，记录 HTTP 任务状态、原始响应哈希、快照记录数；真实失败也纳入报告，但不宣称该来源通过。浏览器抽样对照文件并保存实测报告。
