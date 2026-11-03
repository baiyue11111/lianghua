"""Monthly quality, growth, and valuation ranking from point-in-time facts."""
from dataclasses import dataclass, field
from datetime import date
from math import isfinite

from core.calculation import Calculation
from protocol import LIQUIDITY_WINDOW, DataRequest, MarketFrame, TargetSet
from strategy import validate_params

NAME = "基本面质量成长价值"
LIQUIDITY_FIELDS = ("amount", "volume")


@dataclass(frozen=True)
class Params:
    positions: int = field(default=20, metadata={"label": "最多持股数", "minimum": 1, "maximum": 1000})
    ranking: str = field(default="score", metadata={"label": "排序方法", "options": ("score", "rank")})
    growth_field: str = field(
        default="profit_growth",
        metadata={"label": "增长因子", "options": ("profit_growth", "revenue_growth")},
    )
    trend_window: int = field(default=0, metadata={"label": "短趋势窗口", "minimum": 0})

    def __post_init__(self):
        validate_params(self)


class Strategy:
    strategy_id = "quality_value"

    def __init__(self, params: Params):
        self.params = params

    def requirements(self):
        fields = ("close", "book_value", "roe", self.params.growth_field, *LIQUIDITY_FIELDS)
        if self.params.trend_window:
            fields = (*fields, "signal_close")
        return DataRequest(
            fields, max(20, self.params.trend_window)
        )

    def generate(self, frame: MarketFrame, account) -> TargetSet | None:
        if type(frame.context.get("trading")) is not bool:
            raise ValueError("trading phase must be explicit")
        if not frame.context["trading"]:
            return None
        if "next_session" not in frame.context:
            raise ValueError("next_session is required for monthly rebalance")
        following = date.fromisoformat(frame.context["next_session"])
        if following <= frame.at.date():
            raise ValueError("next_session must follow decision session")
        if (frame.at.year, frame.at.month) == (following.year, following.month):
            return None

        candidates = {}
        request = self.requirements()
        fields = tuple(name for name in request.fields
                       if name not in (*LIQUIDITY_FIELDS, "signal_close"))
        for symbol, row in frame.fields.items():
            history = frame.history[symbol]
            if len(history) < request.history_bars:
                continue
            liquid = True
            for bar in history[-LIQUIDITY_WINDOW:]:
                for name in LIQUIDITY_FIELDS:
                    value = bar.fields[name]
                    if value is None:
                        liquid = False
                    elif type(value) not in (int, float) or not isfinite(value):
                        raise ValueError(f"{symbol}: {name} must be finite numeric or None")
                    elif value <= 0:
                        liquid = False
            if not liquid:
                continue
            if self.params.trend_window:
                signal = row["signal_close"]
                if signal is not None and (
                    type(signal) not in (int, float) or not isfinite(signal) or signal <= 0
                ):
                    raise ValueError(f"{symbol}: signal_close must be finite and positive or None")
                history_signals = []
                history_available = True
                for bar in history[-self.params.trend_window:]:
                    value = bar.fields["signal_close"]
                    if value is None:
                        history_available = False
                        continue
                    if type(value) not in (int, float) or not isfinite(value) or value <= 0:
                        raise ValueError(
                            f"{symbol}: historical signal_close must be finite and positive or None"
                        )
                    history_signals.append(value)
                if signal is None or not history_available:
                    continue
                if signal <= Calculation.sma(history_signals, self.params.trend_window):
                    continue
            values = {name: row[name] for name in fields}
            for name, value in values.items():
                if value is not None and (type(value) not in (int, float) or not isfinite(value)):
                    raise ValueError(f"{symbol}: {name} must be finite numeric or None")
            if any(value is None for value in values.values()):
                continue
            close = values["close"]
            book = values["book_value"]
            roe = values["roe"]
            growth = values[self.params.growth_field]
            if roe <= 0 or book <= 0 or close <= 0:
                continue
            book_yield = book / close
            if not isfinite(book_yield):
                raise ValueError(f"{symbol}: book_yield must be finite")
            candidates[symbol] = {
                "roe": roe,
                self.params.growth_field: growth,
                "book_yield": book_yield,
            }

        ranked = ()
        if candidates:
            scored = Calculation.fundamentals(
                MarketFrame(frame.at, frame.venue, candidates, frame.context),
                {"roe": 1.0, self.params.growth_field: 1.0, "book_yield": 1.0},
                ranking=self.params.ranking,
            ).fields
            ranked = tuple(sorted(scored, key=lambda symbol: (-scored[symbol]["score"], symbol)))
        selected = ranked[:self.params.positions]
        weights = Calculation.equal_weights(selected, len(selected)) if selected else {}
        return TargetSet(
            self.strategy_id,
            frame.at,
            weights,
            {symbol: "quality_growth_value" for symbol in selected},
        )
