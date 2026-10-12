"""Reconstruct split/bonus-share adjustment factors from 总市值 + 收盘价.

The supplied price matrices are UNADJUSTED (不复权). Cross-sectional momentum /
reversal ranking on unadjusted closes is badly corrupted: a 10送10 bonus issue
shows up as a -50% "20-day return" and would be picked as a top loser.

Total share count is recoverable exactly, because the vendor computes
    总市值_t = 总股本_t * 收盘价_t
so
    总股本_t = 总市值_t / 收盘价_t

On a bonus/split ex-date the share count is multiplied by k and the reference
price is divided by k, therefore

    shares_ratio * price_ratio = k * (1/k) * (1 + r) = 1 + r

where r is that day's genuine market move. That identity is the detector.
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
OUT_DIR = ROOT / "data" / "adjust"

# Detector thresholds. Deliberately conservative: we would rather miss a tiny
# 10送0.5 (a ~5% artefact) than fabricate a factor out of a seasoned equity
# offering (增发), which raises share count WITHOUT any ex-rights price cut.
MIN_SHARE_RATIO = 1.04      # share count must rise >=4%
MAX_PRICE_RATIO = 0.94      # price must actually gap down >=6%
MAX_RESIDUAL = 0.11         # |shares_ratio * price_ratio - 1| <= 11%


def load_part(part: int) -> tuple[list[date], list[dict]]:
    path = PRICE_DIR / f"raw_prices_matrix_full_part{part}.xlsx"
    close_rows = iter_sheet_rows(path, "收盘价")
    cap_rows = iter_sheet_rows(path, "总市值")
    close_header = next(close_rows)
    cap_header = next(cap_rows)
    if close_header != cap_header:
        raise ValueError(f"part{part}: 收盘价 / 总市值 calendars differ")
    calendar = [date.fromisoformat(str(v)) for v in close_header[2:] if v is not None]
    width = len(calendar)
    out = []
    for c_row, m_row in zip(close_rows, cap_rows, strict=True):
        sym_c = str(c_row[0]).zfill(6)
        sym_m = str(m_row[0]).zfill(6)
        if sym_c != sym_m:
            raise ValueError(f"part{part}: row misalignment {sym_c} vs {sym_m}")
        closes = [_f(v) for v in c_row[2 : width + 2]] + [None] * max(0, width - (len(c_row) - 2))
        caps = [_f(v) for v in m_row[2 : width + 2]] + [None] * max(0, width - (len(m_row) - 2))
        out.append({"symbol": sym_c, "name": str(c_row[1]), "close": closes[:width], "cap": caps[:width]})
    return calendar, out


def _f(v: object) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def build_factors(closes: list[float | None], caps: list[float | None]) -> tuple[list[float], list[tuple[int, float, float, float]]]:
    """Return (cumulative backward-adjust factor per bar, detected events)."""
    n = len(closes)
    shares: list[float | None] = [None] * n
    for i in range(n):
        c, m = closes[i], caps[i]
        if c is not None and m is not None and c > 0 and m > 0:
            shares[i] = m / c

    factors = [1.0] * n
    events: list[tuple[int, float, float, float]] = []
    cum = 1.0
    prev_idx: int | None = None
    for i in range(n):
        if shares[i] is None or closes[i] is None or closes[i] <= 0:
            factors[i] = cum
            continue
        if prev_idx is not None:
            k = shares[i] / shares[prev_idx]
            p = closes[i] / closes[prev_idx]
            if k >= MIN_SHARE_RATIO and p <= MAX_PRICE_RATIO and abs(k * p - 1.0) <= MAX_RESIDUAL:
                cum *= k
                events.append((i, k, p, k * p - 1.0))
        factors[i] = cum
        prev_idx = i
    return factors, events


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    calendar_ref: list[date] | None = None
    symbols: list[str] = []
    names: list[str] = []
    factor_arrays: list[list[float]] = []
    audit: list[dict] = []

    for part in range(1, 8):
        calendar, rows = load_part(part)
        if calendar_ref is None:
            calendar_ref = calendar
        elif calendar_ref != calendar:
            raise ValueError(f"part{part}: calendar mismatch")
        for row in rows:
            factors, events = build_factors(row["close"], row["cap"])
            symbols.append(row["symbol"])
            names.append(row["name"])
            factor_arrays.append(factors)
            for idx, k, p, resid in events:
                audit.append({
                    "symbol": row["symbol"],
                    "name": row["name"],
                    "ex_date": calendar[idx].isoformat(),
                    "share_ratio": round(k, 6),
                    "price_ratio": round(p, 6),
                    "implied_market_move_pct": round(resid * 100, 3),
                    "close_before": row["close"][idx - 1] if idx else None,
                    "close_after": row["close"][idx],
                })
        print(f"part{part}: {len(rows)} rows, cumulative symbols={len(symbols)}", flush=True)

    assert calendar_ref is not None
    pq.write_table(
        pa.table({"symbol": symbols, "name": names, "adj_factor": factor_arrays}),
        OUT_DIR / "adjust_factors.parquet",
        compression="zstd",
    )
    pq.write_table(pa.table({"trade_date": calendar_ref}), OUT_DIR / "adjust_calendar.parquet", compression="zstd")

    audit.sort(key=lambda r: (r["ex_date"], r["symbol"]))
    header = list(audit[0].keys()) if audit else []
    lines = [",".join(header)]
    for r in audit:
        lines.append(",".join("" if r[k] is None else str(r[k]) for k in header))
    (OUT_DIR / "exrights_events.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")

    affected = sum(1 for f in factor_arrays if f[-1] != 1.0)
    summary = {
        "symbols": len(symbols),
        "trading_days": len(calendar_ref),
        "symbols_with_adjustment": affected,
        "exrights_events": len(audit),
        "thresholds": {
            "min_share_ratio": MIN_SHARE_RATIO,
            "max_price_ratio": MAX_PRICE_RATIO,
            "max_residual": MAX_RESIDUAL,
        },
    }
    (OUT_DIR / "adjust_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
