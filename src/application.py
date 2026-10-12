"""Application workflow shared by command-line and HTTP adapters."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
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
    metrics = value["metrics"]
    manifest = {
        "schema": 1, "run_id": request.run_id, "strategy_id": request.strategy_id,
        "status": "succeeded", "dataset_id": request.dataset_id,
        "parameters": dict(request.parameters),
        "effective_config": value["effective_config"],
        "config_hash": value.get("config_hash"),
        "source_hashes": value.get("provenance", {}).get("source_hashes", {}),
        "data_hash": value.get("provenance", {}).get("data_hash"),
        "execution_hash": value.get("provenance", {}).get("execution_hash"),
        "limitations": ["运行结果未经历史资格认定。"],
        "note": "运行结果未经历史资格认定。",
    }
    for name, item in (("metrics.json", metrics), ("result.json", value), ("trace.json", value.get("trace", {})), ("manifest.json", manifest)):
        content = json.dumps(item, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode()
        data.write("runs", request.run_id, name, content)


def save_failed_backtest(data: DataPort, request: BacktestRequest, error: Exception,
                         partial: object | None) -> str:
    from plugins.recorders.jsonl import _json_value
    value = {"run_id": request.run_id, "status": "failed", "code": getattr(error, "code", "run_failed"),
             "detail": str(error), "partial": _json_value(partial) if partial is not None else None}
    name = "failed.json"
    data.write("runs", request.run_id, name, json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode())
    return f"data/runs/{request.run_id}/{name}"


@dataclass(frozen=True)
class BacktestPlan:
    """The checked application input that an adapter can execute."""

    config: RunConfig
    values: dict[str, Any]
    strategy: Any
    adapter: Any | None
    issues: tuple[dict[str, Any], ...]


def load_adapter(data: DataPort, settings: Mapping[str, Any]):
    """Build the one configured history reader used by the DataBus."""
    import importlib
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
    strategy = load_strategy(values["module"], values["params"])

    if adapter is None:
        issues = list(missing_adapter_issues or [{
            "code": "backtest_adapter_missing",
            "message": "未配置历史数据适配器。",
        }])
    else:
        requirements = strategy.requirements()
        if data is None or not hasattr(adapter, "build_query"):
            raise ValueError("a configured DataBus and query adapter are required")
        try:
            dataset = data.query(adapter.build_query(values, requirements))
        except Exception as error:
            issues = [{"code": "historical_input_invalid", "message": f"{type(error).__name__}: {error}"}]
        else:
            issues = list(adapter.check(dataset))
            if not issues:
                values["requirements"] = {
                    **dict(values["requirements"]),
                    "fields": list(dataset.fields),
                }
                values["fields"] = list(dataset.fields)
                values.update(adapter.context(dataset))

    return BacktestPlan(config, values, strategy, adapter, tuple(issues))
