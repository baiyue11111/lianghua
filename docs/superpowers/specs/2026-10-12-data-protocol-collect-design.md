# 统一数据协议与数据获取模块试点

状态：待实施设计。本文没有表示代码已经完成迁移。

## 目标与范围

先在 `src/protocal.py` 建立所有业务数据共用的标准口，再用现有东方财富报价链路验证。工程师在这个文件找到协议，在 `src/collect/` 找到外部数据模块。

试点覆盖报价请求、供应商响应、快照、任务、失败证据、审核、调度以及显示结果。其他供应商沿用现有解析实现，但共用的任务和快照管理随试点迁入新模块。不在本次重写数据湖、公告解析、回测、历史清洗或离线实验。

用户要求三个层：display、核心逻辑、获取与读写数据。它们是同一模块的三个职责，不是三个独立入口。

## 文件布局

```text
src/
├── protocal.py           唯一公共总线、数据契约、数据分发、UI 消息转换、装配
├── storage.py            唯一物理存储实现
├── core/                固定回测内核，本次不修改
└── collect/             所有数据获取模块的目录
    ├── __init__.py       不自动创建实例或启动线程
    ├── display.py        共用：接业务操作，输出协议中立结果
    ├── core.py           共用：请求校验、同步流程、任务与调度规则
    ├── data.py           共用：证据编解码、通过协议口读写
    └── quotes.py         一个报价采集模块，获取并解析外部报价
```

`display.py` 不生成网页，不导入 React/FastAPI 或 `frontend/ui-protocol.py`，不获取行情、不保存文件。它向 `protocal.py` 返回数据，后者使用既有 `UiMessage` 送入 UI protocol。

`core.py` 不导入供应商 SDK、文件库、存储实现或 UI 类型。它使用注入的采集器与数据服务，决定操作顺序。

`data.py` 负责共用的数据读写，业务持久化只能使用注入的数据协议口。`quotes.py` 属于第三层，负责报价网络请求及供应商字段解析，只返回记录和原始响应，不保存文件。两个文件都不持有根目录、不创建 DataStore、不调用文件 open、Path.read/write、mkdir 或数据库持久连接。

后续公告、基本面等各放一个采集文件，复用 display/core/data。不要给每种采集器再建一套三层目录，也不把所有采集实现塞进 quotes.py。

## 标准数据协议

继续使用既有四项能力，避免再造一套请求包装：

```python
class DataPort(Protocol):
    def read(self, kind: str, identity: str, name: str) -> bytes: ...
    def write(self, kind: str, identity: str, name: str, value: bytes) -> DataRef: ...
    def list(self, kind: str, identity: str | None = None) -> tuple[DataRef, ...]: ...
    def path(self, kind: str, identity: str, name: str) -> Path: ...
```

在 `protocal.py` 集中定义 DataPort、DataRef、DataError 和 DataBus。DataRef 保留 `kind, identity, name, file, sha256, size`。DataError 提供 `code` 和 `detail`；稳定代码为 `invalid_identity`、`missing`、`io_failed`。模块的证据内容校验失败仍是业务错误，不冒充文件缺失。

DataBus 实现以上契约，统一验证调用参数与错误，再调用唯一 DataStore。内部 bytes 和 Path 不经过 JSON/base64 转换。前端继续只使用 UiRequest/UiMessage；当前试点不把任意文件写入能力作为前端新功能开放。

`list` 增加可选 identity 是为精确列举一个组，避免获取任务时扫描全部行情和数万份分红证据。缺失文件由 read 抛出 `DataError(code='missing')`；业务上允许“尚无快照”的调用方可以把这个明确状态显示出来，不能吞掉损坏或其他 I/O 错误。

kind 只选择数据分类，不选择存储入口。活动分类仍为 sources、runs、state、validation。archive 由清理工具管理，活动协议拒绝它。新分类只在统一分类声明中登记，不新增实现或路径参数。

path 只为必须接收路径的文件库提供完整、校验后的文件名。不能返回目录供模块自行拼接。试点的 JSON 读写不用 path。

## 装配与调用方向

