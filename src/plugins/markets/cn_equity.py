"""A-share order planning and next-session execution rules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_DOWN
from hashlib import sha256

from core.contracts import (
    AccountSnapshot,
    ExecutionResult,
    FillEvent,
    MarketFrame,
    OrderRejection,
    OrderRequest,
    Side,
    TargetSet,
)


def _decimal(value: object, name: str, *, positive: bool = False) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite() or (positive and result <= 0):
        raise ValueError(f"{name} must be a finite{' positive' if positive else ''} number")
    return result


@dataclass(frozen=True)
class AShareRules:
    venue: str
    lot_size: int
    slippage_rate: Decimal
    commission_rate: Decimal
    minimum_commission: Decimal
    max_amount_fraction: Decimal
    stamp_duty_schedule: tuple[tuple[datetime, Decimal], ...]
    transfer_fee_schedule: tuple[tuple[datetime, Decimal], ...]

    def __post_init__(self) -> None:
        if not self.venue.strip():
            raise ValueError("venue is required")
        if type(self.lot_size) is not int or self.lot_size <= 0:
            raise ValueError("lot_size must be a positive integer")
        for name in ("slippage_rate", "commission_rate", "minimum_commission"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                raise ValueError(f"{name} must be a finite non-negative Decimal")
        if (not isinstance(self.max_amount_fraction, Decimal)
                or not self.max_amount_fraction.is_finite()
                or not 0 < self.max_amount_fraction <= 1):
            raise ValueError("max_amount_fraction must be a Decimal in (0, 1]")
        self._validate_schedule(self.stamp_duty_schedule, "stamp_duty_schedule")
        self._validate_schedule(self.transfer_fee_schedule, "transfer_fee_schedule")

    @staticmethod
    def _validate_schedule(schedule: tuple[tuple[datetime, Decimal], ...], name: str) -> None:
        if not schedule:
            raise ValueError(f"{name} is required")
        previous = None
        for effective_at, rate in schedule:
            if effective_at.tzinfo is None or effective_at.utcoffset() is None:
                raise ValueError(f"{name} timestamps must be timezone-aware")
            if previous is not None and effective_at <= previous:
                raise ValueError(f"{name} must be strictly increasing")
            if not isinstance(rate, Decimal) or not rate.is_finite() or rate < 0:
                raise ValueError(f"{name} rates must be finite non-negative Decimals")
            previous = effective_at

    def stamp_duty(self, at: datetime) -> Decimal:
        return self._scheduled_rate(self.stamp_duty_schedule, at, "stamp duty")

    def transfer_fee(self, at: datetime) -> Decimal:
        return self._scheduled_rate(self.transfer_fee_schedule, at, "transfer fee")

    @staticmethod
    def _scheduled_rate(
        schedule: tuple[tuple[datetime, Decimal], ...], at: datetime, name: str
    ) -> Decimal:
        applicable = tuple(rate for effective_at, rate in schedule if effective_at <= at)
        if not applicable:
            raise ValueError(f"no {name} rate is effective at execution time")
        return applicable[-1]


class AShareMarketAdapter:
    def __init__(self, rules: AShareRules) -> None:
        self.rules = rules

    def plan(
        self,
        target: TargetSet,
        frame: MarketFrame,
        account: AccountSnapshot,
        pending: tuple[OrderRequest, ...],
    ) -> tuple[OrderRequest, ...]:
        self._venue(frame)
        if any(weight < 0 for weight in target.weights.values()):
            raise ValueError("A-share cash market is long-only")
        desired: dict[str, Decimal] = {}
        for symbol, weight in target.weights.items():
            values = self._fields(frame, symbol, ("close",))
            close = _decimal(values["close"], "close", positive=True)
            raw = account.equity * Decimal(str(weight)) / close
            lots = (raw / self.rules.lot_size).to_integral_value(rounding=ROUND_DOWN)
            desired[symbol] = lots * self.rules.lot_size
        for symbol in account.positions:
            desired.setdefault(symbol, Decimal("0"))

        old = {order.instrument: order for order in pending}
        planned: list[OrderRequest] = []
        for symbol in sorted(desired):
            current = account.positions.get(symbol, Decimal("0"))
            difference = desired[symbol] - current
            if difference == 0:
                continue
            side = Side.BUY if difference > 0 else Side.SELL
            quantity = abs(difference)
            prior = old.get(symbol)
            if prior is not None and prior.side is side and prior.quantity == quantity:
                planned.append(prior)
                continue
            payload = "|".join((target.source_id, frame.at.isoformat(), symbol, side.value, str(quantity)))
            planned.append(OrderRequest(sha256(payload.encode()).hexdigest(), frame.at, symbol, side, quantity))
        return tuple(planned)

    def _venue(self, frame: MarketFrame) -> None:
        if frame.venue != self.rules.venue:
            raise ValueError(f"expected venue {self.rules.venue}, got {frame.venue}")

    @staticmethod
    def _fields(frame: MarketFrame, symbol: str, required: tuple[str, ...]):
        if symbol not in frame.fields:
            raise KeyError(f"missing market fields for {symbol}")
        values = frame.fields[symbol]
        missing = tuple(name for name in required if name not in values)
        if missing:
            raise KeyError(f"missing fields for {symbol}: {', '.join(missing)}")
        return values


class AShareExecutionModel:
    def __init__(self, rules: AShareRules) -> None:
        self.rules = rules

    def execute(
        self,
        orders: tuple[OrderRequest, ...],
        frame: MarketFrame,
        account: AccountSnapshot,
    ) -> ExecutionResult:
        if frame.venue != self.rules.venue:
            raise ValueError(f"expected venue {self.rules.venue}, got {frame.venue}")
        cash = account.cash
        sellable = dict(account.sellable)
        fills: list[FillEvent] = []
        remaining: list[OrderRequest] = []
        rejections: list[OrderRejection] = []
        for order in orders:
            if order.created_at >= frame.at:
                raise ValueError("orders must execute on a later market frame")
            values = AShareMarketAdapter._fields(
                frame,
                order.instrument,
                ("open", "volume", "amount20", "tradable", "limit_locked"),
            )
            if type(values["tradable"]) is not bool or type(values["limit_locked"]) is not bool:
                raise TypeError("tradable and limit_locked must be bool")
            if not values["tradable"] or values["limit_locked"]:
                rejections.append(OrderRejection(order.order_id, frame.at, "not_tradable"))
                remaining.append(order)
                continue
            if _decimal(values["volume"], "volume") <= 0:
                rejections.append(OrderRejection(order.order_id, frame.at, "zero_volume"))
                remaining.append(order)
                continue

            open_price = _decimal(values["open"], "open", positive=True)
            amount20 = _decimal(values["amount20"], "amount20", positive=True)
            adverse = Decimal("1") + self.rules.slippage_rate if order.side is Side.BUY else Decimal("1") - self.rules.slippage_rate
            price = open_price * adverse
            cap_quantity = (amount20 * self.rules.max_amount_fraction / price / self.rules.lot_size).to_integral_value(rounding=ROUND_DOWN) * self.rules.lot_size
            quantity = min(order.quantity, cap_quantity)
            if order.side is Side.SELL:
                quantity = min(quantity, sellable.get(order.instrument, Decimal("0")))
            liquidating_odd_lot = (
                order.side is Side.SELL
                and quantity == order.quantity
                and quantity == sellable.get(order.instrument, Decimal("0"))
            )
            if not liquidating_odd_lot:
                quantity = (quantity / self.rules.lot_size).to_integral_value(rounding=ROUND_DOWN) * self.rules.lot_size
            if order.side is Side.BUY:
                quantity = self._affordable(quantity, price, cash, frame.at)
            if quantity <= 0:
                rejections.append(OrderRejection(order.order_id, frame.at, "quantity_not_executable"))
                remaining.append(order)
                continue

            gross = price * quantity
            commission = max(self.rules.minimum_commission, gross * self.rules.commission_rate)
            stamp = gross * self.rules.stamp_duty(frame.at) if order.side is Side.SELL else Decimal("0")
            transfer = gross * self.rules.transfer_fee(frame.at)
            fee = commission + stamp + transfer
            fills.append(FillEvent(
                order.order_id,
                frame.at,
                order.instrument,
                order.side,
                quantity,
                price,
                fee,
                {"commission": commission, "stamp_duty": stamp, "transfer_fee": transfer},
            ))
            if order.side is Side.BUY:
                cash -= gross + fee
            else:
                sellable[order.instrument] = sellable.get(order.instrument, Decimal("0")) - quantity
            if quantity < order.quantity:
                remaining.append(OrderRequest(
                    order.order_id,
                    order.created_at,
                    order.instrument,
                    order.side,
                    order.quantity - quantity,
                ))
        return ExecutionResult(frame.at, orders, tuple(fills), tuple(remaining), tuple(rejections))

    def _affordable(
        self, quantity: Decimal, price: Decimal, cash: Decimal, at: datetime
    ) -> Decimal:
        transfer_rate = self.rules.transfer_fee(at)
        lots = int(quantity / self.rules.lot_size)
        while lots > 0:
            candidate = Decimal(lots * self.rules.lot_size)
            gross = candidate * price
            commission = max(self.rules.minimum_commission, gross * self.rules.commission_rate)
            if gross + commission + gross * transfer_rate <= cash:
                return candidate
            lots -= 1
        return Decimal("0")
