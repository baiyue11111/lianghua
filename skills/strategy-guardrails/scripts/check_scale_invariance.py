#!/usr/bin/env python3
"""G2 acceptance check: capital-scale invariance from official --backtest output.

Usage:
    python check_scale_invariance.py \
        --case 10w=/tmp/bt_10w.json \
        --case 100w=/tmp/bt_100w.json \
        --case 1000w=/tmp/bt_1000w.json

`--case` values are the JSON files emitted by `.venv/bin/lianghua --backtest`.
Only the 10w and 100w cases are required; 1000w is informational because
capacity effects are expected there (G4) and are NOT a failure criterion.

Exit code 0 = PASS, 1 = FAIL (thresholds exceeded or data unusable), 2 = bad args.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def load_case(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    intervals = data.get("intervals") or []
    if not intervals:
        raise ValueError("no intervals in output")
    result = intervals[0].get("result") or {}
    metrics = result.get("metrics") or {}
    for key in ("total_return", "sharpe"):
        if key not in metrics:
            raise ValueError(f"metrics.{key} missing")
    equity = float(result.get("final_equity") or metrics.get("final_equity") or 0.0)
    cash = float(result.get("final_cash") or 0.0)
    return {
        "path": str(path),
        "run_id": data.get("run_id", "?"),
        "ret": float(metrics["total_return"]),
        "cagr": float(metrics.get("cagr") or 0.0),
        "sharpe": float(metrics["sharpe"]),
        "mdd": float(metrics.get("max_drawdown") or 0.0),
        "fills": result.get("fills"),
        "cash_share": cash / equity if equity else 0.0,
        "first": str(result.get("first_at", ""))[:10],
        "last": str(result.get("last_at", ""))[:10],
        "position_unit": result.get("position_unit", "?"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--case", action="append", required=True, metavar="TIER=PATH",
                    help="capital tier and official backtest output JSON, e.g. 10w=bt_10w.json")
    ap.add_argument("--return-threshold", type=float, default=0.50,
                    help="max |return_10w - return_100w| in return units (0.50 = 50pp)")
    ap.add_argument("--sharpe-threshold", type=float, default=0.05,
                    help="max |sharpe_10w - sharpe_100w|")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON only")
    args = ap.parse_args()

    cases: dict[str, dict] = {}
    for raw in args.case:
        if "=" not in raw:
            print(f"bad --case {raw!r}, expected TIER=PATH")
            return 2
        tier, text = raw.split("=", 1)
        path = Path(text)
        if not path.exists():
            print(f"missing output file: {path}")
            return 2
        try:
            cases[tier.strip()] = load_case(path)
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            print(f"unusable output {path}: {exc}")
            return 1

    if "10w" not in cases or "100w" not in cases:
        print("both --case 10w=... and --case 100w=... are required")
        return 2

    small, mid = cases["10w"], cases["100w"]
    gap_ret = abs(small["ret"] - mid["ret"])
    gap_sharpe = abs(small["sharpe"] - mid["sharpe"])
    cap = cases.get("1000w")
    cap_gap = abs(cap["ret"] - mid["ret"]) if cap else None
    fail = gap_ret > args.return_threshold or gap_sharpe > args.sharpe_threshold

    payload = {
        "verdict": "FAIL" if fail else "PASS",
        "gap_return_pp": round(gap_ret * 100, 2),
        "gap_sharpe": round(gap_sharpe, 4),
        "thresholds": {"return_pp": args.return_threshold * 100,
                       "sharpe": args.sharpe_threshold},
        "capacity_gap_1000w_pp": round(cap_gap * 100, 2) if cap_gap is not None else None,
        "cases": cases,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1 if fail else 0

    print("资金规模不变性验收（G2） / capital-scale invariance")
    print(f"阈值: 10w↔100w 收益差 ≤ {args.return_threshold*100:.0f}pp, "
          f"夏普差 ≤ {args.sharpe_threshold:.2f}\n")
    header = f"{'档位':<8}{'总收益':>10}{'CAGR':>9}{'夏普':>8}{'MDD':>9}{'现金占比':>10}{'成交笔数':>10}  {'区间':<24}"
    print(header)
    print("-" * len(header))
    for tier in ("10w", "50w", "100w", "1000w"):
        c = cases.get(tier)
        if not c:
            continue
        print(f"{tier:<8}{c['ret']*100:>9.2f}%{c['cagr']*100:>8.2f}%{c['sharpe']:>8.3f}"
              f"{c['mdd']*100:>8.2f}%{c['cash_share']*100:>9.2f}%"
              f"{str(c['fills'] or '-'):>10}  {c['first']}~{c['last']}")
    print(f"\n10w ↔ 100w: 收益差 {gap_ret*100:.2f}pp | 夏普差 {gap_sharpe:.4f}")
    if cap:
        print(f"100w ↔ 1000w: 收益差 {cap_gap*100:.2f}pp（容量效应，按 G4 单独判读，不计入 FAIL）")
        if cap["cash_share"] > 0.30:
            print(f"  ⚠ 1000w 现金占比 {cap['cash_share']*100:.2f}% > 30%，"
                  "结果由容量主导；adv_fraction 为整单跳过的保守上界")

    if fail:
        print("\nVERDICT: FAIL — 不得进入策略排名 / 持仓结论")
        print("决策树: ① grep G1 净值反馈 → ② 配置层关闭 dilute_positions 重跑 → "
              "③ 差距仍存再查 run_events.allocations 跳过原因")
        return 1
    print("\nVERDICT: PASS — 小/中资金档一致；仍须人工复核首尾成交与事件日志")
    return 0


if __name__ == "__main__":
    sys.exit(main())
