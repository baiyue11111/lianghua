# 统一数据协议与数据获取模块实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 protocal.py 定义统一数据协议，用 src/collect 三层模块替换现有报价与共用采集管理链路，证明显示和存储均经过统一总线。

**Architecture:** 一个 DataStore、一个 DataBus。CollectPort 接现有 UI 业务操作；core 管流程；data 获取供应商数据并调用注入的 DataBus。数据调用不重新进入 UI 分发。

**Tech Stack:** 现有 Python、unittest、Pydantic、JSON 和 urllib；不增加依赖。

**Spec:** `docs/superpowers/specs/2026-10-12-data-protocol-collect-design.md`

## Global Constraints

- 本计划待实施，不把文档作为完成证明。
- frontend、固定回测内核本次不修改；不启动常驻端口。
- protocal.py 是唯一公共总线和数据契约源；不新建 protocol.py 拼写别名。
- 一个 DataStore、一个 DataBus；业务模块只接收 DataBus。
- 不增加 fallback、自动重试、旧路径读取、转发别名或重复 Store。
- 成功协议字段、供应商字段、raw_page、快照哈希规则保持不变。
- 外部股价试点为既有 EastmoneyQuotes；共用任务/快照/调度随它迁移；不重写其他供应商。
- 使用当前工作树，保护既有未提交改动；提交只选本任务差异，不能盲目 git add 全项目。

## Review Focus

- 损坏证据不得被当成尚无数据：Task 2 校验 hash/schema，错误不吞掉。
- 原始响应缺证券、重复证券、缺时间或非法数值：Task 3 拒绝成功发布，保留已获得的证据。
- 快照写入失败、指针写入失败、任务状态保存失败：Task 3 不能虚报成功；源任务锁必须释放。
- 正在采集时重复请求、close 时调度到期：Task 3 验证忙状态及线程收尾。
- 同步内部数据调用导致循环导入或递归 UI 分发：Task 1、4 验证单独导入与完整调用链。

## 文件职责与删除范围

| 文件 | 改动 |
|---|---|
| src/protocal.py | 定义 DataPort/DataRef/DataError/DataBus，装配并接 CollectPort |
| src/storage.py | 保留物理 I/O；删除重复契约；支持按 identity 列举及稳定错误 |
| src/collect/display.py | 接 sources/sync/schedule/job/snapshot/evidence/reviews 的既有操作 |
| src/collect/core.py | 请求模型、SyncService、Scheduler；不访问文件或供应商实现 |
| src/collect/data.py | 共用 Store 业务编解码；只通过 DataBus 存储 |
| src/collect/quotes.py | 单个报价采集模块：EastmoneyQuotes 网络请求与响应解析 |
| src/collect/__init__.py | 空初始化，无实例或线程 |
| src/workbench/port.py | 删除 source 操作和相应线程/调度逻辑 |
| src/workbench/providers.py | 删除 EastmoneyQuotes；其他供应商暂留 |
| src/workbench/models.py | 删除迁出的 source 模型，保留研究/回测/基准模型 |
| src/workbench/store.py、schedule.py | 迁移完成后删除，不保留 re-export |
| src/workbench/benchmark.py 等消费者 | 更新 Store 导入及注入；业务计算不改 |
| 现有 DataPort 类型消费者 | 将类型引用改为 TYPE_CHECKING 的 protocal 契约；消除循环导入 |
| configs/workbench.json、pyproject.toml | 更新报价 adapter 和 package 配置 |
| tests/collect/、tests/test_data_store.py、tests/test_protocol_boundary.py | 试点合同、失败、边界与 AB 证据 |

## 实施前：保存旧链路 AB 基准

- [ ] 在改业务代码前，固定报价请求、供应商响应、采集时钟和任务 ID，运行旧完整链路，保存独立核对过的记录、证据、快照 JSON、snapshot_id、文件哈希和 UI payload 到 tests/collect/fixtures/。不冻结消息随机 ID。
- [ ] 基准保存后才开始 Task 1；Task 5 只对这些基准运行新版对比，不依赖已删除的旧实现。

## Task 1：集中数据协议并建立唯一 DataBus

