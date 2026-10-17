# 量研研究工作台

本地量化研究项目：比较存档策略运行、抽样核对原始数据、手动或定时同步报价、日线、公告及财报目录。

## 启动

安装 Python 项目依赖后，分别在 `frontend/display` 和 `frontend/web` 执行 `npm ci`。在项目根目录运行 `uv run lianghua-dev`，同时启动 Python 后端和 React。打开 `http://127.0.0.1:3000/`，按 Ctrl+C 一起关闭。端口 3000 和 8000 必须空闲。Web 目录的 `npm run dev` 只启动 React。浏览器和 CLI 都通过 `frontend/ui-protocol.py` 连接 `src/protocal.py`；HTTP 只有 `POST /api/protocol` 一个业务路由。后端从 `data/source/registry/sources.json` 登记来源、数据集和基准，由 `DataBus` 读取数据并交给固定 `Kernel` 执行。见[接线状态与验证](docs/interaction-entrypoints.md)。

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
  "dataset_id": "lake",
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

回测省略 `market` 时默认请求 `CN_A` 全市场。`scope` 的市场定义来自 registry；`codes`、`index` 和 `filters` 可组合，按 AND 取交集。全市场或指数筛选需要真实历史成员数据，财务条件需要带可知时间的事实数据。市场未登记或所需数据缺失时会明确拒绝，不会静默缩成可用子集。实际可用范围由 registry 与数据覆盖决定；此配置约定不表示当前数据已覆盖全 A，也不表示 HK 已可回测。

成功结果写入标准输出，错误写入标准错误，并用非零退出码表示失败。策略、数据集和基准由 registry 与现有插件登记，不复制旧的万能配置结构。

## 说明

- [接口与边界](docs/workbench-interfaces.md)
- [后端协议入口](docs/backend-protocol.md)
- [交互入口与适配边界](docs/interaction-entrypoints.md)
- [`ui-protocol.py` 审计与目标协议](docs/ui-protocol-audit.md)
- [真实接入验收](docs/workbench-delivery.html)
- [中文策略详解](frontend/web/public/strategies.html)，运行时可访问 `/strategies.html`
- [统一后端协议](docs/backend-protocol.md)

中文词汇集中在 `frontend/display/src/workbench/locale.ts`。只翻译显示内容，API 标识、原始响应和文件哈希保持原样。未知名称保留原文，避免猜测含义。

这是项目首个本地 Git 快照。源代码、配置、测试、锁文件和文档纳入版本管理。原始数据、生成的回测运行、依赖目录和备份留在本机，未纳入 Git。仅检出源码不会自动恢复行情数据和运行存档。

历史策略指标未通过完整经济有效性验收，特别是未复权旧基线不能作为策略效果证据。页面解释当前代码；每个存档运行使用自己的代码版本。
