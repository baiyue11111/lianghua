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
  "accounting": "research",
  "market": {"scope": "CN_A", "codes": ["600009"]},
  "initial_cash": "1000000",
  "execution": {
    "price": {"model": "ratio", "params": {"rate": "0.001", "tick": "0.01"}},
    "quantity": {"model": "capacity", "params": {}},
    "fees": {"model": "dated", "params": {"rounding": "none"}},
    "tax": {"model": "none", "params": {}},
    "check": {"model": "cn_a_share", "params": {"tick": "0.01", "min_qty": 100, "step": 100}}
  },
  "intervals": [
    {"name": "甲", "start": "2024-01-01", "end": "2024-06-30"},
    {"name": "乙", "start": "2024-07-01", "end": "2024-12-31"},
    {"start": "2025-01-01", "end": "2025-06-30"}
  ]
}
```

`intervals` 必须是非空列表；每段提供 `start`、`end`，`name` 可选。每段独立回测，使用相同策略参数和初始资金，各自持有独立账户与持仓。CLI 不接受顶层日期范围或 search 配置。

先筛策略可用 `accounting=research`，或直接运行 `lianghua --backtest backtest-research.json`。研究模式以供应商复权口径计量投资份额，用原价在下一交易日开盘模拟买卖，并复用数量、流动性、滑点和费用规则。分派已包含在复权收益中，不重复建立分红结算账本，也不要求个人税务资料。结果标注 `accounting=research`、`return_basis=pretax_adjusted_research_after_costs` 和 `position_unit=adjusted_units`；持仓数量是复权研究份额，成交记录中的数量仍按原价股数估计。

研究模式的股数换算保留 12 位小数，再按整股和交易单位下单，避免除法尾差少卖一股或误判超卖。运行存档保留信号、订单、费用、持仓、净值和数据版本，不再复制每天全市场的行情；策略仍读取完整输入。

研究模式不复现现金红利等待到账、零碎股份分配和个人红利税。缺少当日观测的持仓按最后观测价估值，当日不成交；整段行情缺口、回测期外上市和已核实的历史更码分别列入 `data_quality`，保留完整代码清单，不编造价格。先用该模式比较策略，再将候选策略改为 `accounting=account` 对照精算；两种模式的差异需要测量，不预先承诺差异大小。根目录研究配置的末日按现有完整因子快照设为 2026-10-13。

账户模式 `accounting=account` 保留分红与股份记账。`execution.tax.model=none` 不模拟个人红利税；送转在除权后计为应收股份，按新增股份上市日转为可交易持仓，现金按明确支付日入账。`fifo` 另需个人税务和实际到账证据。省略 accounting 时保持账户模式。双均线按每只证券自己的历史窗口决定是否可以计算信号，单只股票历史不足不会阻断其余证券。

采集名称明确区分来源：`--data daily_baostock` 为 BaoStock 不复权日线与扩展字段，`--data daily_sina` 为新浪不复权日线。修复已存数据可用 `lianghua --collect '沪深:[2014,2017]' --data daily_sina --codes sz.000012 --refresh`，仅替换指定证券与区间的行情。`lianghua --collect '沪深:doctor' --codes sz.000012 --refresh` 则刷新证券事实。日线与事实独立收集，回测统一验证实际需要的数据。

BaoStock 日线适配器在同一进程内按请求间隔采集。来源配置的 `params` 可设置 `interval`（秒，默认 1）、`retry_delay`（首次重试等待秒数，默认 30）、`retries`（额外重试次数，默认 2，最多 5）。只有网络错误重试，后续等待时间翻倍；黑名单、权限和参数错误立即返回。该配置控制采集请求的速度，不创建后台定时任务。

回测省略 `market` 时默认请求 `CN_A` 全市场。`scope` 的市场定义来自 registry；`codes`、`index` 和 `filters` 可组合，按 AND 取交集。普通市场按 registry 中唯一的 `code_pattern` 从标准证券全集筛选，收集与回测共用该规则，不再维护市场成员名单。指数筛选只读取 registry 的 `members` 绑定所指向的标准成分记录，按生效日期和公开时间解析调入、调出；注册表和回测配置都不保存成分股名单。财务条件需要带可知时间的事实数据。市场未登记或所需数据缺失时会明确拒绝，不会静默缩成可用子集。实际可用范围由 registry 与数据覆盖决定；此配置约定不表示当前数据已覆盖全 A，也不表示 HK 已可回测。

CLI 返回指标、净值、参数和运行 ID；完整成交与执行轨迹按 ID 保存在 `data/result/<run_id>/`，网页查询仍读取完整结果。错误写入标准错误，并用非零退出码表示失败。策略、数据集和基准由 registry 与现有插件登记，不复制旧的万能配置结构。

## 基本面计算

`manifest.fields` 可把一个请求字段绑定到事实文件：

```json
{
  "files": ["financials.parquet"],
  "fields": {
    "roe": {
      "source": "facts",
      "file": "financials.parquet",
      "code": "code",
      "effective": "report_date",
      "known": "announced_at",
      "column": "roe",
      "unit": "ratio"
    }
  }
}
```

Parquet 文件必须登记在 `manifest.files` 证据列表，单位由采集层标准化。策略在现有 `requirements` 里声明字段，并在现有 `generate` 内调用计算：

```python
from core.calculation import Calculation
from protocol import DataRequest

def requirements(self):
    return DataRequest(("roe",), 1)

def generate(self, frame, account):
    model = Calculation.fundamentals(frame, {"roe": 1.0})
    for symbol in frame.fields:
        score = model.fields[symbol]["score"]
```

计算只对 `frame.at` 的证券横截面做 population z-score。权重由调用方明确给出，可正可负，不会自动归一化。某只证券任一因子为 `None` 时，`score` 为 `None`，证券仍保留；常量因子记为 0。结果只含标准化因子和 `score`，`history` 为空，不回算过去时点。

事实字段的 `known` 必须来自真实、带时区的公告时刻。财报所属季度不能代替公告时刻。盘后公告从下一交易日起可用；已知修订不会改写过去时点。当前生产的两张事实表没有 `known` 字段，因此这个模块能力不代表现有生产数据已能运行基本面回测。该计算也不承诺收益。

## 说明

- [中文策略详解](frontend/web/public/strategies.html)，运行时可访问 `/strategies.html`

中文词汇集中在 `frontend/display/src/research/locale.ts`。只翻译显示内容，API 标识、原始响应和文件哈希保持原样。未知名称保留原文，避免猜测含义。

这是项目首个本地 Git 快照。源代码、配置、测试、锁文件和文档纳入版本管理。原始数据、生成的回测运行、依赖目录和备份留在本机，未纳入 Git。仅检出源码不会自动恢复行情数据和运行存档。

历史策略指标未通过完整经济有效性验收，特别是未复权旧基线不能作为策略效果证据。页面解释当前代码；每个存档运行使用自己的代码版本。

数据只通过 `DataBus` 读写。收集适配器把数据源字段转换为标准字段，回测只读取标准记录。行情位于 `data/source/cn_a`，复权、分红和证券事实位于 `data/source/facts`；原始响应留在 `log` 用于审计。交易日历同时记录交易日和已核实的日期范围。字段缺失会明确报错，不会用旧缓存或默认值补齐。

`lianghua --collect 沪深:doctor` 独立补齐已登记的证券主档、复权和分红事实，默认检查已存日历起点至复权覆盖终点；可用 `--codes sz.000002` 指定证券，或用 `--start`、`--end` 指定范围。它不重新合并日线；完整的行情覆盖仍用 `check` 检查。