**Interfaces:** 使用设计文档中的四方法签名。`DataBus(store: DataPort)` 实现 DataPort；DataRef 六字段；DataError(code: str, detail: str)。装配只构造一次。

- [ ] 在 tests/collect/test_protocol.py 写合同测试：写 sources/quotes/snapshot.json 的 bytes，读取完全一致，返回大小和 SHA-256；kind 和 identity 越界被拒绝；缺失返回 missing；真实写入失败返回 io_failed。
- [ ] 添加按 identity 列举测试：quotes 的调用不读取其他 identity 的内容；损坏其他分类文件不影响结果。
- [ ] 运行 `.venv/bin/python -m unittest tests.collect.test_protocol -v`，确认新增能力或稳定错误未实现导致失败。
- [ ] 在 protocal.py 定义契约和 DataBus；在 storage.py 删除重复类型。模块只做类型引用，运行时装配延迟导入。文件哈希采用分块读取，避免一次把大文件读入内存。
- [ ] 更新现有类型引用并接线，一个 DataBus 包装一个 DataStore；不得让业务模块直接持有 DataStore。
- [ ] 运行上述测试及 `tests.test_data_store`，全部通过；用导入阻断器确认协议类型可用且不触发装配。
- [ ] 提交本任务文件；在执行记录中写明测试结果和共享口的构造次数。

## Task 2：建立共用 collect/data 与单文件 collect/quotes

**Consumes:** Task 1 的 DataPort；注入 transport Callable。**Produces:** `Store(data: DataPort)` 保留现有 read/write/items/snapshot/get_snapshot/latest/review/reviews；`EastmoneyQuotes(config, transport)` 保留 collect(request) 结果结构。

- [ ] 新建 tests/collect/test_data.py，固定请求两个证券，固定供应商 JSON。独立手工断言 code、price、volume、amount、data_at、raw_page 和原始响应不变。
- [ ] 测试 Store 的 snapshot 保存原始响应及规范记录，哈希复核正确；只经 DataBus 读写；损坏数据必须失败。
- [ ] 测试失败响应保留已获得 raw；不把缺字段或重复证券修成正常记录；网络超时不自动重试。
- [ ] 运行 `.venv/bin/python -m unittest tests.collect.test_data -v`，确认失败后实现 collect/data.py 与 collect/quotes.py。
- [ ] 迁移既有 Store 到 collect/data.py，EastmoneyQuotes 到 collect/quotes.py，删除旧定义。Store 不调用 path.exists；缺失通过明确的 DataError.missing 处理，其他错误继续上报。
- [ ] 运行测试通过；确认 collect/data.py 不出现物理文件 I/O、根目录或 DataStore 构造。
- [ ] 保存变更和测试结果，提交本任务文件。

## Task 3：建立 collect/core，迁移同步与调度

**Consumes:** Store 和 collect(request)；不导入其具体实现。**Produces:** SyncService 保留 begin/execute/run；Scheduler 保留 configure/tick/start，并提供 close() 停止调度、等待调度线程退出；请求/状态模型迁入 core。时间与任务 ID 使用注入 callable 以便独立复现，生产默认值只在装配设置。

- [ ] 在 tests/collect/test_core.py 写成功流程测试：running → 调用供应商 → 快照 → latest → succeeded；记录写入顺序。
- [ ] 写失败测试：超时、格式错误、写快照失败不更换旧 latest；保存失败证据；任务锁释放，后续任务可开始。
- [ ] 写指针写失败与最终任务保存失败测试：不得返回 succeeded；不得吞异常。允许保留未发布快照作为失败现场，不添加跨文件事务框架。
- [ ] 写同源并发与 Scheduler.close 测试：重复 begin 明确失败；关闭后无新任务；不能靠睡眠掩盖竞态。
- [ ] 运行 `.venv/bin/python -m unittest tests.collect.test_core -v`，失败后迁移 SyncService、Scheduler、source 请求与状态模型到 core.py。
- [ ] 用阻断导入器测试 core 导入不加载 frontend、storage、urllib 或供应商 SDK；测试全部通过后删除旧 schedule.py 与 store.py。
- [ ] 保存并提交本任务变更。

