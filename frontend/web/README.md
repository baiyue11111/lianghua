# Web 界面

苹果风格界面。入口 `src/Workspace.tsx`，地址与路由在 `src/app/router.ts`，视觉系统 `src/apple/theme.css`，页面与通用组件在 `src/apple/`。

侧栏只列**目的地**（行情 / 策略研究 / 数据中心）；新建回测与单次回测是对象下面的动作和层级，走地址而不是导航。

| 地址 | 页面 | 内容 |
| --- | --- | --- |
| `#/market` | 行情 | 基准指数卡、自选与证券检索、K 线 / 成交量 / 均线、证券信息、基准收盘曲线 |
| `#/strategies` | 策略研究 | 策略分组表（搜索、筛选、列头排序、分页）、运行状态、数据集 |
| `#/strategies/:key` | 策略详情 | 策略定义与参数、每次配置尝试的收益/回撤/夏普与状态，点行进入单次回测 |
| `#/runs/:id` | 回测结果 | 四项指标、权益曲线与基准对照、effective_config 配置详情、成交记录分页、数据限制 |
| `#/runs/new` | 新建回测 | 策略参数表单 → backtest.prepare 预检 → backtest.run 运行与对比 |
| `#/data` | 数据中心 | 数据源、快照与审核、体检、修复、调度、诊断命令、35 个 operation 覆盖对照 |

地址是位置的唯一来源：刷新、分享、后退都还原到同一个对象；选择不存在时页面明确说「不在存档中」，不会退回到「第一个策略」。

交互核心 `frontend/display` 注册的全部 35 个 operation 都有数据位或触发入口，清单位于 `src/apple/ops.ts`；数据中心页调用 `features()` 实时核对注册表并显示覆盖数。读操作在页面挂载时加载一次，写操作一律 `automatic=false`，只由用户事件触发，输入按注册 schema 组装。

展示模型位于 `src/research/model.ts`，格式工具位于 `src/shared/format.ts`。组件测试挂载现行 `Workspace`，使用固定的后端响应。

## 刷新策略

`features.ts` 不为 operation 声明轮询。页面挂载时读一次，用户可主动刷新。数据中心的「刷新数据」重新读取状态、来源、任务和调度。策略存档列表只读索引，打开单次回测时再取详情。

K 线显示已存区间。指数卡读取每个基准已有的区间。回测结果页用权益曲线的起止日期查询对照基准。

## 协议与验证

浏览器通过 `src/backend.ts` 调用 `POST /api/protocol`。页面通过 `useOperation` 读取交互状态并提交操作，核心校验输入、追踪请求和结果。页面样式来自 `src/index.css` 和 `src/apple/theme.css`。

运行验证：

```sh
npm run test:dev
npm run lint
npm run build -- --outDir /tmp/readable-web-build
```

受控浏览器检查页：

```sh
node scripts/visual.mjs
```

命令输出临时 HTML 路径。此页使用真实组件与交互核心，后端是固定测试替身，不连接生产服务。可用页面片段：`#market`、`#strategies`、`#result`、`#config`、`#trades`、`#data`、`#run`。未登记的只读 operation 返回空资源以显示空态，写操作仍被测试替身拒绝。

组件测试通过服务端渲染调用真实事件，未替换组件或 hooks；它不验证浏览器生命周期、实际图表绘制或手机尺寸。
