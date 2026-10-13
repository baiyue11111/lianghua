"""Workbench module port used only through ``src/protocal.py``."""

from __future__ import annotations

from pathlib import Path

from .backtests import Backtests
from .benchmark import Benchmarks
from .research import Research
from protocal import parse_backtest_request
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from protocal import DataPort


def _object(value):
    if not isinstance(value, dict):
        raise TypeError("operation data must be an object")
    return value


class WorkbenchPort:
    """Catalog, research and historical runs; collection has its own module."""

    def __init__(self, root: Path, config: dict, data: DataPort, *, store, research=None):
        self.root = root.resolve()
        self.store = store
        self.benchmarks = Benchmarks(self.store, config.get("benchmarks", []))
        self.research = research if research is not None else Research(data)
        self.backtests = Backtests(self.root, config.get("backtests", {}), data)

    def handle(self, operation: str, data):
        value = _object(data)
        handlers = {
            "workbench.strategies": self._strategies,
            "workbench.preflight": self._preflight,
            "workbench.runs": self._runs,
            "workbench.run": self._run,
            "workbench.benchmarks": self._benchmarks,
            "workbench.benchmark": self._benchmark,
        }
        try:
            handler = handlers[operation]
        except KeyError as error:
            raise ValueError(f"unsupported workbench operation: {operation}") from error
        return handler(value)

    def _strategies(self, _):
        return self.backtests.catalog()

    def _preflight(self, value):
        request = parse_backtest_request(value)
        return self.backtests.preflight(request)

    def _runs(self, _):
        return {"items": self.research.list()}

    def _run(self, value):
        return self.research.detail(value["id"], value.get("offset", 0), value.get("limit", 50))

    def _benchmarks(self, _):
        return self.benchmarks.list()

    def _benchmark(self, value):
        return self.benchmarks.series(value["id"], value["start"], value["end"])



__all__ = ["WorkbenchPort"]
