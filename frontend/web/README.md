# Web 界面

苹果风格界面。入口 `src/Workspace.tsx`，地址与路由在 `src/app/router.ts`，视觉系统 `src/apple/theme.css`，页面与通用组件在 `src/apple/`。

侧栏只列**目的地**（行情 / 策略研究 / 数据中心）；新建回测与单次回测是对象下面的动作和层级，走地址而不是导航。

| 地址 | 页面 | 内容 |
| --- | --- | --- |
| `#/market` | 行情 | 基准指数卡、自选与证券检索、K 线 / 成交量 / 均线、证券信息、基准收盘曲线、采集与数据状态 |
| `#/strategies` | 策略研究 | 策略分组表（搜索、筛选、列头排序、分页）、运行状态、数据集 |
| `#/strategies/:key` | 策略详情 | 策略定义与参数、每次配置尝试的收益/回撤/夏普与状态，点行进入单次回测 |
| `#/runs/:id` | 回测结果 | 四项指标、权益曲线与基准对照、effective_config 配置详情、成交记录分页、数据限制 |
| `#/runs/new` | 新建回测 | 策略参数表单 → backtest.prepare 预检 → backtest.run 运行与对比 |
| `#/data` | 数据中心 | 数据源、快照与审核、体检、修复、调度、诊断命令、35 个 operation 覆盖对照 |

地址是位置的唯一来源：刷新、分享、后退都还原到同一个对象；选择不存在时页面明确说「不在存档中」，不会退回到「第一个策略」。

交互核心 `frontend/display` 注册的全部 35 个 operation 都有数据位或触发入口，清单位于 `src/apple/ops.ts`；数据中心页调用 `features()` 实时核对注册表并显示覆盖数。读操作在页面挂载时加载一次，写操作一律 `automatic=false`，只由用户事件触发，输入按注册 schema 组装。

`src/market/`、`src/research/`、`src/shared/app.css` 是旧界面组件，保留供现有组件测试使用，**运行时不再挂载**。展示模型仍在 `src/research/model.ts`，格式工具在 `src/shared/format.ts`。

## 刷新策略

`features.ts` 不为任何 operation 声明 `poll`，`data.status` 也不再带后端未实现的 `rescan` 参数。交互核心仍保留轮询能力（`core.ts` 的 `spec.poll` 及其测试），本项目注册项改为「挂载加载一次 + 用户触发刷新」：行情页「刷新状态」重取 `collection.automatic / collection.current / data.status / data.check`，数据中心「刷新数据」重取该页全部读操作。壳层不再常驻挂载 `research.runs`，侧栏统计只保留本地信息与策略目录。

这样改的原因是部分读操作会触发全量事实扫描，`research.runs` 会逐条读取 163 份存档；当单次耗时大于轮询周期时，1s / 3s 自动轮询会让请求在后端 `asyncio.to_thread` 线程池里无限堆积，连 `service.info` 这种零 IO 请求都要排队。状态报告里的全表指纹已彻底删除：`Views._fingerprint()`、`source_facts()` 的 `fingerprint` 开关、`partition_sha256` 返回字段，以及只验证该指纹的测试全部移除，不留按需取证入口。`src/storage.py` 的单表证据摘要是另一套能力，供 `DataPort.evidence()` 与采集链路使用，保持不变。

默认取数区间同样做了收敛：K 线默认只取最近约 250 个已登记交易日，不再从 2003 年全历史开始，避免早期停牌/未上市日产生 NaN 被数据校验拒绝；基准对比的起止日期若落在周末或超出日历末端，由后端 `Benchmarks.series()` 对齐到最近的已登记交易日，底层数据质量校验不放宽。

## 通用 schema 渲染

浏览器仍然是挂载口子；所有请求经过 `src/backend.ts` 的 `/api/protocol`。后端 `src/backend.py` 现在为每个响应补齐 `UiMessage.presentation`（`title / role / fields`）与命名空间 `extensions`，字段提示来自返回数据形状，不规定 React 组件或布局。`frontend/display` 保留完整 `UiMessage`，`src/interaction/model.ts` 的 `useOperation` 在原有 `value / status / error / invoke` 之外同时暴露 `message / presentation / kind / extensions / links`。

`src/apple/Resource.tsx` 保留通用渲染注册表：按 `kind / role / format` 分派 Table、Series、KV、Status、Notice，并按 `FieldHint.path` 读取字段，供后续新 operation 复用。五屏默认使用专用渲染（K 线、权益曲线、数据源表等），不再同时挂载通用视图——同一份 `UiMessage` 渲染两遍只放大浏览器开销，不增加信息。写操作仍保持强 `input_schema`、`prepare / requires / ready / mode=write`，没有泛化成无类型数据。

`src/backend.ts` 与 `frontend/ui-protocol.py` 未改动；协议新增的是对既有 `presentation / extensions` 能力的实际使用。

运行验证：

```sh
npm run test:dev
npm run lint
npm run build -- --outDir /tmp/readable-web-build
```

构建只做无缓存类型检查，不向依赖链接写入，也没有在 TypeScript 配置里写临时路径。

受控浏览器检查页：

```sh
node scripts/visual.mjs
```

命令输出临时 HTML 路径。此页使用真实组件与交互核心，后端是固定测试替身，不连接生产服务。可用页面片段：`#market`、`#strategies`、`#result`、`#config`、`#trades`、`#data`、`#run`。未登记的只读 operation 返回空资源以显示空态，写操作仍被测试替身拒绝。

组件测试通过服务端渲染调用真实事件，未替换组件或 hooks；它不验证浏览器生命周期、实际图表绘制或手机尺寸。
