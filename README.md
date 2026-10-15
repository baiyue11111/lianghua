# 量研研究工作台

本地量化研究项目：比较存档策略运行、抽样核对原始数据、手动或定时同步报价、日线、公告及财报目录。

## 启动

安装 Python 项目依赖后，分别在 `frontend/display` 和 `frontend/web` 执行 `npm ci`。Web 的 `npm run dev` 只启动 React。浏览器和 CLI 都通过 `frontend/ui-protocol.py` 连接 `src/protocal.py`；HTTP 只有 `POST /api/protocol` 一个业务路由。后端从 `data/source/registry/sources.json` 登记来源、数据集和基准，由 `DataBus` 读取数据并交给固定 `Kernel` 执行。见[接线状态与验证](docs/interaction-entrypoints.md)。

## 命令行入口

先运行下面的命令查看当前 CLI 参数，不把旧配置文件当作后端装配入口：

```text
lianghua-backtest --help
lianghua-backtest run --help
```

业务请求通过 `src/protocal.py` 的现有操作目录验证，例如 `workbench.strategies`、`workbench.preflight` 和 `workbench.backtest`。数据由 `DataBus` 查询，`Kernel` 执行。

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
