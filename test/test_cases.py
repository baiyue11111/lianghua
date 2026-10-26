"""Run fixed configurations against fixed inputs and reviewed answers."""

import csv
from decimal import Decimal
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

import pyarrow as pa
import pyarrow.parquet as pq


ROOT = Path(__file__).resolve().parent
NUMBERS = {"open", "high", "low", "close", "volume", "amount", "tradestatus"}


def number(value):
    text = format(Decimal(str(value)), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def prepare(root, changes):
    files = json.loads((ROOT / "data/files.json").read_text())
    for name, value in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    with (ROOT / "data/bars.csv").open(newline="") as stream:
        rows = [{key: float(value) if key in NUMBERS else value
                 for key, value in row.items()} for row in csv.DictReader(stream)]
    for change in changes:
        matched = [row for row in rows if row["code"] == change["code"]
                   and row["date"] == change["date"]]
        if len(matched) != 1:
            raise AssertionError(f"行情修改必须命中一条记录：{change}")
        matched[0].update(change["values"])
    folder = root / "data/source/fixture"
    (folder / "daily").mkdir(exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows), folder / "daily/bars.parquet")
    path = folder / "manifest.json"
    manifest = json.loads(path.read_text())
    for ref in manifest["files"]:
        ref["sha256"] = sha256((folder / ref["file"]).read_bytes()).hexdigest()
    path.write_text(json.dumps(manifest), encoding="utf-8")


def snapshot(result, start, end):
    points = result["trace"]["equity"]
    selected = [i for i, point in enumerate(points) if start <= point["day"] <= end]
    return {
        "final_cash": number(result["final_cash"]),
        "final_equity": number(result["final_equity"]),
        "positions": {key: number(value) for key, value in result["positions"].items()},
        "pending_orders": result["pending_orders"],
        "metrics": result["metrics"],
        "equity": [{key: value if key == "day" else number(value)
                    for key, value in points[i].items()} for i in selected],
        "targets": [{"day": points[i]["day"], "weights": None if target is None else target["weights"],
                     "reasons": None if target is None else target["reasons"]}
                    for i in selected for target in [result["trace"]["targets"][i]]],
        "trades": [{key: number(value) if key in {
            "shares", "price", "gross_amount", "commission", "stamp_duty", "transfer_fee"
        } else value for key, value in trade.items() if key != "order_id"}
                   for trade in result["trades"]],
    }


class Case(unittest.TestCase):
    maxDiff = None

    def __init__(self, folder):
        super().__init__()
        self.folder = Path(folder)

    def shortDescription(self):
        return self.folder.name

    def runTest(self):
        config = json.loads((self.folder / "config.json").read_text())
        changes = json.loads((self.folder / "input.json").read_text())["changes"]
        expected = json.loads((self.folder / "expected.json").read_text())
        project = ROOT.parent
        with TemporaryDirectory() as directory:
            root = Path(directory)
            prepare(root, changes)
            path = root / "config.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            run = subprocess.run(
                [sys.executable, "-m", "frontend.cli", "--backtest", str(path)],
                cwd=root, env={**os.environ, "PYTHONPATH": os.pathsep.join((str(project / "src"), str(project)))},
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            output = json.loads(run.stdout)
            self.assertEqual(len(output["intervals"]), len(config["intervals"]))
            actual = []
            for interval, part in zip(config["intervals"], output["intervals"], strict=True):
                result = json.loads((root / "data/result" / part["run_id"] / "result.json").read_text())
                self.assertEqual(result["parameters"], config["params"])
                actual.append(snapshot(result, interval["start"], interval["end"]))
            self.assertEqual(actual, expected)


def load_tests(loader, tests, pattern):
    folders = sorted(path.parent for path in (ROOT / "cases").glob("*/config.json"))
    if not folders:
        raise AssertionError("缺少固定策略用例")
    return unittest.TestSuite(Case(folder) for folder in folders)
