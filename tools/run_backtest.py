"""Retired legacy reversal runner, kept only to inspect old invalid results.

BaoStock adjustment factors model hypothetical return continuity; their ratio
is not the number of bonus shares an actual investor receives and does not
settle cash dividends or taxes. The historical ``run`` implementation below
made that accounting error and also force-liquidated positions using future
knowledge. Both public entry points now fail before producing new results.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from panel_features import (  # noqa: E402
    own_day_lagged_return,
    own_day_rolling_mean,
    trailing_mean,
    valid_mask,
)

ROOT = Path(__file__).resolve().parents[1]
CLEAN = ROOT / "data" / "clean"
BENCH = ROOT / "data" / "benchmark" / "sse_composite.json"
OUTDIR = ROOT / "runs" / "backtest"

TRADING_DAYS = 244.0  # A-share sessions per year, 2014-2025 average


class UnvalidatedAccountingError(RuntimeError):
    """The legacy factor-as-share ledger cannot support investor NAV claims."""


_RETIRED_REASON = (
    "legacy factor-ratio holdings do not settle actual bonus shares, "
    "cash dividends or taxes; this runner cannot generate valid returns"
)


# --------------------------------------------------------------------------- #
# board-specific daily price limits
# --------------------------------------------------------------------------- #
def board_limit(symbol: str, day: str) -> float:
    if symbol.startswith(("688", "689")):
        return 0.20  # 科创板
    if symbol.startswith(("300", "301")):
        return 0.20 if day >= "2020-08-24" else 0.10  # 创业板改革日
    if symbol.startswith(("43", "83", "87", "88", "920")):
        return 0.30  # 北交所
    return 0.10


# --------------------------------------------------------------------------- #
# data
# --------------------------------------------------------------------------- #
def load_panel() -> dict:
    tbl = pq.read_table(
        CLEAN / "panel.parquet",
        columns=["symbol", "name", "open", "close", "amount", "adj_factor"],
    )
    symbols = tbl.column("symbol").to_pylist()
    names = tbl.column("name").to_pylist()
    to_arr = lambda c: np.asarray(tbl.column(c).to_pylist(), dtype=np.float64)  # noqa: E731
    op, cl, am, aj = (to_arr(c) for c in ("open", "close", "amount", "adj_factor"))
    if aj.shape != cl.shape or op.shape != cl.shape:
        raise ValueError("adjustment and price panel shapes differ")
    priced = valid_mask(cl) | valid_mask(op)
    invalid = priced & ~(np.isfinite(aj) & (aj > 0))
    if invalid.any():
        row, column = np.argwhere(invalid)[0]
        raise ValueError(
            f"missing or invalid adjustment factor: {symbols[row]} column {column}; "
            f"{int(invalid.sum())} priced bars affected")
    cal = [str(d) for d in pq.read_table(CLEAN / "calendar.parquet").column("trade_date").to_pylist()]
    bench = {r["trade_date"]: float(r["close"]) for r in json.loads(BENCH.read_text("utf-8"))}
    return {
        "symbols": symbols,
        "names": names,
        "open": op,
        "close": cl,
        "amount": am,
        "adj": aj,
        "calendar": cal,
        "bench": np.array([bench.get(d, np.nan) for d in cal], dtype=np.float64),
    }


def build_signals(P: dict, args, names: list[str] | None = None) -> dict:
    close, op, am, adj = P["close"], P["open"], P["amount"], P["adj"]
    bar = valid_mask(close)                       # a real close bar exists
    tradable = bar & valid_mask(op)               # can actually transact
    aclose, aopen = close * adj, op * adj

    ma = own_day_rolling_mean(aclose, args.ma_win, mask=bar)
    r20 = own_day_lagged_return(aclose, args.r_win, mask=bar)
    am20 = own_day_rolling_mean(am, args.amount_win, mask=valid_mask(am) & bar)

    liquid = bar & np.isfinite(am20) & (am20 >= args.amount_floor) & np.isfinite(ma)
    above = liquid & (aclose > ma)
    den = liquid.sum(axis=0)
    num = above.sum(axis=0)
    breadth = np.where(den >= args.min_universe, num / np.maximum(den, 1), np.nan)
    breadth5 = trailing_mean(breadth, args.breadth_smooth)
    exclude = None
    if args.exclude_st and names is not None:
        exclude = np.array(
            [("ST" in (nm or "").upper()) or ("退" in (nm or "")) for nm in names], dtype=bool
        )
    return {
        "exclude": exclude,
        "bar": bar, "tradable": tradable, "aclose": aclose, "aopen": aopen,
        "ma": ma, "r20": r20, "am20": am20, "liquid": liquid, "above": above,
        "universe": den, "above_n": num, "breadth": breadth, "breadth5": breadth5,
    }


def month_end_flags(calendar: list[str]) -> np.ndarray:
    flags = np.zeros(len(calendar), dtype=bool)
    for i, d in enumerate(calendar):
        if i + 1 == len(calendar) or calendar[i + 1][:7] != d[:7]:
            flags[i] = True
    return flags


# --------------------------------------------------------------------------- #
# selection (uses only information available at the close of bar t)
# --------------------------------------------------------------------------- #
def select(S: dict, t: int, args) -> tuple[list[int], float, int, list[tuple]]:
    ok = S["above"][:, t] & np.isfinite(S["r20"][:, t])
    if S.get("exclude") is not None:
        ok = ok & ~S["exclude"]
    pool = np.flatnonzero(ok)
    if pool.size == 0:
        return [], float("nan"), 0, []
    a = S["am20"][pool, t]
    cut = float(np.quantile(a, args.amount_quantile))
    cold = pool[a <= cut] if args.amount_quantile < 1.0 else pool
    if args.cold_side == "high":                     # control: buy the CROWDED decile
        cut_hi = float(np.quantile(a, 1.0 - args.amount_quantile))
        cold, cut = pool[a >= cut_hi], cut_hi
    if cold.size == 0:
        return [], cut, int(pool.size), []
    key = S["r20"][cold, t]
    if args.sort == "r20_desc":                      # control: momentum, not reversal
        key = -key
    elif args.sort == "random":                      # control: random pick from the pool
        key = np.random.default_rng(1234 + t).random(cold.size)
    order = cold[np.argsort(key, kind="stable")]
    ranking = [
        (int(j), float(S["am20"][j, t]), float(S["r20"][j, t]),
         float(S["aclose"][j, t]), float(S["ma"][j, t]))
        for j in order[: args.log_ranking]
    ]
    return [int(j) for j in order[: args.n_hold]], cut, int(pool.size), ranking


# --------------------------------------------------------------------------- #
# engine
# --------------------------------------------------------------------------- #
def run(P: dict, S: dict, args) -> dict:
    raise UnvalidatedAccountingError(_RETIRED_REASON)
    cal, symbols, names = P["calendar"], P["symbols"], P["names"]
    n_sym, T = P["close"].shape
    op_raw, cl_raw, adj = P["open"], P["close"], P["adj"]
    aclose, aopen, tradable, bar = S["aclose"], S["aopen"], S["tradable"], S["bar"]
    b5 = S["breadth5"]
    is_me = month_end_flags(cal)

    finite = np.flatnonzero(np.isfinite(b5))
    if finite.size == 0:
        raise RuntimeError("breadth is never computable -- check the panel")
    t0 = int(finite[0])

    last_bar = np.full(n_sym, -1, dtype=np.int64)
    for i in range(n_sym):
        w = np.flatnonzero(bar[i])
        if w.size:
            last_bar[i] = w[-1]

    cash = float(args.capital)
    pos: dict[int, dict] = {}
    state = "FLAT"
    pending: tuple | None = None
    trades: list[dict] = []
    daily: list[dict] = []
    sel_log: list[dict] = []
    pos_log: list[dict] = []
    blocked = {"buy_limit_up": 0, "sell_limit_down": 0, "suspended": 0}
    forced = 0
    last_px = np.full(n_sym, np.nan)

    def fee_buy(gross: float) -> float:
        return max(args.min_commission, gross * args.commission)

    def fee_sell(gross: float) -> tuple[float, float]:
        return max(args.min_commission, gross * args.commission), gross * args.stamp

    def prev_bar(i: int, t: int) -> int:
        w = np.flatnonzero(bar[i][:t])
        return int(w[-1]) if w.size else -1

    def gap_blocked(i: int, t: int, side: str) -> bool:
        """True when the open is at the board limit and the order cannot fill."""
        pb = prev_bar(i, t)
        if pb < 0:
            return False
        lim = board_limit(symbols[i], cal[t]) - 0.005
        g = aopen[i, t] / aclose[i, pb] - 1.0
        return g >= lim if side == "buy" else g <= -lim

    def sell(i: int, t: int, reason: str) -> bool:
        p = pos[i]
        if not tradable[i, t]:
            return False
        if gap_blocked(i, t, "sell"):
            blocked["sell_limit_down"] += 1
            return False
        assert p["exec_idx"] < t, f"T+1 violation on {symbols[i]} at {cal[t]}"
        eff = round(p["shares"] * adj[i, t] / p["adj_entry"])
        px = op_raw[i, t] * (1.0 - args.slippage)
        gross = eff * px
        comm, stamp = fee_sell(gross)
        net = gross - comm - stamp
        nonlocal cash
        cash += net
        trades.append({
            "exec_date": cal[t], "signal_date": cal[t - 1], "symbol": symbols[i],
            "name": names[i], "side": "SELL", "reason": reason, "shares": eff,
            "price_raw": round(px, 4), "gross": round(gross, 2),
            "commission": round(comm, 2), "stamp": round(stamp, 2),
            "net_cash": round(net, 2), "hold_days": t - p["exec_idx"],
            "pnl": round(net - p["cost"], 2),
            "return_pct": round((net / p["cost"] - 1.0) * 100, 4) if p["cost"] else "",
            "entry_date": cal[p["exec_idx"]], "entry_price_raw": round(p["price"], 4),
        })
        del pos[i]
        return True

    def force_liquidate(i: int, t: int) -> None:
        nonlocal cash, forced
        p = pos[i]
        pb = prev_bar(i, t + 1)
        px = float(cl_raw[i, pb]) if pb >= 0 else 0.0
        eff = round(p["shares"] * adj[i, pb] / p["adj_entry"]) if pb >= 0 else 0
        gross = eff * px
        comm, stamp = fee_sell(gross) if gross > 0 else (0.0, 0.0)
        net = max(0.0, gross - comm - stamp)
        cash += net
        forced += 1
        trades.append({
            "exec_date": cal[t], "signal_date": cal[t], "symbol": symbols[i],
            "name": names[i], "side": "SELL", "reason": "delist_force", "shares": eff,
            "price_raw": round(px, 4), "gross": round(gross, 2),
            "commission": round(comm, 2), "stamp": round(stamp, 2),
            "net_cash": round(net, 2), "hold_days": t - p["exec_idx"],
            "pnl": round(net - p["cost"], 2),
            "return_pct": round((net / p["cost"] - 1.0) * 100, 4) if p["cost"] else "",
            "entry_date": cal[p["exec_idx"]], "entry_price_raw": round(p["price"], 4),
        })
        del pos[i]

    def buy(i: int, t: int, budget: float, reason: str) -> bool:
        nonlocal cash
        if not tradable[i, t] or budget <= 0:
            if not tradable[i, t]:
                blocked["suspended"] += 1
            return False
        if gap_blocked(i, t, "buy"):
            blocked["buy_limit_up"] += 1
            return False
        px = op_raw[i, t] * (1.0 + args.slippage)
        if px <= 0:
            return False
        lots = int(min(budget, cash) / (px * args.lot * (1.0 + args.commission)))
        shares = lots * args.lot
        if shares <= 0:
            return False
        gross = shares * px
        comm = fee_buy(gross)
        if gross + comm > cash:
            shares -= args.lot
            if shares <= 0:
                return False
            gross = shares * px
            comm = fee_buy(gross)
        cash -= gross + comm
        pos[i] = {
            "shares": shares, "adj_entry": float(adj[i, t]), "exec_idx": t,
            "price": px, "cost": gross + comm,
        }
        trades.append({
            "exec_date": cal[t], "signal_date": cal[t - 1], "symbol": symbols[i],
            "name": names[i], "side": "BUY", "reason": reason, "shares": shares,
            "price_raw": round(px, 4), "gross": round(gross, 2),
            "commission": round(comm, 2), "stamp": 0.0,
            "net_cash": round(-(gross + comm), 2), "hold_days": "", "pnl": "",
            "return_pct": "", "entry_date": cal[t], "entry_price_raw": round(px, 4),
        })
        return True

    # ---------------- main loop: execute -> mark -> decide ----------------- #
    for t in range(t0, T):
        action_done = ""
        if pending is not None:
            kind, payload = pending
            if kind == "EXIT":
                for i in list(pos):
                    sell(i, t, "breadth_exit")
                pending = ("EXIT", None) if pos else None
                action_done = "EXIT"
            else:
                # `payload` is always the FULL target basket plus a retry
                # counter, which makes this block idempotent: re-running it can
                # only finish unfilled legs, never unwind a correct fill.
                target, tries = payload
                tset = set(target)
                for i in list(pos):
                    if i not in tset:
                        sell(i, t, "rebalance_out" if kind == "REBAL" else "entry_reset")
                # Size new buys off the OPEN of the execution bar. Using the
                # close of t here would be a look-ahead leak.
                held_val = 0.0
                for i, p in pos.items():
                    px = op_raw[i, t] if tradable[i, t] else last_px[i]
                    if not np.isfinite(px):
                        px = last_px[i]
                    if np.isfinite(px):
                        held_val += round(p["shares"] * adj[i, t] / p["adj_entry"]) * px
                per = (cash + held_val) / args.n_hold
                for i in target:
                    if i not in pos and len(pos) < args.n_hold:
                        buy(i, t, per, "rebalance_in" if kind == "REBAL" else "entry")
                unfilled = [i for i in target if i not in pos]
                stale = [i for i in pos if i not in tset]
                retry = bool(unfilled or stale) and args.retry_unfilled and tries + 1 < args.max_retry_days
                pending = (kind, (target, tries + 1)) if retry else None
                action_done = kind if tries == 0 else f"{kind}_RETRY{tries}"

        # delisted holdings -> force out
        for i in list(pos):
            if last_bar[i] <= t and not bar[i, t]:
                force_liquidate(i, t)

        # ---- mark to market at the close of t ----
        mv = 0.0
        for i, p in pos.items():
            if bar[i, t]:
                last_px[i] = cl_raw[i, t]
            px = last_px[i]
            eff = round(p["shares"] * adj[i, t] / p["adj_entry"])
            v = eff * px
            mv += v
            if args.log_positions:
                pos_log.append({
                    "date": cal[t], "symbol": symbols[i], "name": names[i],
                    "shares": eff, "close_raw": round(float(px), 4),
                    "market_value": round(float(v), 2),
                    "unrealised_pct": round((v / p["cost"] - 1) * 100, 3),
                })
        equity = cash + mv

        daily.append({
            "date": cal[t], "cash": round(cash, 2), "market_value": round(mv, 2),
            "equity": round(equity, 2), "n_positions": len(pos),
            "universe_size": int(S["universe"][t]), "above_ma120": int(S["above_n"][t]),
            "breadth_raw": round(float(S["breadth"][t]), 6),
            "breadth_5d": round(float(b5[t]), 6), "state": state,
            "action": action_done, "bench_close": float(P["bench"][t]),
        })

        # ---- decide from the close of t, to be filled at t+1 open ----
        if t + 1 >= T:
            break
        b = b5[t]
        if not np.isfinite(b):
            continue
        want = None
        if args.always_in:
            if state == "FLAT":
                state, want = "IN", "ENTRY"
            elif is_me[t]:
                want = "REBAL"
        elif state == "IN":
            if b < args.exit_breadth:
                state, want = "FLAT", ("EXIT", None)
            elif is_me[t]:
                want = "REBAL"
        elif b >= args.entry_breadth:
            state, want = "IN", "ENTRY"
        if want in ("REBAL", "ENTRY"):
            tgt, cut, pool_n, ranking = select(S, t, args)
            if not tgt:
                continue
            pending = (want, (tgt, 0))
            chosen = set(tgt)
            for rk, (j, a20, r, ac, ma_) in enumerate(ranking, 1):
                sel_log.append({
                    "signal_date": cal[t], "trigger": want, "rank": rk,
                    "symbol": symbols[j], "name": names[j],
                    "amount20_wan": round(a20 / 1e4, 1), "r20_pct": round(r * 100, 3),
                    "adj_close": round(ac, 4), "ma120": round(ma_, 4),
                    "above_ma120_pct": round((ac / ma_ - 1) * 100, 3),
                    "cold_cut_wan": round(cut / 1e4, 1), "pool_size": pool_n,
                    "selected": int(j in chosen), "already_held": int(j in pos),
                    "breadth_5d": round(float(b), 6),
                })
        elif want is not None:
            pending = want

    # ------------------------- self-checks -------------------------------- #
    # Churn guard: the retry path used to be non-idempotent and unwound legs it
    # had just filled. A round trip shorter than 3 sessions is the fingerprint.
    churn = [t for t in trades if t["side"] == "SELL" and t["hold_days"] != "" and int(t["hold_days"]) < 3]
    n_pos_series = [d["n_positions"] for d in daily if d["state"] == "IN"]

    return {
        "daily": daily, "trades": trades, "selection": sel_log, "positions": pos_log,
        "t0": t0, "blocked": blocked, "forced": forced,
        "checks": {
            "same_week_round_trips": len(churn),
            "min_positions_while_in_market": int(min(n_pos_series)) if n_pos_series else None,
            "median_positions_while_in_market": int(np.median(n_pos_series)) if n_pos_series else None,
            "days_in_market_below_full_book": int(sum(1 for v in n_pos_series if v < args.n_hold)),
        },
        "open_at_end": [{"symbol": symbols[i], "shares": p["shares"]} for i, p in pos.items()],
    }


# --------------------------------------------------------------------------- #
# metrics
# --------------------------------------------------------------------------- #
def metrics(dates: list[str], eq: np.ndarray, label: str) -> dict:
    eq = np.asarray(eq, dtype=np.float64)
    ret = eq[1:] / eq[:-1] - 1.0
    yrs = len(eq) / TRADING_DAYS
    total = eq[-1] / eq[0] - 1.0
    cagr = (eq[-1] / eq[0]) ** (1 / yrs) - 1.0 if yrs > 0 else float("nan")
    vol = float(np.std(ret, ddof=1) * np.sqrt(TRADING_DAYS)) if ret.size > 1 else float("nan")
    peak = np.maximum.accumulate(eq)
    dd = eq / peak - 1.0
    mdd = float(dd.min())
    i_dd = int(np.argmin(dd))
    down = ret[ret < 0]
    sortino = float(cagr / (np.std(down, ddof=1) * np.sqrt(TRADING_DAYS))) if down.size > 1 else float("nan")
    yearly: dict[str, float] = {}
    for y in sorted({d[:4] for d in dates}):
        idx = [k for k, d in enumerate(dates) if d[:4] == y]
        a, b = idx[0], idx[-1]
        base = eq[a - 1] if a > 0 else eq[a]
        yearly[y] = round((eq[b] / base - 1.0) * 100, 2)
    return {
        "label": label,
        "start": dates[0], "end": dates[-1], "years": round(yrs, 2),
        "start_value": round(float(eq[0]), 2), "end_value": round(float(eq[-1]), 2),
        "total_return_pct": round(total * 100, 2),
        "cagr_pct": round(cagr * 100, 2),
        "ann_vol_pct": round(vol * 100, 2),
        "sharpe": round(float(cagr / vol), 3) if vol and np.isfinite(vol) else None,
        "sortino": round(sortino, 3) if np.isfinite(sortino) else None,
        "max_drawdown_pct": round(mdd * 100, 2),
        "max_drawdown_date": dates[i_dd],
        "calmar": round(float(cagr / abs(mdd)), 3) if mdd else None,
        "best_day_pct": round(float(ret.max()) * 100, 2) if ret.size else None,
        "worst_day_pct": round(float(ret.min()) * 100, 2) if ret.size else None,
        "positive_days_pct": round(float((ret > 0).mean()) * 100, 2) if ret.size else None,
        "yearly_return_pct": yearly,
    }


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys = list(rows[0].keys())
    out = [",".join(keys)]
    for r in rows:
        out.append(",".join("" if r.get(k) is None else str(r.get(k, "")) for k in keys))
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def parse_args(argv: list[str] | None = None):
    p = argparse.ArgumentParser()
    p.add_argument("--prefix", default="breadth_low_amount_reversal")
    p.add_argument("--capital", type=float, default=1_000_000.0)
    p.add_argument("--amount-floor", type=float, default=50_000_000.0)
    p.add_argument("--ma-win", type=int, default=120)
    p.add_argument("--amount-win", type=int, default=20)
    p.add_argument("--r-win", type=int, default=20)
    p.add_argument("--entry-breadth", type=float, default=0.60)
    p.add_argument("--exit-breadth", type=float, default=0.45)
    p.add_argument("--breadth-smooth", type=int, default=5)
    p.add_argument("--amount-quantile", type=float, default=0.10)
    p.add_argument("--n-hold", type=int, default=8)
    p.add_argument("--commission", type=float, default=0.0003)
    p.add_argument("--min-commission", type=float, default=5.0)
    p.add_argument("--stamp", type=float, default=0.0005)
    p.add_argument("--slippage", type=float, default=0.001)
    p.add_argument("--lot", type=int, default=100)
    p.add_argument("--min-universe", type=int, default=100)
    p.add_argument("--log-ranking", type=int, default=30)
    p.add_argument("--log-positions", action="store_true", default=True)
    p.add_argument("--no-log-positions", dest="log_positions", action="store_false")
    p.add_argument("--retry-unfilled", action="store_true", default=True)
    p.add_argument("--max-retry-days", type=int, default=3)
    p.add_argument("--metrics-only", action="store_true")
    # ---- controls used by the adversarial review (defaults = spec strategy) ----
    p.add_argument("--sort", choices=["r20_asc", "r20_desc", "random"], default="r20_asc",
                   help="r20_asc = the specified reversal leg; the others are controls")
    p.add_argument("--cold-side", choices=["low", "high"], default="low",
                   help="low = the specified 冷门 decile; high buys the crowded decile")
    p.add_argument("--always-in", action="store_true",
                   help="control: ignore the breadth gate and stay invested")
    p.add_argument("--exclude-st", action="store_true",
                   help="drop names whose (latest) name carries ST / 退")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> dict:
    args = parse_args(argv)
    raise UnvalidatedAccountingError(_RETIRED_REASON)
    OUTDIR.mkdir(parents=True, exist_ok=True)
    P = load_panel()
    S = build_signals(P, args)
    R = run(P, S, args)

    daily = R["daily"]
    dates = [d["date"] for d in daily]
    eq = np.array([d["equity"] for d in daily], dtype=np.float64)
    bc = np.array([d["bench_close"] for d in daily], dtype=np.float64)
    # forward-fill the benchmark so a missing vendor print cannot break the ratio
    for k in range(1, len(bc)):
        if not np.isfinite(bc[k]):
            bc[k] = bc[k - 1]
    beq = bc / bc[0] * args.capital

    m_strat = metrics(dates, eq, "strategy")
    m_bench = metrics(dates, beq, "sse_composite")

    sret = eq[1:] / eq[:-1] - 1.0
    bret = beq[1:] / beq[:-1] - 1.0
    ok = np.isfinite(sret) & np.isfinite(bret)
    beta = float(np.cov(sret[ok], bret[ok])[0, 1] / np.var(bret[ok], ddof=1)) if ok.sum() > 2 else float("nan")
    ex = sret[ok] - bret[ok]
    ir = float(ex.mean() / ex.std(ddof=1) * np.sqrt(TRADING_DAYS)) if ok.sum() > 2 and ex.std() else float("nan")

    trades = R["trades"]
    sells = [t for t in trades if t["side"] == "SELL" and t["pnl"] != ""]
    pnls = np.array([float(t["pnl"]) for t in sells], dtype=np.float64)
    rets = np.array([float(t["return_pct"]) for t in sells if t["return_pct"] != ""], dtype=np.float64)
    holds = np.array([float(t["hold_days"]) for t in sells], dtype=np.float64)
    fees = sum(float(t["commission"]) + float(t["stamp"]) for t in trades)
    exposure = float(np.mean([d["n_positions"] > 0 for d in daily]))
    in_days = sum(1 for d in daily if d["state"] == "IN")

    summary = {
        "generated_from": "tools/run_backtest.py",
        "parameters": vars(args),
        "data": {
            "panel_symbols": len(P["symbols"]),
            "calendar_days": len(P["calendar"]),
            "backtest_first_day": dates[0],
            "backtest_last_day": dates[-1],
            "warmup_days_skipped": R["t0"],
            "median_liquid_universe": int(np.median([d["universe_size"] for d in daily])),
        },
        "breadth": {
            "mean_5d": round(float(np.nanmean([d["breadth_5d"] for d in daily])), 4),
            "min_5d": round(float(np.nanmin([d["breadth_5d"] for d in daily])), 4),
            "max_5d": round(float(np.nanmax([d["breadth_5d"] for d in daily])), 4),
            "days_state_in": in_days,
            "days_state_flat": len(daily) - in_days,
            "pct_time_in_market": round(in_days / len(daily) * 100, 2),
        },
        "strategy": m_strat,
        "benchmark": m_bench,
        "relative": {
            "excess_cagr_pct": round(m_strat["cagr_pct"] - m_bench["cagr_pct"], 2),
            "beta_vs_sse": round(beta, 3) if np.isfinite(beta) else None,
            "information_ratio": round(ir, 3) if np.isfinite(ir) else None,
            "drawdown_improvement_pp": round(
                m_strat["max_drawdown_pct"] - m_bench["max_drawdown_pct"], 2),
        },
        "trading": {
            "fills": len(trades),
            "buys": sum(1 for t in trades if t["side"] == "BUY"),
            "sells": len(trades) - sum(1 for t in trades if t["side"] == "BUY"),
            "closed_round_trips": int(pnls.size),
            "win_rate_pct": round(float((pnls > 0).mean()) * 100, 2) if pnls.size else None,
            "avg_return_pct": round(float(rets.mean()), 3) if rets.size else None,
            "median_return_pct": round(float(np.median(rets)), 3) if rets.size else None,
            "avg_win_pct": round(float(rets[rets > 0].mean()), 3) if (rets > 0).any() else None,
            "avg_loss_pct": round(float(rets[rets <= 0].mean()), 3) if (rets <= 0).any() else None,
            "profit_factor": round(float(pnls[pnls > 0].sum() / abs(pnls[pnls <= 0].sum())), 3)
                             if (pnls <= 0).any() and (pnls > 0).any() else None,
            "avg_hold_trading_days": round(float(holds.mean()), 1) if holds.size else None,
            "total_fees_rmb": round(fees, 2),
            "fees_pct_of_initial_capital": round(fees / args.capital * 100, 2),
            "rebalances": len({s["signal_date"] for s in R["selection"]}),
            "pct_days_holding": round(exposure * 100, 2),
            "blocked_orders": R["blocked"],
            "forced_liquidations": R["forced"],
            "open_positions_at_end": R["open_at_end"],
        },
        "integrity": {
            "t_plus_1_enforced": True,
            "signal_close_t_fill_open_t_plus_1": True,
            "rolling_windows": "own trading days per symbol (see panel_features)",
            "prices_for_signals": "split-adjusted",
            "prices_for_cash": "raw, with adj-factor share multiplier on open positions",
            "negative_cash_days": int(sum(1 for d in daily if d["cash"] < -1e-6)),
            "self_checks": R["checks"],
        },
    }

    if not args.metrics_only:
        pre = OUTDIR / args.prefix
        rows = []
        for k, d in enumerate(daily):
            r = dict(d)
            r["bench_equity"] = round(float(beq[k]), 2)
            r["strategy_index"] = round(float(eq[k] / eq[0] * 100), 4)
            r["bench_index"] = round(float(beq[k] / beq[0] * 100), 4)
            rows.append(r)
        write_csv(Path(f"{pre}_equity.csv"), rows)
        write_csv(Path(f"{pre}_trades.csv"), trades)
        write_csv(Path(f"{pre}_selection.csv"), R["selection"])
        write_csv(Path(f"{pre}_positions.csv"), R["positions"])
        Path(f"{pre}_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


if __name__ == "__main__":
    main()
