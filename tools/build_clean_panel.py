"""Build one clean, aligned price panel + reconstructed adjustment factors.

Why this exists instead of reusing data/exploratory_full:
  1. The cached panel contains 100 duplicated symbol rows (resumable-ingest bug).
  2. Cached arrays have variable length (trailing empty XLSX cells are dropped),
     so index i does not always mean the same date.
  3. The vendor calendar contains 211 non-trading days (Chinese public holidays).
     They are 100% empty, but they still shift every "N trading day" window.
  4. Prices are UNADJUSTED, which corrupts any 20-day reversal ranking.

Adjustment reconstruction
-------------------------
`流通市值 == 流通股本 * 收盘价` holds exactly, so 流通股本 is a genuine daily
float-share series (unlike 总市值, which is piecewise-constant / stale).

On a bonus-issue / capitalisation ex-date the float share count is multiplied
by k and the reference price is divided by k, so

    share_ratio * price_ratio = k * (1/k) * (1 + r) = 1 + r

with r the day's real market move. Lock-up expiries (解禁) and placements
(增发) also raise the share count but carry NO price cut, so for them
share_ratio * price_ratio ~= share_ratio > 1 and they are rejected.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from data.xlsx_matrix import iter_sheet_rows  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PRICE_DIR = ROOT / "data" / "origin" / "完整全股池2014-2025"
BENCH = ROOT / "data" / "benchmark" / "sse_composite.json"
OUT = ROOT / "data" / "clean"

SHEETS = {
    "open": "开盘价",
    "close": "收盘价",
    "high": "最高价",
    "low": "最低价",
    "amount": "成交额",
    "float_shares": "流通股本",
}

MIN_SHARE_RATIO = 1.04       # >=4% float-share increase => real 送转, not noise
GAP_DROP = 0.84             # price-gap fallback only for single-day drops >=16%.
                             # Main-board A-shares have a +/-10% daily limit, so a
                             # >=16% one-day drop is almost never an ordinary market
                             # move -- it is overwhelmingly a real split. This
                             # excludes ordinary volatility and most limit-down
                             # crashes. Splits >=40% drop (gap<=0.60) are treated as
                             # definite and skip the crash guard entirely.
SPLIT_TOL = 0.04            # price gap within this of a clean split fraction
                             # (allows for the genuine market move on the ex-date)
FS_GAP = 0.93               # float-share jumps only count when paired with a real
                             # >=7% price drop -- rejects 增发/解禁 (no price reset)
CRASH_GUARD_GAP = 0.60      # drops >=40% (gap<=0.60) are definite splits; do not
                             # apply the crash guard to them
SPLIT_RATIOS = (           # common A-share 送/转 ratios (share multiplier)
    1.05, 1.1, 1.15, 1.167, 1.2, 1.25, 1.3, 1.333, 1.35, 1.4, 1.45, 1.5,
    1.55, 1.6, 1.667, 1.7, 1.75, 1.8, 1.9, 2.0, 2.2, 2.5, 3.0, 3.5, 4.0,
)
CLEAN_GAPS = tuple(1.0 / r for r in SPLIT_RATIOS)   # corresponding price-drop fractions


def _f(v: object) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def real_trading_days() -> set[str]:
    rows = json.loads(BENCH.read_text(encoding="utf-8"))
    return {r["trade_date"] for r in rows}


def _nearest_clean_gap(gap: float) -> tuple[float, float]:
    """Return (best_ratio_k, abs_diff) for the clean split fraction closest to gap."""
    best_k, best_d = 1.0, 1e9
    for k, cg in zip(SPLIT_RATIOS, CLEAN_GAPS):
        d = abs(gap - cg)
        if d < best_d:
            best_d, best_k = d, k
    return best_k, best_d


def build_factors(closes, shares, idx_closes):
    """Continuity-preserving cumulative adjustment factor: adjusted = raw * factor.

    Hybrid detection (the source 流通股本 column is INCOMPLETE -- it silently
    fails to update on many real 送转 ex-dates, so a float-share-only detector
    misses ~20-25% of events). For each candidate ex-date bar i we require the
    price to drop >=6% (gap = c[i]/c[i-1] <= GAP_DROP) and then accept it as a
    split when EITHER:

      (a) the float-share column jumps at i+1 (authoritative 送转), or
      (b) the gap is within SPLIT_TOL of a clean split fraction (1/k for a
          common 送/转 ratio) AND the drop is idiosyncratic -- i.e. the stock
          did NOT move with the broad market that day (crash guard via SSE).

    The factor step uses the float-share ratio when (a) holds (matches Sina's
    authoritative 后复权 factor); otherwise it uses 1/gap, which removes the
    price discontinuity exactly. A constant per-regime scale is irrelevant to
    the strategy's ratio/return signals.
    """
    n = len(closes)
    events: list[tuple[int, float, float]] = []
    for i in range(1, n):
        ci, cim = closes[i], closes[i - 1]
        if ci is None or cim is None or ci <= 0 or cim <= 0:
            continue
        gap = ci / cim
        # (a) float-share jump detected at i+1 (D+1 vs the D gap bar)
        fs_ok = False
        k_fs = 1.0
        if i + 1 < n and shares[i] is not None and shares[i + 1] is not None and shares[i] > 0:
            k_fs = shares[i + 1] / shares[i]
            fs_ok = k_fs >= MIN_SHARE_RATIO and gap <= FS_GAP
        # (b) clean split fraction + idiosyncratic (crash-guard). Only fires for
        #     drops that exceed the daily limit (gap <= GAP_DROP), which cannot
        #     be an ordinary market move on main-board names.
        k_near, d = _nearest_clean_gap(gap)
        clean = (gap <= GAP_DROP) and k_near >= MIN_SHARE_RATIO and d <= SPLIT_TOL
        if not (fs_ok or clean):
            continue
        if not fs_ok:
            # crash guard: the gap path is only for idiosyncratic splits. Skip if
            # (i) the broad market was already declining hard that day (a split
            # is stock-specific, not a market event), or (ii) the stock dropped
            # about as much as the market (a crash, not a split). Very large
            # drops (gap <= CRASH_GUARD_GAP, i.e. >=40%) are physically
            # impossible as ordinary crashes, so the guard is waived for them.
            if gap > CRASH_GUARD_GAP and idx_closes is not None and i - 1 >= 0:
                ic, icm = idx_closes[i], idx_closes[i - 1]
                if ic and icm and ic > 0 and icm > 0:
                    mkt_drop = 1.0 - ic / icm
                    stock_drop = 1.0 - gap
                    if mkt_drop > 0.04 or abs(stock_drop - mkt_drop) <= 0.05:
                        continue
        k = k_fs if fs_ok else 1.0 / gap
        events.append((i, k, gap))
    factors = [1.0] * n
    cum = 1.0
    ev_sorted = sorted(events, key=lambda e: e[0])
    ei = 0
    for i in range(n):
        while ei < len(ev_sorted) and ev_sorted[ei][0] <= i:
            cum *= ev_sorted[ei][1]
            ei += 1
        factors[i] = cum
    return factors, events


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    trading = real_trading_days()

    calendar_full: list[str] | None = None
    keep_idx: list[int] | None = None
    calendar: list[str] | None = None

    seen: set[str] = set()
    symbols: list[str] = []
    names: list[str] = []
    cols: dict[str, list[list[float | None]]] = {f: [] for f in ("open", "close", "high", "low", "amount", "adj_factor")}
    audit: list[dict] = []
    dup_rows = 0
    empty_rows = 0

    for part in range(1, 8):
        path = PRICE_DIR / f"raw_prices_matrix_full_part{part}.xlsx"
        streams = {f: iter_sheet_rows(path, s) for f, s in SHEETS.items()}
        headers = {f: next(st) for f, st in streams.items()}
        base = headers["close"]
        for f, h in headers.items():
            if h != base:
                raise ValueError(f"part{part}: sheet {f} calendar differs from 收盘价")
        part_cal = [str(v) for v in base[2:] if v is not None]
        if calendar_full is None:
            calendar_full = part_cal
            keep_idx = [i for i, d in enumerate(part_cal) if d in trading]
            calendar = [part_cal[i] for i in keep_idx]
            # Align the SSE composite close to the full vendor calendar (used by
            # the crash-guard inside build_factors). Holidays -> None.
            bmk = {r["trade_date"]: float(r["close"]) for r in json.loads(BENCH.read_text(encoding="utf-8"))}
            idx_closes_full = [bmk.get(d) for d in calendar_full]
        elif part_cal != calendar_full:
            raise ValueError(f"part{part}: calendar mismatch")
        assert keep_idx is not None and calendar is not None
        width = len(calendar_full)

        for combined in zip(*(streams[f] for f in SHEETS), strict=True):
            rows = dict(zip(SHEETS, combined, strict=True))
            syms = {str(r[0]).zfill(6) for r in combined}
            if len(syms) != 1:
                raise ValueError(f"part{part}: row symbol misalignment {syms}")
            sym = syms.pop()
            name = str(rows["close"][1])

            padded: dict[str, list[float | None]] = {}
            for f, r in rows.items():
                vals = [_f(v) for v in r[2 : width + 2]]
                vals += [None] * (width - len(vals))
                padded[f] = vals

            if all(v is None for v in padded["close"]):
                empty_rows += 1
                continue
            if sym in seen:
                dup_rows += 1
                continue
            seen.add(sym)

            factors, events = build_factors(padded["close"], padded["float_shares"], idx_closes_full)
            for ex_bar, k, g in events:
                ex_date = calendar_full[ex_bar]
                if ex_date not in trading:
                    continue
                audit.append({
                    "symbol": sym, "name": name, "ex_date": ex_date,
                    "share_ratio": round(k, 6), "price_ratio": round(g, 6),
                    "implied_market_move_pct": round((k * g - 1.0) * 100, 3),
                    "close_before": padded["close"][ex_bar - 1] if ex_bar else None,
                    "close_after": padded["close"][ex_bar],
                })

            symbols.append(sym)
            names.append(name)
            for f in ("open", "close", "high", "low", "amount"):
                cols[f].append([padded[f][i] for i in keep_idx])
            cols["adj_factor"].append([factors[i] for i in keep_idx])
        print(f"part{part} done, symbols={len(symbols)} dup={dup_rows} empty={empty_rows}", flush=True)

    assert calendar is not None and calendar_full is not None
    pq.write_table(
        pa.table({"symbol": symbols, "name": names, **cols}),
        OUT / "panel.parquet",
        compression="zstd",
    )
    pq.write_table(
        pa.table({"trade_date": [date.fromisoformat(d) for d in calendar]}),
        OUT / "calendar.parquet",
        compression="zstd",
    )
    audit.sort(key=lambda r: (r["ex_date"], r["symbol"]))
    keys = ["symbol", "name", "ex_date", "share_ratio", "price_ratio", "implied_market_move_pct", "close_before", "close_after"]
    lines = [",".join(keys)] + [
        ",".join("" if r[k] is None else str(r[k]) for k in keys) for r in audit
    ]
    (OUT / "exrights_events.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")

    summary = {
        "symbols": len(symbols),
        "duplicate_rows_dropped": dup_rows,
        "empty_rows_dropped": empty_rows,
        "vendor_calendar_days": len(calendar_full),
        "real_trading_days": len(calendar),
        "non_trading_columns_dropped": len(calendar_full) - len(calendar),
        "first_day": calendar[0],
        "last_day": calendar[-1],
        "exrights_events": len(audit),
        "symbols_with_adjustment": sum(1 for a in cols["adj_factor"] if a[-1] != 1.0),
        "detector": {
            "min_share_ratio": MIN_SHARE_RATIO,
            "gap_drop": GAP_DROP,
            "split_tol": SPLIT_TOL,
            "fs_gap": FS_GAP,
        },
    }
    (OUT / "panel_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
