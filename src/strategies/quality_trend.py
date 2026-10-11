"""Quality-gated medium-term trend strategy; no I/O or execution access."""

from __future__ import annotations

from collections.abc import Mapping

from contracts import Candidate, StrategyFrame, TargetPortfolio


class QualityTrendStrategy:
    strategy_id = "quality_trend"

    def __init__(self, params: Mapping[str, object]) -> None:
        self.positions = int(params.get("positions", 3))
        if self.positions not in {3, 5}:
            raise ValueError("positions must be one of 3 or 5")

    def target(self, frame: StrategyFrame) -> TargetPortfolio:
        eligible = [candidate for candidate in frame.candidates if self._eligible(candidate)]
        roe_rank = _descending_rank(eligible, "roe")
        momentum_rank = _descending_rank(eligible, "r120")
        ranked = sorted(
            eligible,
            key=lambda candidate: (0.1 * roe_rank[candidate.symbol] + 0.9 * momentum_rank[candidate.symbol], candidate.symbol),
        )
        selected = ranked[: self.positions]
        if not selected or frame.breadth_exposure <= 0:
            return TargetPortfolio(self.strategy_id, frame.as_of, {}, 0.0, {})
        weight = frame.breadth_exposure / len(selected)
        weights = {candidate.symbol: weight for candidate in selected}
        reasons = {candidate.symbol: "quality_trend_rank" for candidate in selected}
        return TargetPortfolio(self.strategy_id, frame.as_of, weights, frame.breadth_exposure, reasons)

    @staticmethod
    def _eligible(candidate: Candidate) -> bool:
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
        )


def _descending_rank(candidates: list[Candidate], field: str) -> dict[str, float]:
    ordered = sorted(candidates, key=lambda item: (-float(getattr(item, field)), item.symbol))
    denominator = max(len(ordered) - 1, 1)
    return {candidate.symbol: index / denominator for index, candidate in enumerate(ordered)}
