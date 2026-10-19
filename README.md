# 量化研究与回测

本地量化研究项目：比较存档策略运行、抽样核对原始数据、手动或定时同步报价、日线、公告及财报目录。

## 启动

安装 Python 项目依赖后，分别在 `frontend/display` 和 `frontend/web` 执行 `npm ci`。在项目根目录运行 `uv run lianghua-dev`，同时启动 Python 后端和 React。打开 `http://127.0.0.1:3000/`，按 Ctrl+C 一起关闭。端口 3000 和 8000 必须空闲。Web 目录的 `npm run dev` 只启动 React。浏览器和 CLI 共用前端交互核心，通过 `frontend/ui-protocol.py` 连接 `src/backend.py`；HTTP 只有 `POST /api/protocol` 一个业务路由。`src/protocol.py` 定义标准数据结构和校验，`src/market` 负责 DataBus、历史数据读取和只读视图，`src/storage.py` 负责文件存储。后端从 `data/source/registry/sources.json` 登记来源、数据集和基准，再交给固定 Kernel 执行。操作按 `backtest.*`、`research.*`、`collection.*`、`data.*` 分组，旧操作名直接拒绝。

## 命令行入口

查看当前 CLI 参数，或用 JSON 配置运行多区间回测：

```text
lianghua --help
lianghua --backtest config.json
```

配置示例：

```json
{
  "module": "plugins.strategies.moving_average_cross",
  "params": {"fast_window": 5, "slow_window": 20},
  "dataset_id": "cn_a",
  "market": {"scope": "CN_A", "codes": ["600009"]},
  "initial_cash": "1000000",
  "intervals": [
    {"name": "甲", "start": "2024-01-01", "end": "2024-06-30"},
    {"name": "乙", "start": "2024-07-01", "end": "2024-12-31"},
    {"start": "2025-01-01", "end": "2025-06-30"}
  ]
}
```

`intervals` 必须是非空列表；每段提供 `start`、`end`，`name` 可选。每段独立回测，使用相同策略参数和初始资金，各自持有独立账户与持仓。CLI 不接受顶层日期范围或 search 配置。

回测省略 `market` 时默认请求 `CN_A` 全市场。`scope` 的市场定义来自 registry；`codes`、`index` 和 `filters` 可组合，按 AND 取交集。普通市场按 registry 中唯一的 `code_pattern` 从标准证券全集筛选，收集与回测共用该规则，不再维护市场成员名单。指数筛选只读取 registry 的 `members` 绑定所指向的标准成分记录，按生效日期和公开时间解析调入、调出；注册表和回测配置都不保存成分股名单。财务条件需要带可知时间的事实数据。市场未登记或所需数据缺失时会明确拒绝，不会静默缩成可用子集。实际可用范围由 registry 与数据覆盖决定；此配置约定不表示当前数据已覆盖全 A，也不表示 HK 已可回测。

成功结果写入标准输出，错误写入标准错误，并用非零退出码表示失败。策略、数据集和基准由 registry 与现有插件登记，不复制旧的万能配置结构。

## 说明

- [中文策略详解](frontend/web/public/strategies.html)，运行时可访问 `/strategies.html`

中文词汇集中在 `frontend/display/src/research/locale.ts`。只翻译显示内容，API 标识、原始响应和文件哈希保持原样。未知名称保留原文，避免猜测含义。

这是项目首个本地 Git 快照。源代码、配置、测试、锁文件和文档纳入版本管理。原始数据、生成的回测运行、依赖目录和备份留在本机，未纳入 Git。仅检出源码不会自动恢复行情数据和运行存档。

历史策略指标未通过完整经济有效性验收，特别是未复权旧基线不能作为策略效果证据。页面解释当前代码；每个存档运行使用自己的代码版本。

数据只通过 `DataBus` 读写。收集适配器把数据源字段转换为标准字段，回测只读取标准记录。行情位于 `data/source/cn_a`，复权、分红和证券事实位于 `data/source/facts`；原始响应留在 `log` 用于审计。交易日历同时记录交易日和已核实的日期范围。字段缺失会明确报错，不会用旧缓存或默认值补齐。
