"""Fixed strategy and settlement cases through the current archive."""

import asyncio
from contextlib import closing
from dataclasses import replace
from decimal import Decimal
from importlib import import_module
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import pyarrow as pa

from backend import Research, build_backend
from market import build_data
from protocol import TableWrite
from tests.fee_fixture import execution
from tests.history_fixture import make_snapshot


CASES = Path(__file__).parent / "fixtures" / "backtest_cases"
START = "2020-01-31"
END = "2020-02-04"
ui = import_module("frontend.ui-protocol")


def number(value):
    text = format(Decimal(str(value)), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def snapshot(result):
    points = [(point, target) for point, target in zip(
        result["trace"]["equity"], result["trace"]["targets"], strict=True
    ) if START <= point["day"] <= END]
    amounts = {"shares", "price", "gross_amount", "commission", "stamp_duty", "transfer_fee"}
    return {
        "final_cash": number(result["final_cash"]),
        "final_equity": number(result["final_equity"]),
        "positions": {key: number(value) for key, value in result["positions"].items()},
        "pending_orders": result["pending_orders"],
        "metrics": result["metrics"],
        "equity": [{key: value if key == "day" else number(value)
                    for key, value in point.items()} for point, _ in points],
        "targets": [{"day": point["day"], "weights": None if target is None else target["weights"],
                     "reasons": None if target is None else target["reasons"]}
                    for point, target in points],
        "trades": [{key: number(value) if key in amounts else value
                    for key, value in trade.items() if key != "order_id"}
                   for trade in result["trades"]],
    }


class FixedBacktestCases(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        make_snapshot(self.root)

    def run_case(self, name, strategy, params, market, rate):
        expected = json.loads((CASES / f"{name}.json").read_text())
        rules = execution()
        rules["price"]["params"]["rate"] = rate
        config = {
            "run_id": name,
            "module": f"plugins.strategies.{strategy}",
            "params": params,
            "dataset_id": "fixture",
            "market": market,
            "initial_cash": "100000",
            "accounting": "account",
            "limits": {"stock_weight": "1", "adv_fraction": "0.01"},
            "execution": rules,
            "intervals": [{"name": name, "start": START, "end": END}],
        }
        path = self.root / "config.json"
        path.write_text(json.dumps(config))
        request = ui.UiRequest(name, "cli.backtest", "urn:lianghua:ui:input:cli.backtest:v1",
                               {"config": str(path)})
        with closing(build_backend(self.root)) as backend:
            [response] = asyncio.run(backend.send(request))
        self.assertEqual(response.kind, "resource", response.data)
        [interval] = response.data["intervals"]
        self.assertEqual(interval["run_id"], f"{name}-interval-0001")
        with closing(build_data(self.root)) as data:
            result = Research(data).result(interval["run_id"])
        self.assertEqual(result["parameters"], params)
        self.assertEqual([snapshot(result)], expected)

    def test_breadth_keeps_four_slots_with_two_candidates(self):
        self.run_case("breadth_slots", "breadth_low_amount_reversal", {
            "positions": 4, "amount_quantile": 0.1, "amount_floor": 50000000.0,
            "entry_breadth": 0.6, "exit_breadth": 0.45, "trend_window": 120,
            "return_window": 20, "amount_window": 20, "breadth_window": 5,
            "entry_cadence": "month_end", "sort_by": "return", "sort_desc": False,
        }, {"scope": "CN_A"}, "0.001")

    def moving_average(self, name, exposure):
        self.run_case(name, "moving_average_cross", {
            "fast_window": 2, "slow_window": 3, "rebalance": "daily", "exposure": exposure,
        }, {"scope": "CN_A", "codes": ["600003"]}, "0")

    def test_moving_average_holds_existing_shares(self):
        self.moving_average("moving_average_hold", 1.0)

    def test_moving_average_buys_half_exposure(self):
        self.moving_average("moving_average_half", 0.5)

    def test_moving_average_exits_at_next_open(self):
        with closing(build_data(self.root)) as data:
            manifest = data.manifest("fixture")
            reference = manifest["inputs"]["bars"]
            table = data.table(reference)
            rows = table.to_pylist()
            [row] = [row for row in rows if row["code"] == "sh.600003"
                     and str(row["date"]) == "2020-02-03"]
            row.update(low=8.0, close=8.0)
            source = pa.Table.from_pylist(rows, schema=table.schema)
            definition = replace(data.definition(reference.removeprefix("lake:")), name="exit_bars")
            data.define(definition)
            data.write(TableWrite(definition.name, source))
            manifest["inputs"]["bars"] = f"lake:{definition.name}"
            data.save_manifest("fixture", manifest)
        self.moving_average("moving_average_exit", 1.0)
