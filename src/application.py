"""Application workflow shared by command-line and HTTP adapters."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import importlib
import json
from typing import Any, Iterable, Mapping

from config import RunConfig
from strategy import load_strategy
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from protocal import DataPort
from core.backtest import BacktestRequest


def save_backtest(data: DataPort, request: BacktestRequest, value: Mapping[str, Any]) -> None:
    """Publish the manifest last, after the completed run's records exist."""
    metrics = {name: value[name] for name in (
        "final_cash", "final_equity", "steps", "fills", "pending_orders",
    )}
    manifest = {
        "schema": 1, "run_id": request.run_id, "strategy_id": request.strategy_id,
        "status": "input_checked", "dataset_id": request.dataset_id,
        "parameters": dict(request.parameters),
        "effective_config": {
            "dataset_id": request.dataset_id, "start": request.start.isoformat(),
            "end": request.end.isoformat(), "initial_cash": str(request.initial_cash),
        },
        "limitations": ["运行结果未经历史资格认定。"],
        "note": "运行结果未经历史资格认定。",
    }
    for name, item in (("metrics.json", metrics), ("result.json", value), ("manifest.json", manifest)):
        content = json.dumps(item, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode()
        data.write("runs", request.run_id, name, content)


@dataclass(frozen=True)
class BacktestPlan:
    """The checked application input that an adapter can execute."""

    config: RunConfig
    values: dict[str, Any]
    strategy: Any
    adapter: Any | None
    issues: tuple[dict[str, Any], ...]


def load_adapter(data: DataPort, settings: Mapping[str, Any]):
    """Build the configured history adapter. No adapter means no execution."""
    name = settings.get("adapter")
    if not name:
        return None
    module, symbol = name.split(":", 1)
    return getattr(importlib.import_module(module), symbol)(data, dict(settings))


def prepare_backtest(
    request: Mapping[str, Any],
    root: Path,
    settings: Mapping[str, Any] | None = None,
    *,
    adapter: Any | None = None,
    missing_adapter_issues: Iterable[dict[str, Any]] | None = None,
    data: DataPort | None = None,
) -> BacktestPlan:
    """Validate one request and assemble the strategy plus history adapter.

    CLI and HTTP code translate their own input into ``request`` and their own
    output from the returned plan/result. They do not implement this workflow.
    """
    config = RunConfig.from_mapping(request)
    values = config.values
    if adapter is None and settings is not None:
        if data is None:
            raise ValueError("data port is required")
        adapter = load_adapter(data, settings)
    strategy = load_strategy(values["module"], values["params"])

    if adapter is None:
        issues = list(missing_adapter_issues or [{
            "code": "backtest_adapter_missing",
            "message": "未配置历史数据适配器。",
        }])
    else:
        issues = list(adapter.check(values, strategy.requirements()))
        if not issues:
            values.update(adapter.context(values, strategy.requirements()))

    return BacktestPlan(config, values, strategy, adapter, tuple(issues))
