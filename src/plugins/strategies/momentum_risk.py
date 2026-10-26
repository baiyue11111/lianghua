"""Cross-sectional momentum with inverse-volatility risk allocation."""
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from math import isfinite, nextafter, sqrt
from statistics import stdev

from core.calculation import Calculation
from protocol import AccountSnapshot, DataRequest, MarketFrame, TargetSet
from strategy import validate_params

NAME = "横截面动量风险预算"


@dataclass(frozen=True)
class Params:
    positions: int = field(default=12, metadata={"label": "持仓数量", "minimum": 1, "maximum": 1000})
    momentum_window: int = field(default=120, metadata={"label": "动量窗口", "minimum": 2, "maximum": 2000, "unit": "交易日"})
    skip_window: int = field(default=20, metadata={"label": "跳过近期", "minimum": 0, "maximum": 1999, "unit": "交易日"})
    volatility_window: int = field(default=60, metadata={"label": "波动窗口", "minimum": 2, "maximum": 2000, "unit": "交易日"})
    target_volatility: float = field(default=0.20, metadata={"label": "目标年化波动", "exclusive_minimum": 0, "maximum": 1})
    allocation: str = field(default="risk_budget", metadata={"label": "权重方式", "options": ("equal", "inverse_vol", "risk_budget")})
    ranking: str = field(default="momentum", metadata={"label": "候选排序", "options": ("momentum", "risk_adjusted", "reversal")})
    trend_window: int = field(default=0, metadata={"label": "长期趋势窗口", "minimum": 0, "maximum": 2000, "unit": "交易日"})
    breadth_min: float = field(default=0.0, metadata={"label": "最低市场趋势宽度", "minimum": 0, "maximum": 1})
    rebalance_period: str = field(default="monthly", metadata={"label": "调仓周期", "options": ("monthly", "weekly")})
    trim: float = field(default=0.0, metadata={"label": "排序截尾比例", "minimum": 0, "maximum": 1})

    def __post_init__(self):
        validate_params(self)
        if self.skip_window >= self.momentum_window:
            raise ValueError("skip_window must be smaller than momentum_window")
        if self.breadth_min > 0 and self.trend_window == 0:
            raise ValueError("breadth_min requires trend_window")
        if self.trim >= 1:
            raise ValueError("trim must be less than 1")


