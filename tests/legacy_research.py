from tests.fee_fixture import fees
"""Frozen old execution trace for migration samples only; no production returns."""
from datetime import date
import json
import numpy as np
from contracts import Side, Order, Fill, TargetPortfolio
from execution import ExecutionEngine, PositionBook, remaining_order
from research import (ExploratoryMarket, BacktestResult, _market_breadth_state,
    _is_week_end, _frame_at, _orders_for_target, _bar, Strategy)

def run_strategy(
    market: ExploratoryMarket,
    strategy: Strategy,
    initial_cash: float = 100_000.0,
    trade_start: date | None = None,
    trade_end: date | None = None,
) -> BacktestResult:
    """Run a strategy under the shared T+1-open execution engine.

    Strategies default to the original weekly signal cadence. A strategy may
    declare ``signal_frequency = "daily"`` when its market gate is evaluated
    every trading day, while still choosing to rebalance only on selected
    dates inside its immutable frame.
    """
    engine = ExecutionEngine(fees())
    book = PositionBook(initial_cash)
    order_sequence = 0
    pending: list[Order] = []
    orders: list[Order] = []
    fills: list[Fill] = []
    targets: list[TargetPortfolio] = []
    equity: list[tuple[date, float]] = []
    holdings: list[dict[str, Any]] = []
    signals: list[dict[str, Any]] = []
    held_since: dict[str, int] = {}
    last_prices: dict[str, float] = {}
    breadth_state = _market_breadth_state(market)
    signal_frequency = getattr(strategy, "signal_frequency", "weekly")
    include_industries = bool(getattr(strategy, "requires_industries", False))
    use_market_gate = bool(getattr(strategy, "use_market_gate", True))
    if signal_frequency not in {"weekly", "daily"}:
        raise ValueError("signal_frequency must be weekly or daily")
    for index, trade_date in enumerate(market.calendar):
        for symbol_index, symbol in enumerate(market.symbols):
            close = market.close[symbol_index, index]
            if np.isfinite(close) and close > 0:
                last_prices[symbol] = float(close)
        if pending:
            carry: list[Order] = []
            for order in pending:
                symbol_index = market.symbols.index(order.symbol)
                bar = _bar(market, symbol_index, index)
                previous_close = float(market.close[symbol_index, index - 1]) if index else None
                fill = engine.execute(order, bar, book, previous_close)
                fills.append(fill)
                outstanding = remaining_order(order, fill)
                if outstanding is not None:
                    carry.append(outstanding)
                if fill.status == "filled" and fill.side is Side.BUY:
                    held_since.setdefault(fill.symbol, index)
                elif book.shares(fill.symbol) == 0:
                    held_since.pop(fill.symbol, None)
            pending = carry
        value = book.cash + sum(book.shares(symbol) * last_prices.get(symbol, 0.0) for symbol in book.holdings)
        equity.append((trade_date, value))
        if hasattr(strategy, "update_equity"):
            strategy.update_equity(value)
        holdings.append(
            {
                "trade_date": trade_date.isoformat(),
                "cash": book.cash,
                "equity": value,
                "shares": json.dumps(book.holdings, ensure_ascii=False, sort_keys=True),
            }
        )
        signal_due = signal_frequency == "daily" or _is_week_end(market.calendar, index)
        if not signal_due or index + 1 >= len(market.calendar) or index < 120:
            continue
        if trade_start is not None and trade_date < trade_start:
            continue
        if trade_end is not None and trade_date > trade_end:
            continue
        build_candidates = not (
            signal_frequency == "daily" and use_market_gate and not bool(breadth_state[3][index])
        )
        frame = _frame_at(
            market,
            index,
            held_since,
            breadth_state,
            include_industries,
            build_candidates,
        )
        target = strategy.target(frame)
        targets.append(target)
        signals.append(
            {
                "trade_date": trade_date.isoformat(),
                "market_breadth_120": frame.market_breadth_120,
                "market_breadth_120_smoothed": frame.market_breadth_120_smoothed,
                "market_on": frame.market_on,
                "is_month_end": frame.is_month_end,
                "candidate_count": len(frame.candidates),
                "target_count": len(target.target_weights),
                "gross_exposure": target.gross_exposure,
                "rebalance": target.rebalance,
            }
        )
        if not target.rebalance:
            continue
        pending = []
        desired = _orders_for_target(market, index, index + 1, target, book, value, strategy.strategy_id, order_sequence)
        order_sequence += len(desired)
        pending.extend(desired)
        orders.extend(desired)
    return BacktestResult(
        strategy.strategy_id,
        tuple(equity),
        tuple(orders),
        tuple(fills),
        tuple(targets),
        tuple(holdings),
        tuple(signals),
    )

