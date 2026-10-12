"""Frozen original migration baseline; tests only, never a production plugin."""

from __future__ import annotations

from collections.abc import Mapping
from math import ceil, isfinite

from contracts import Candidate, StrategyFrame, TargetPortfolio


class BreadthLowAmountReversalStrategy:
    """Buy eight cold, recently weak stocks while the daily market gate is on."""

    strategy_id = "breadth_low_amount_reversal"
    signal_frequency = "daily"
    requires_industries = False

    def __init__(self, params: Mapping[str, object] | None = None) -> None:
        params = params or {}
        self.positions = int(params.get("positions", 8))
        self.amount_quantile = float(params.get("amount_quantile", 0.10))
        self.amount_floor = float(params.get("amount_floor", 50_000_000))
        self.entry_breadth = float(params.get("entry_breadth", 0.60))
        self.exit_breadth = float(params.get("exit_breadth", 0.45))
        self.entry_cadence = str(params.get("entry_cadence", "month_end"))
        if self.entry_cadence not in {"month_end", "on_qualification"}:
            raise ValueError("entry_cadence must be month_end or on_qualification")
        if self.positions != 8:
            raise ValueError("positions must be 8 for the clarified strategy")
        if self.amount_quantile != 0.10:
            raise ValueError("amount_quantile must be 0.10 for the clarified strategy")
        if self.amount_floor != 50_000_000:
            raise ValueError("amount_floor must be 50000000 for the clarified strategy")
        if self.entry_breadth != 0.60 or self.exit_breadth != 0.45:
            raise ValueError("breadth thresholds must be 0.60 and 0.45")

    def target(self, frame: StrategyFrame) -> TargetPortfolio:
        if not frame.market_on:
            return TargetPortfolio(
                self.strategy_id,
                frame.as_of,
                {},
                0.0,
                {"__market__": "market_breadth_below_45pct"},
            )

        held = tuple(sorted((frame.held_days or {}).keys()))
        entry_due = not held and self.entry_cadence == "on_qualification"
        # Hysteresis lets existing holdings survive between 45% and 60%.
        # It does not permit opening a flat account below the entry threshold.
        if not held:
            breadth = frame.market_breadth_120_smoothed
            if breadth is None or not isfinite(breadth) or not 0 <= breadth <= 1:
                raise ValueError("flat entry requires observed five-day breadth")
            if breadth < self.entry_breadth:
                # A flat account may still have unfilled buys. Replace their
                # old target when today's close no longer qualifies for entry.
                return TargetPortfolio(self.strategy_id, frame.as_of, {}, 0,
                                       {"__market__": "wait_for_60pct_entry"})

        if not frame.is_month_end and not entry_due:
            return self._target_for_symbols(frame, held, "hold_until_month_end", rebalance=False)

        eligible = [candidate for candidate in frame.candidates if self._eligible(candidate)]
        low_amount_count = max(1, ceil(len(eligible) * self.amount_quantile)) if eligible else 0
        low_amount = sorted(eligible, key=lambda candidate: (float(candidate.amount20), candidate.symbol))[:low_amount_count]
        selected = sorted(
            low_amount,
            key=lambda candidate: (float(candidate.r20), candidate.symbol),
        )[: self.positions]
        return self._target_for_symbols(
            frame,
            [candidate.symbol for candidate in selected],
            "qualified_low_amount_20d_reversal" if entry_due and not frame.is_month_end
            else "monthly_low_amount_20d_reversal",
        )

    def _target_for_symbols(
        self,
        frame: StrategyFrame,
        symbols: tuple[str, ...] | list[str],
        reason: str,
        *, rebalance: bool = True,
    ) -> TargetPortfolio:
        selected = tuple(symbols)
        if not selected:
            return TargetPortfolio(self.strategy_id, frame.as_of, {}, 0.0, {}, rebalance)
        weights = {symbol: 1.0 / self.positions for symbol in selected}
        reasons = {symbol: reason for symbol in selected}
        return TargetPortfolio(self.strategy_id, frame.as_of, weights, sum(weights.values()), reasons, rebalance)

    def _eligible(self, candidate: Candidate) -> bool:
        return bool(
            candidate.amount20 is not None
            and candidate.amount20 >= self.amount_floor
            and candidate.close is not None
            and candidate.ma120 is not None
            and candidate.close > candidate.ma120
            and candidate.r20 is not None
        )