## Task 4：建立 collect/display 并接入两个协议口

**Consumes:** core 的服务及 source 模型；结果仍是 JSON 业务结构。**Produces:** `CollectPort(sources, service, scheduler)` 的 handle(operation, data)、start()、close()。

- [ ] 写 tests/collect/test_display.py，逐项验证原有 sources/sync/job/schedule/snapshot/evidence/failedEvidence/reviews/review 输入与输出；分页及非法索引明确报错。
- [ ] 写协议集成测试：已有 UiRequest 进入 Protocol，CollectPort 输出由 Protocol 包装为既有 UiMessage；schema、kind、correlation_id 与成功数据结构不变。
- [ ] 完整调用中给 DataBus 记录真实读写，确认来源是同一实例；不能直接传 DataStore 给 Store。内部数据调用不得触发第二次 CollectPort.handle。
- [ ] 运行 `.venv/bin/python -m unittest tests.collect.test_display tests.test_protocol_boundary -v`，确认失败后迁移 source handlers，WorkbenchPort 删除重复方法。
- [ ] build_backend 直接装配 CollectPort 作为 Modules.sources；WorkbenchPort 保留研究与回测职责。各服务共享一个 Store，不在多个层重建。
- [ ] 更新 quotes adapter 配置及所有 Store/source-model 消费者导入；Benchmarks 行为不变。更新 pyproject 包声明。
- [ ] 删除旧 EastmoneyQuotes、source handlers、旧 import 与转发入口；测试通过后提交。

## Task 5：冻结 AB 验证与消融检查

**Files:** tests/collect/fixtures/quote-response.json、quote-request.json、expected-snapshot.json；tests/collect/test_ab.py；docs/collect-protocol-check.json。

- [ ] 读取实施前保存的 AB 基准并复核期望值；不把旧实现复制到生产或保留 fallback。
- [ ] 对新链路输入相同请求/响应/时钟/ID。对比记录、原始证据、快照 JSON、snapshot_id、文件 SHA-256、任务状态和 UI payload。消息随机 ID 单独验证关系，不用于业务哈希。
- [ ] 比较失败行为：超时与损坏不发布新 latest。新增失败原始证据单独标明为改进，不伪称旧版完全一致。
- [ ] 做消融检查：把 DataBus 替换为明确拒绝写入的合同实现，采集不得显示成功、不得在临时项目落文件。恢复正常 DataBus 后完整链路成功。
- [ ] 模块测试 `.venv/bin/python -m unittest discover -s tests/collect -p 'test_*.py' -v` 必须全部通过。
- [ ] 仅运行受接线影响的现有检查：tests.test_workbench、tests.test_workbench_benchmark、tests.test_protocal、tests.test_protocol_boundary、tests.test_strategy_backtests，以及 `npm run test:dev`（frontend/web）。不以全系统测试代替模块 AB。
- [ ] 静态检查新 collect 模块无 open/read_text/write_text/read_bytes/write_bytes/mkdir/持久数据库连接或任意 output/root 参数；标准数据类型仅有一份；旧定义已删除。误命中的业务 read/write 方法逐项解释。
- [ ] 不启动服务、不调用线上报价做 AB（两次实时响应会变化）；独立 HTTP transport 合同用捕获响应测试，不把冻结样本称作线上可用性证明。
- [ ] 写结果 JSON：每个比较项、哈希、测试命令、失败场景和剩余绕过范围。审查最终差异并交付三层图。

## 完成标准与后续清理

本计划完成时，仅宣告“标准数据协议 + 外部报价及共用采集管理试点完成”。数据湖 tools/lake.py、历史输入、其他验证工具、旧 runs 清理继续属于上一个未完成任务。保留 data-io-audit 清单，把本次已迁移项标为完成，其他项保持未迁移。

后续按此顺序复用已验证的口：数据湖 → 历史输入与快照制作 → 回测审计 → 其他采集/验证工具 → 离线实验与旧入口删除。每次迁移独立做模块 AB；不再为每个数据类型新增存储协议。
