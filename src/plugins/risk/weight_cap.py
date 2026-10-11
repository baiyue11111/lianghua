"""Independent per-instrument weight cap with no redistribution."""

from dataclasses import dataclass
from math import isfinite

from core.contracts import AccountSnapshot, TargetSet


@dataclass(frozen=True)
class WeightCapRisk:
    max_absolute_weight: float

    def __post_init__(self) -> None:
        if not isfinite(self.max_absolute_weight) or self.max_absolute_weight <= 0:
            raise ValueError("max_absolute_weight must be finite and positive")

    def apply(self, target: TargetSet, account: AccountSnapshot) -> TargetSet:
        del account
        cap = self.max_absolute_weight
        weights = {
            symbol: max(-cap, min(cap, weight))
            for symbol, weight in target.weights.items()
        }
        reasons = dict(target.reasons)
        reasons.update({symbol: "risk_weight_cap" for symbol in weights if weights[symbol] != target.weights[symbol]})
        return TargetSet(target.source_id, target.as_of, weights, reasons)
