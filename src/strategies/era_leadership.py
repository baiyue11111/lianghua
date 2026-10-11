"""Industry-era leadership strategy; no I/O or execution access."""

from __future__ import annotations

from collections.abc import Mapping

from contracts import Candidate, StrategyFrame, TargetPortfolio


class EraLeadershipStrategy:
    """Own leaders in the active industry regime and de-risk in a closed market."""

    strategy_id = "era_leadership"
    requires_industries = True

    def __init__(self, params: Mapping[str, object]) -> None:
        self.positions = int(params.get("positions", 3))
        if self.positions != 3:
            raise ValueError("positions must be 3")

    def target(self, frame: StrategyFrame) -> TargetPortfolio:
        eligible = [candidate for candidate in frame.candidates if self._eligible(candidate, frame)]
        ranked = sorted(eligible, key=lambda candidate: (-float(candidate.r120), -float(candidate.roe), candidate.symbol))
        closed_market = frame.breadth_exposure <= 0
        selected = ranked[: 1 if closed_market else self.positions]
        exposure = 1 / 3 if closed_market else frame.breadth_exposure
        if not selected or exposure <= 0:
            return TargetPortfolio(self.strategy_id, frame.as_of, {}, 0.0, {})
        weight = exposure / len(selected)
        weights = {candidate.symbol: weight for candidate in selected}
        reasons = {candidate.symbol: "era_leadership_rank" for candidate in selected}
        return TargetPortfolio(self.strategy_id, frame.as_of, weights, exposure, reasons)

    @staticmethod
    def _eligible(candidate: Candidate, frame: StrategyFrame) -> bool:
        return bool(
            candidate.industry is not None
            and candidate.industry in frame.active_industries
            and candidate.net_income_ttm is not None
            and candidate.net_income_ttm > 0
            and candidate.net_assets is not None
            and candidate.net_assets > 0
            and candidate.roe is not None
            and candidate.roe >= 10
            and candidate.pe is not None
            and 0 < candidate.pe <= 200
            and candidate.pb is not None
            and 0 < candidate.pb <= 60
            and candidate.amount20 is not None
            and candidate.amount20 >= 100_000_000
            and candidate.amount120 is not None
            and candidate.amount120 > 0
            and candidate.amount20 / candidate.amount120 >= 1
            and candidate.close is not None
            and candidate.ma120 is not None
            and candidate.close > candidate.ma120
            and candidate.r120 is not None
            and candidate.r120 > 0
        )