class Strategy:
    strategy_id = "momentum_risk"

    def __init__(self, params: Params):
        self.params = params

    def requirements(self) -> DataRequest:
        p = self.params
        return DataRequest(("signal_close",), max(p.momentum_window, p.volatility_window, p.trend_window) + 1)

    @staticmethod
    def _prices(frame: MarketFrame, symbols: tuple[str, ...]):
        prices = {}
        for symbol in symbols:
            values = {}
            for row in frame.history[symbol]:
                value = row.fields["signal_close"]
                if value is None:
                    continue
                if type(value) not in (int, float) or not isfinite(value) or value <= 0:
                    raise ValueError(f"signal_close for {symbol} must be finite positive")
                values[row.at] = float(value)
            prices[symbol] = values
        return prices

    @staticmethod
    def _weights(symbols, inverse_vol, gross):
        total = sum(inverse_vol.values())
        weights = {symbol: gross * inverse_vol[symbol] / total for symbol in symbols}
        limit = Decimal(str(gross))
        while sum(Decimal(str(weight)) for weight in weights.values()) > limit:
            symbol = max(weights, key=weights.get)
            weights[symbol] = nextafter(weights[symbol], 0.0)
        return weights

    def generate(self, frame: MarketFrame, account: AccountSnapshot) -> TargetSet | None:
        if type(frame.context.get("trading")) is not bool:
            raise ValueError("trading phase must be explicit")
        if not frame.context["trading"]:
            return None
        if "next_session" not in frame.context:
            raise ValueError("next_session is required to determine rebalance period")
        following = date.fromisoformat(frame.context["next_session"])
        if following <= frame.at.date():
            raise ValueError("next_session must follow decision session")
        p = self.params
        if p.rebalance_period == "monthly":
            current_period = frame.at.year, frame.at.month
            next_period = following.year, following.month
        else:
            current_period = frame.at.isocalendar()[:2]
            next_period = following.isocalendar()[:2]
        if current_period == next_period:
            return None
        symbols = tuple(sorted(frame.history))
        times = sorted({row.at for rows in frame.history.values() for row in rows})
        if p.breadth_min > 0 and len(times) <= p.trend_window:
            raise ValueError("market breadth cannot be computed without trend endpoints")
        if not frame.history and p.breadth_min == 0:
            return TargetSet(self.strategy_id, frame.at, {}, {})
        if not times or times[-1] != frame.at:
            raise ValueError("history must include decision session")
        enough_history = len(times) >= max(p.momentum_window, p.volatility_window, p.trend_window) + 1
        if not enough_history and p.breadth_min == 0:
            return TargetSet(self.strategy_id, frame.at, {}, {})

        prices = self._prices(frame, symbols)
        trend = {}
        if p.trend_window:
            trend_times = times[-p.trend_window:]
            for symbol, series in prices.items():
                window = tuple(series.get(at) for at in trend_times)
                trend[symbol] = (
                    series[frame.at] > Calculation.sma(window, p.trend_window)
                    if len(window) == p.trend_window and all(value is not None for value in window)
                    else None
                )
        if p.breadth_min > 0:
            measured = sum(value is not None for value in trend.values())
            rising = sum(value is True for value in trend.values())
            if measured == 0:
                raise ValueError("market breadth cannot be computed without trend endpoints")
            if rising / measured < p.breadth_min:
                return TargetSet(self.strategy_id, frame.at, {}, {})
        if not enough_history:
            return TargetSet(self.strategy_id, frame.at, {}, {})

        momentum_start = times[-p.momentum_window - 1]
        momentum_end = times[-p.skip_window - 1]
        vol_times = times[-p.volatility_window - 1:]
        candidates = []
        for symbol in symbols:
            series = prices[symbol]
            if p.trend_window and trend.get(symbol) is not True:
                continue
            start, end = series.get(momentum_start), series.get(momentum_end)
            if start is None or end is None:
                continue
            returns = []
            previous = series.get(vol_times[0])
            if previous is None:
                continue
            ready = True
            for at in vol_times[1:]:
                current = series.get(at)
                if current is None:
                    ready = False
                    break
                change = current / previous - 1.0
                if not isfinite(change):
                    raise ValueError("daily returns must be finite")
                returns.append(change)
                previous = current
            if not ready:
                continue
            sigma = stdev(returns)
            score = end / start - 1.0
            if not isfinite(score) or not isfinite(sigma):
                raise ValueError("momentum and volatility must be finite")
            directed_score = -score if p.ranking == "reversal" else score
            if directed_score > 0 and sigma > 0:
                rank = directed_score / sigma if p.ranking == "risk_adjusted" else directed_score
                if not isfinite(rank):
                    raise ValueError("ranking score must be finite")
                candidates.append((rank, symbol, sigma, tuple(returns)))

        trim = int(Decimal(str(p.trim)) * len(candidates))
        selected = Calculation.select(candidates, trim + p.positions, lambda row: row[0], True,
                                      symbol=lambda row: row[1])[trim:]
        by_symbol = {row[1]: row for row in candidates}
        if not selected:
            return TargetSet(self.strategy_id, frame.at, {}, {})
        if p.allocation == "equal":
            weights = Calculation.equal_weights(selected, len(selected))
        else:
            min_volatility = min(by_symbol[symbol][2] for symbol in selected)
            inverse = {symbol: min_volatility / by_symbol[symbol][2] for symbol in selected}
            gross = 1.0
            if p.allocation == "risk_budget":
                total_inverse = sum(inverse.values())
                combined = [
                    sum((inverse[symbol] / total_inverse) * by_symbol[symbol][3][i]
                        for symbol in selected)
                    for i in range(p.volatility_window)
                ]
                forecast = stdev(combined) * sqrt(252)
                if not isfinite(forecast):
                    raise ValueError("portfolio volatility must be finite")
                if forecast > 0:
                    gross = min(1.0, p.target_volatility / forecast)
            weights = self._weights(selected, inverse, gross)
        reason = "negative_skip_momentum_reversal" if p.ranking == "reversal" else "positive_skip_momentum"
        return TargetSet(self.strategy_id, frame.at, weights,
                         {symbol: reason for symbol in selected})
