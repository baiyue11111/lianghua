"""Quality-gated trend-pullback strategy; no I/O or execution access."""

from __future__ import annotations

from collections.abc import Mapping

from contracts import Candidate, StrategyFrame, TargetPortfolio


class TrendPullbackStrategy:
    """Buy qualified pullbacks within an established long-term trend."""

    strategy_id = "trend_pullback"

    def __init__(self, params: Mapping[str, object]) -> None:
        self.positions = int(params.get("positions", 3))
        self.drawdown = float(params.get("drawdown", 0.08))
        self.max_hold_days = int(params.get("max_hold_days", 10))
        if self.positions not in {3, 5}:
            raise ValueError("positions must be one of 3 or 5")
        if self.drawdown not in {0.08, 0.12}:
            raise ValueError("drawdown must be 0.08 or 0.12")
        if self.max_hold_days not in {10, 20}:
            raise ValueError("max_hold_days must be 10 or 20")

    def target(self, frame: StrategyFrame) -> TargetPortfolio:
        held_days = frame.held_days or {}
        eligible = [
            candidate
            for candidate in frame.candidates
            if self._eligible(candidate) and not self._exit_required(candidate, held_days)
        ]
        ranked = sorted(
            eligible,
            key=lambda candidate: (float(candidate.drawdown20), -float(candidate.r120), candidate.symbol),
        )
        selected = ranked[: self.positions]
        if not selected or frame.breadth_exposure <= 0:
            return TargetPortfolio(self.strategy_id, frame.as_of, {}, 0.0, {})
        weight = frame.breadth_exposure / len(selected)
        weights = {candidate.symbol: weight for candidate in selected}
        reasons = {candidate.symbol: "trend_pullback_rank" for candidate in selected}
        return TargetPortfolio(self.strategy_id, frame.as_of, weights, frame.breadth_exposure, reasons)

    def _exit_required(self, candidate: Candidate, held_days: Mapping[str, int]) -> bool:
        if candidate.symbol not in held_days:
            return False
        return bool(
            candidate.ma20 is not None
            and candidate.close is not None
            and candidate.close >= candidate.ma20
            or candidate.close is not None
            and candidate.ma120 is not None
            and candidate.close <= candidate.ma120
            or held_days[candidate.symbol] >= self.max_hold_days
        )

    def _eligible(self, candidate: Candidate) -> bool:
        return bool(
            candidate.net_income_ttm is not None
            and candidate.net_income_ttm > 0
            and candidate.net_assets is not None
            and candidate.net_assets > 0
            and
            candidate.roe is not None
            and candidate.roe >= 10
            and candidate.pe is not None
            and 0 < candidate.pe <= 200
            and candidate.pb is not None
            and 0 < candidate.pb <= 60
            and candidate.amount20 is not None
            and 50_000_000 <= candidate.amount20 <= 500_000_000
            and candidate.close is not None
            and candidate.ma120 is not None
            and candidate.close > candidate.ma120
            and candidate.r120 is not None
            and candidate.r120 > 0
            and candidate.drawdown20 is not None
            and candidate.drawdown20 <= -self.drawdown
        )
