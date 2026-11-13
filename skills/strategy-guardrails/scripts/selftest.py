#!/usr/bin/env python3
"""Self-test the two guardrail scripts with synthetic fixtures (no /tmp dependency).

Run after editing SKILL.md, a script, or the engine contract:
    python skills/strategy-guardrails/scripts/selftest.py

Exit 0 when every check behaves as expected, 1 otherwise.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = sys.executable


def fake_output(ret: float, sharpe: float, cash: float = 0.12, equity: float = 1e7) -> dict:
    return {
        "run_id": "selftest000000000000000000000000",
        "intervals": [{
            "index": 1,
            "run_id": "selftest000000000000000000000000-interval-0001",
            "result": {
                "first_at": "2014-01-02T15:00:00+08:00",
                "last_at": "2026-11-09T15:00:00+08:00",
                "fills": 1234,
                "final_cash": str(cash * equity),
                "final_equity": str(equity),
                "position_unit": "adjusted_units",
                "metrics": {"initial_equity": 1e6, "final_equity": equity,
                            "total_return": ret, "cagr": 0.22,
                            "annualized_volatility": 0.21, "sharpe": sharpe,
                            "max_drawdown": -0.44, "observations": 3123},
            },
        }],
    }


DIRTY = '''from dataclasses import dataclass, field
@dataclass(frozen=True)
class Params:
    max_positions: int = field(default=20)
    dilute_positions: int = field(default=60)
class S:
    def __init__(self):
        self.nav_values = []
        self.nav_dates = []
    def _dilution(self, equity):
        return equity / self.nav_values[0]
    def generate(self, frame, account):
        equity = float(account.equity)
        self.nav_values.append(equity)
        return self._dilution(equity)
'''
CLEAN = '''from dataclasses import dataclass, field
@dataclass(frozen=True)
class Params:
    max_positions: int = field(default=20)
class S:
    def generate(self, frame, account):
        return {"600000": 0.05}
'''


def run(args: list[str]) -> int:
    return subprocess.run([PY] + args, capture_output=True, text=True).returncode


def main() -> int:
    results: list[tuple[str, bool]] = []
    with tempfile.TemporaryDirectory(prefix="guardrail-selftest-") as raw:
        tmp = Path(raw)
        for name, ret, sharpe in (("base10", 9.4029, 1.095), ("base100", 13.1300, 1.116),
                                 ("nodil10", 12.0205, 1.100), ("nodil100", 11.9323, 1.058)):
            (tmp / f"{name}.json").write_text(json.dumps(fake_output(ret, sharpe)), encoding="utf-8")
        (tmp / "dirty.py").write_text(DIRTY, encoding="utf-8")
        (tmp / "clean.py").write_text(CLEAN, encoding="utf-8")
        (tmp / "cfg.json").write_text(json.dumps({
            "initial_cash": "1000000",
            "params": {"principal": 1000000.0, "dilute_positions": 60, "max_positions": 20},
        }), encoding="utf-8")

        scale = str(HERE / "check_scale_invariance.py")
        static = str(HERE / "check_strategy.py")
        checks = [
            ("scale: baseline 372.7pp gap -> FAIL",
             run([scale, "--case", f"10w={tmp/'base10.json'}",
                  "--case", f"100w={tmp/'base100.json'}"]), 1),
            ("scale: feedback off 8.8pp gap -> PASS",
             run([scale, "--case", f"10w={tmp/'nodil10.json'}",
                  "--case", f"100w={tmp/'nodil100.json'}"]), 0),
            ("static: NAV feedback -> FAIL",
             run([static, str(tmp / "dirty.py")]), 1),
            ("static: clean strategy -> PASS",
             run([static, str(tmp / "clean.py")]), 0),
            ("static: active dilution in config -> FAIL",
             run([static, "--config", str(tmp / "cfg.json")]), 1),
        ]
        for label, actual, expected in checks:
            ok = actual == expected
            results.append((label, ok))
            print(f"[{'OK ' if ok else 'BAD'}] {label} (rc={actual}, expected={expected})")

    failed = sum(1 for _, ok in results if not ok)
    print(f"\n{len(results) - failed}/{len(results)} self-tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