```text
UI protocol → protocal.py → collect/display.py → collect/core.py
                                                  ↓
                                            collect/quotes.py → 外部供应商

collect/core.py → collect/data.py
                      ↓
                                   注入的 DataBus（定义在 protocal.py）
                                                  ↓
                                             DataStore → data/

显示返回：collect/display.py → protocal.py → UI protocol
```

`build_backend` 创建一个 DataStore 和一个 DataBus，把同一个 DataBus 注入数据消费者。不得把 DataStore 直接交给业务模块。Protocol 的装配和 UI 消息转换继续保持唯一。

数据调用直接进入 DataBus 的 read/write/list/path，不再次进入 UI 操作 send，不重新分发到数据获取模块。因此不会出现“获取行情 → 协议 → 再次获取行情”的递归。

模块只在 TYPE_CHECKING 下引用 `protocal.py` 的数据类型，运行时通过注入使用对象。`protocal.py` 的运行时装配导入放在 build_backend 内；storage 构造 DataRef 的导入不触发反向装配。测试用阻断导入器验证单独导入 collect.core 不加载 UI、网络或存储实现。

## 试点迁移

保留现有 `workbench.sources/sync/schedule/job/snapshot/evidence/failedEvidence/reviews/review` 操作名、schema 和成功结果结构。它们全部由 CollectPort 接收；WorkbenchPort 删除相应实现。

迁移 Store 的业务编解码、快照哈希与审核到 collect/data.py；删除 workbench/store.py。迁移 SyncService 和 Scheduler 到 collect/core.py；删除 workbench/schedule.py。迁移 source 请求及输出模型；workbench/models.py 仅保留回测、研究和基准模型。模型名称和字段保留，原位置不保留转发别名。

东方财富的 EastmoneyQuotes 从 workbench/providers.py 移到 collect/quotes.py；配置引用更新为 `collect.quotes:EastmoneyQuotes`。其他供应商暂留原文件，由装配层创建并注入，不给它们新增包装、fallback 或自动替换供应商。

Benchmarks 等原有 Store 消费者更新导入到 collect/data.py 并使用同一个数据服务。此机械接线不改变基准计算。不要复制 Store 到两个目录。

## 数据与失败规则

沿用当前活动数据布局，试点不搬动已有文件，也不引入旧路径读取：

```text
data/sources/snapshots/<snapshot_id>.json
data/sources/failed_evidence/<job_id>.json
data/state/jobs/<job_id>.json
data/state/source_jobs/<source_id>.json
data/state/latest/<source_id>.json
data/state/schedules/<source_id>.json
data/state/reviews/<snapshot_id>-<record_index>.json
```

试点成功顺序：登记 running 任务 → 请求供应商 → 校验响应 → 保存完整原始证据与规范记录 → 更新 latest → 登记 succeeded。失败不得更新 latest；已获取的原始响应保留为失败证据；任务退出 running 状态。先保存证据，再更新指针。

保持供应商字段含义、证券身份、原始响应、raw_page、时间和快照哈希规则。对 malformed 响应增加失败证据属于明确的新行为，不能把它说成与旧版失败路径逐字等价。

不增加自动重试、候选源、修复缺失字段或推测值。网络超时明确失败。写入失败明确反馈，不能报告采集成功；同源任务锁在 finally 释放。调度由 Protocol.start/close 控制，不在导入时启动；close 后不再调度新任务。

## 验收

1. 数据契约与错误只在 protocal.py 定义一次；业务消费者只收到 DataBus。
2. collect 三层职责明确；core 无 UI/网络/文件依赖；display 无数据读写。
3. 报价完整链路的每次业务存储操作经过同一个 DataBus。
4. 原有 UI 请求、响应和操作名保持不变；frontend 无修改。
5. 用同一份固定供应商响应、请求、时钟和任务身份，比较旧版/新版记录、证据、快照内容及哈希，成功结果完全一致。
6. 超时、缺字段、重复证券、非法路径、损坏证据、写入失败、停止调度等有独立测试。
7. 删除试点的旧实现和转发入口；不新增常驻端口。

本次通过只证明数据协议和报价试点成立，不表示全项目绕过点已清零。后续按盘点清单逐模块迁移，沿用这个契约。
