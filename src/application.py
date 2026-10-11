"""Application workflow shared by command-line and HTTP adapters."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import importlib
import json
from typing import Any, Iterable, Mapping
from uuid import uuid4

from config import RunConfig
from strategy import load_strategy


@dataclass(frozen=True)
class BacktestPlan:
    """The checked application input that an adapter can execute."""

    config: RunConfig
    values: dict[str, Any]
    strategy: Any
    adapter: Any | None
    issues: tuple[dict[str, Any], ...]


def load_adapter(root: Path, settings: Mapping[str, Any]):
    """Build the configured history adapter. No adapter means no execution."""
    name = settings.get("adapter")
    if not name:
        return None
    module, symbol = name.split(":", 1)
    return getattr(importlib.import_module(module), symbol)(root.resolve(), dict(settings))


def prepare_backtest(
    request: Mapping[str, Any],
    root: Path,
    settings: Mapping[str, Any] | None = None,
    *,
    adapter: Any | None = None,
    missing_adapter_issues: Iterable[dict[str, Any]] | None = None,
) -> BacktestPlan:
    """Validate one request and assemble the strategy plus history adapter.

    CLI and HTTP code translate their own input into ``request`` and their own
    output from the returned plan/result. They do not implement this workflow.
    """
    config = RunConfig.from_mapping(request)
    values = config.values
    if adapter is None and settings is not None:
        adapter = load_adapter(root, settings)
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


def execute_backtest(plan: BacktestPlan, run_id: str | None = None) -> dict:
    """Execute a checked plan and leave persistence to the calling adapter."""
    if plan.issues:
        message = "; ".join(issue["message"] for issue in plan.issues)
        raise ValueError("回测输入未验收：" + message)
    if plan.adapter is None:
        raise ValueError("未配置历史数据适配器。")
    return plan.adapter.run(plan.values, plan.strategy, run_id or uuid4().hex)


def run_backtest(config_path: Path, root: Path) -> dict:
    """CLI config-file adapter for the shared backtest application service."""
    raw = json.loads(config_path.read_bytes())
    plan = prepare_backtest(raw["request"], root, raw["backtests"])
    return execute_backtest(plan)
