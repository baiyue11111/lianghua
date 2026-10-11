"""Pure, causal strategy activation over net shadow returns.

Decisions are made after close and are eligible for next-session execution.
Paused strategies continue their independent shadow ledger. This module does
not read files, trade, or claim that a gate policy has passed validation.
"""
from dataclasses import dataclass
from datetime import date
from math import isfinite, sqrt
from statistics import fmean, stdev


@dataclass(frozen=True)
class GatePolicy:
    lookback: int
    entry_sharpe: float
    exit_sharpe: float
    max_drawdown: float
    confirmations: int
    min_active_days: int

    def __post_init__(self):
        for name in ("lookback", "confirmations", "min_active_days"):
            value = getattr(self, name)
            if type(value) is not int or value < (2 if name == "lookback" else 0 if name == "min_active_days" else 1):
                raise ValueError(f"invalid {name}")
        if not all(isfinite(x) for x in (self.entry_sharpe, self.exit_sharpe, self.max_drawdown)):
            raise ValueError("policy thresholds must be finite")
        if self.entry_sharpe <= self.exit_sharpe or not 0 < self.max_drawdown < 1:
            raise ValueError("entry must exceed exit; drawdown must be in (0,1)")


@dataclass(frozen=True)
class ShadowReturn:
    day: date
    net_return: float

    def __post_init__(self):
        if type(self.day) is not date or not isfinite(self.net_return) or self.net_return <= -1:
            raise ValueError("shadow return requires a date and finite return greater than -1")


@dataclass(frozen=True)
class StateEvent:
    day: date
    state: str
    transition: str | None
    reason: str
    sharpe: float | None
    recent_return: float | None
    drawdown: float | None
    confirmation_count: int


def replay_states(points: tuple[ShadowReturn, ...], policy: GatePolicy,
                  *, qualified_on: date | None) -> tuple[StateEvent, ...]:
    """Replay in supplied trading-session order; never inspect a future row.

Window statistics use net shadow returns, including the opening equity of
the window. Hard drawdown exits bypass confirmation and minimum active age.
Qualification becomes knowable on ``qualified_on``, never retrospectively.
"""
    if any(a.day >= b.day for a, b in zip(points, points[1:])):
        raise ValueError("shadow dates must be strictly increasing and unique")
    state, streak, active_since, ever_active = "watch", 0, None, False
    events = []
    for i, point in enumerate(points):
        transition, sharpe, total, drawdown = None, None, None, None
        previous = state
        if qualified_on is None or point.day < qualified_on:
            reason, streak = "historical_qualification_unavailable", 0
        elif i + 1 < policy.lookback:
            reason, streak = "insufficient_shadow_history", 0
        else:
            window = [p.net_return for p in points[i+1-policy.lookback:i+1]]
            deviation = stdev(window)
            sharpe = fmean(window) / deviation * sqrt(252) if deviation > 1e-12 else None
            equity, peak, drawdown = 1.0, 1.0, 0.0
            for value in window:
                equity *= 1 + value
                peak = max(peak, equity)
                drawdown = min(drawdown, equity / peak - 1)
            total = equity - 1
            if drawdown <= -policy.max_drawdown:
                reason, streak = "drawdown_limit", 0
                state = "paused" if ever_active else "watch"
            elif state == "active":
                weak = sharpe is None or sharpe < policy.exit_sharpe
                streak = streak + 1 if weak else 0
                reason = "exit_confirmation" if weak else "maintain_active"
                if streak >= policy.confirmations and i - active_since >= policy.min_active_days:
                    state, reason, streak = "paused", "recent_performance_deteriorated", 0
            else:
                strong = sharpe is not None and sharpe >= policy.entry_sharpe
                streak = streak + 1 if strong else 0
                reason = "entry_confirmation" if strong else "entry_conditions_not_met"
                if streak >= policy.confirmations:
                    state, active_since, streak = "active", i, 0
                    reason = "recent_performance_recovered" if ever_active else "entry_conditions_confirmed"
        if state != previous:
            transition = "paused" if state == "paused" else "resumed" if ever_active else "activated"
        if state == "active":
            ever_active = True
        events.append(StateEvent(point.day, state, transition, reason, sharpe,
                                 total, drawdown, streak))
    return tuple(events)
