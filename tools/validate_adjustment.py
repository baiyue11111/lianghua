"""Validate reconstructed adjustment factors against Sina's official 后复权因子.

Sina exposes the authoritative cumulative backward-adjustment factor series at
    https://finance.sina.com.cn/realstock/company/<sz|sh><code>/hfq.js
as `{"d": ex_date, "f": cumulative_factor}`. Semantics match our own
`adj_factor` exactly: 后复权价 = 原始价 * f, and f steps up on every ex-date.

Difference by construction:
  * Sina's factor includes BOTH share-based events (送股/转增/拆股) and cash
    dividends.
  * Ours is rebuilt from 流通股本 jumps, so it captures share-based events only
    (the source matrices carry no dividend data).

So the test is asymmetric and that is intentional:
  * every LARGE Sina step (>=4%) must be matched by ours  -> catches misses
  * every step of ours must correspond to a Sina step      -> catches false positives
  * Sina's small steps (cash dividends) are expected to be unmatched
"""

from __future__ import annotations

import json
import random
import time
from pathlib import Path

import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
CLEAN = ROOT / "data" / "clean"
OUT = CLEAN / "adjustment_validation.json"

LARGE_STEP = 1.04       # share-based events we are required to capture
MATCH_TOL = 0.05        # relative tolerance when comparing step magnitudes
                         # (ex-date market moves of a few % are normal)
MY_STEP_EPS = 1.0005    # ignore floating-point noise in our own factor series

from collect.quotes import fetch_sina_factors

def steps_from_series(pairs: list[tuple[str, float]]) -> dict[str, float]:
    """Convert a cumulative factor series into {ex_date: step_ratio}."""
    steps = {}
    prev = 1.0
    for d, f in pairs:
        if prev > 0:
            ratio = f / prev
            if ratio > 1.0005:
                steps[d] = ratio
        prev = f
    return steps


def my_steps(calendar: list[str], factors: list[float]) -> dict[str, float]:
    steps = {}
    for i in range(1, len(factors)):
        if factors[i - 1] > 0:
            ratio = factors[i] / factors[i - 1]
            if ratio > MY_STEP_EPS:
                steps[calendar[i]] = ratio
    return steps


def _prev_valid_map(series: list[float | None]) -> list[int | None]:
    """For each bar, the index of the previous bar with a usable close."""
    out: list[int | None] = [None] * len(series)
    prev = None
    for j, v in enumerate(series):
        out[j] = prev
        if v is not None and v > 0:
            prev = j
    return out


def main() -> None:
    table = pq.read_table(CLEAN / "panel.parquet")
    calendar = [d.isoformat() for d in pq.read_table(CLEAN / "calendar.parquet").to_pydict()["trade_date"]]
    symbols = table.column("symbol").to_pylist()
    factors = table.column("adj_factor").to_pylist()
    closes = table.column("close").to_pylist()
    index = {s: i for i, s in enumerate(symbols)}

    lines = (CLEAN / "exrights_events.csv").read_text(encoding="utf-8").splitlines()[1:]
    with_big = []
    seen = set()
    for line in lines:
        parts = line.split(",")
        if float(parts[3]) >= 1.4 and parts[0] not in seen:
            seen.add(parts[0])
            with_big.append(parts[0])

    random.seed(20260823)
    sample = random.sample(with_big, 30) + random.sample(
        [s for s in symbols if factors[index[s]][-1] == 1.0], 20
    )

    details = []
    for sym in sample:
        try:
            pairs = fetch_sina_factors(sym)
        except Exception as exc:  # noqa: BLE001
            details.append({"symbol": sym, "status": f"fetch_failed:{type(exc).__name__}"})
            time.sleep(0.3)
            continue
        if not pairs:
            details.append({"symbol": sym, "status": "no_reference_data"})
            time.sleep(0.3)
            continue

        ref = steps_from_series(pairs)
        mine = my_steps(calendar, factors[index[sym]])

        window = set(calendar)
        ref_in_window = {d: r for d, r in ref.items() if d in window}

        # A large Sina step is NOT automatically a share-based event: a bank
        # paying a 5% cash dividend also produces a >=1.04 step. Classify using
        # the raw close gap on that date -- a share-based event must show the
        # matching proportional price cut, a dividend must not.
        i = index[sym]
        raw = closes[i]
        pos = {d: j for j, d in enumerate(calendar)}
        prev_valid = _prev_valid_map(raw)

        share_based, dividend_like, matched, missed = {}, {}, [], []
        for d, r in sorted(ref_in_window.items()):
            j = pos[d]
            pj = prev_valid[j]
            price_ratio = None
            if pj is not None and raw[j] is not None and raw[pj]:
                price_ratio = raw[j] / raw[pj]
            is_share_based = (
                r >= LARGE_STEP
                and price_ratio is not None
                and price_ratio <= 0.97
                and abs(r * price_ratio - 1.0) <= 0.15
            )
            record = {
                "date": d,
                "reference_step": round(r, 5),
                "raw_price_ratio": round(price_ratio, 5) if price_ratio else None,
                "mine": round(mine[d], 5) if d in mine else None,
            }
            if not is_share_based:
                dividend_like[d] = r
                continue
            share_based[d] = r
            # Exact-date match first.
            if d in mine and abs(mine[d] / r - 1.0) <= MATCH_TOL:
                matched.append(record)
                continue
            # Tolerance match: my detector may fire 1-2 trading days off the
            # official ex-date (first bar reflecting the new share count). Look
            # for a nearby mine step of comparable magnitude.
            offset_match = None
            for off in (-2, -1, 1, 2):
                dj = j + off
                if 0 <= dj < len(calendar):
                    dd = calendar[dj]
                    if dd in mine and abs(mine[dd] / r - 1.0) <= MATCH_TOL:
                        offset_match = dd
                        break
            if offset_match is not None:
                record["matched_on_offset_date"] = offset_match
                matched.append(record)
            else:
                missed.append(record)

        false_positives = [
            {"date": d, "mine": round(r, 5)}
            for d, r in sorted(mine.items())
            if d not in ref_in_window
        ]

        details.append({
            "symbol": sym,
            "status": "ok",
            "reference_share_based_events": len(share_based),
            "reference_dividend_like_steps": len(dividend_like),
            "matched": len(matched),
            "missed": missed,
            "false_positives": false_positives,
            "reference_total_factor": round(pairs[-1][1], 4),
            "my_total_factor": round(factors[i][-1], 4),
        })
        print(
            f"{sym}: share_based={len(share_based)} matched={len(matched)} "
            f"missed={len(missed)} div_like={len(dividend_like)} false_pos={len(false_positives)}",
            flush=True,
        )
        time.sleep(0.3)

    ok = [d for d in details if d["status"] == "ok"]
    verdict = {
        "sampled": len(sample),
        "validated": len(ok),
        "reference_share_based_events_total": sum(d["reference_share_based_events"] for d in ok),
        "matched_total": sum(d["matched"] for d in ok),
        "missed_total": sum(len(d["missed"]) for d in ok),
        "false_positive_total": sum(len(d["false_positives"]) for d in ok),
        "symbols_perfect": sum(1 for d in ok if not d["missed"] and not d["false_positives"]),
        "details": details,
    }
    verdict["recall_pct"] = (
        round(verdict["matched_total"] / verdict["reference_share_based_events_total"] * 100, 2)
        if verdict["reference_share_based_events_total"] else None
    )
    OUT.write_text(json.dumps(verdict, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in verdict.items() if k != "details"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
