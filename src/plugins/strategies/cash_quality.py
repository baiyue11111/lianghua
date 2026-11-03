"""Monthly PIT quality ranks with an optional preference for smaller sales.

The revenue field is a source-reported cumulative CNY amount. It is not market
capitalization, is not annualized, and is not industry-neutral.
"""
from dataclasses import dataclass, field
from datetime import date
from math import isfinite

from core.calculation import Calculation
from protocol import LIQUIDITY_WINDOW, DataRequest, MarketFrame, TargetSet
from strategy import validate_params

NAME = "现金质量"
FACT_FIELDS = ("roe", "net_margin", "debt_ratio", "eps", "cash_per_share")
LIQUIDITY_FIELDS = ("amount", "volume")


@dataclass(frozen=True)
class Params:
    positions: int = field(default=20, metadata={"label": "最多持股数", "minimum": 1, "maximum": 1000})
    size_weight: float = field(default=0.0, metadata={"label": "营收规模权重", "minimum": 0})

    def __post_init__(self):
        validate_params(self)


class Strategy:
    strategy_id = "cash_quality"

    def __init__(self, params: Params):
        self.params = params

    def requirements(self):
        fields = FACT_FIELDS
        if self.params.size_weight > 0:
            fields = (*fields, "revenue")
        return DataRequest((*fields, *LIQUIDITY_FIELDS), LIQUIDITY_WINDOW)

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
        for symbol, row in frame.fields.items():
            history = frame.history[symbol]
            if len(history) < LIQUIDITY_WINDOW:
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

            fields = FACT_FIELDS
            if self.params.size_weight > 0:
                fields = (*fields, "revenue")
            values = {name: row[name] for name in fields}
            for name, value in values.items():
                if value is not None and (type(value) not in (int, float) or not isfinite(value)):
                    raise ValueError(f"{symbol}: {name} must be finite numeric or None")
            if any(value is None for value in values.values()):
                continue
            if (values["roe"] <= 0 or values["net_margin"] <= 0
                    or values["debt_ratio"] < 0 or values["debt_ratio"] > 1
                    or values["eps"] <= 0 or values["cash_per_share"] <= 0):
                continue
            if self.params.size_weight > 0 and values["revenue"] <= 0:
                continue
            candidates[symbol] = {
                "roe": values["roe"],
                "net_margin": values["net_margin"],
                "debt_ratio": values["debt_ratio"],
            }
            if self.params.size_weight > 0:
                candidates[symbol]["revenue"] = values["revenue"]

        ranked = ()
        if candidates:
            weights = {"roe": 1.0, "net_margin": 1.0, "debt_ratio": -1.0}
            if self.params.size_weight > 0:
                weights["revenue"] = -self.params.size_weight
            scored = Calculation.fundamentals(
                MarketFrame(frame.at, frame.venue, candidates, frame.context),
                weights,
                ranking="rank",
            ).fields
            ranked = tuple(sorted(scored, key=lambda symbol: (-scored[symbol]["score"], symbol)))
        selected = ranked[:self.params.positions]
        weights = Calculation.equal_weights(selected, len(selected)) if selected else {}
        return TargetSet(
            self.strategy_id,
            frame.at,
            weights,
            {symbol: "cash_quality" for symbol in selected},
        )
