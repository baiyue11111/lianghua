"""Pure normalization of corporate-action facts and as-of adjustment signals."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import math
import re
from numbers import Integral


class InvalidCorporateAction(ValueError):
    pass


def _date(value, field: str, required: bool = False) -> date | None:
    if value in (None, ""):
        if required:
            raise InvalidCorporateAction(f"missing required date: {field}")
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise InvalidCorporateAction(f"invalid {field}: {value!r}") from exc


_NUMBER = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$")


def _number(value, field: str, default: float = 0.0) -> float:
    if value in (None, ""):
        if math.isnan(default):
            raise InvalidCorporateAction(f"missing numeric {field}")
        return default
    text = str(value).strip()
    if not _NUMBER.fullmatch(text):
        raise InvalidCorporateAction(f"non-numeric {field}: {value!r}")
    try:
        result = float(Decimal(text))
    except (InvalidOperation, ValueError) as exc:
        raise InvalidCorporateAction(f"non-numeric {field}: {value!r}") from exc
    if not math.isfinite(result):
        raise InvalidCorporateAction(f"non-finite {field}: {value!r}")
    return result


@dataclass(frozen=True)
class CorporateActionEvent:
    code: str
    registration_date: date | None
    operation_date: date
    payment_date: date | None
    stock_market_date: date | None
    cash_per_share: float
    stock_dividend_per_share: float
    reserve_to_stock_per_share: float
    share_ratio: float
    cash_after_tax_text: str | None
    executable: bool
    issues: tuple[str, ...]


@dataclass(frozen=True)
class DistributionEntitlement:
    event: CorporateActionEvent
    registration_shares: int
    gross_cash: Decimal
    bonus_shares: Decimal
    reserve_shares: Decimal
    cash_due: date | None
    shares_due: date | None
    tax_verified: bool
    requires_fractional_allocation: bool


def calculate_entitlement(event: CorporateActionEvent, snapshot_date: date,
                          shares: int) -> DistributionEntitlement:
    """Calculate gross claims from a registration-close snapshot, not live holdings.

    No posting, rounding, tax estimation or reinvestment occurs here. Fractional
    shares remain explicit until an independently specified allocation rule.
    """
    if not event.executable or snapshot_date != event.registration_date:
        raise InvalidCorporateAction('valid registration-close snapshot required')
    if isinstance(shares, bool) or not isinstance(shares, Integral) or shares < 0:
        raise InvalidCorporateAction('registration shares must be nonnegative integers')
    count = Decimal(int(shares))
    cash = count * Decimal(str(event.cash_per_share))
    bonus = count * Decimal(str(event.stock_dividend_per_share))
    reserve = count * Decimal(str(event.reserve_to_stock_per_share))
    fractional = any(value != value.to_integral_value() for value in (bonus, reserve))
    return DistributionEntitlement(event, int(shares), cash, bonus, reserve,
                                   event.payment_date, event.stock_market_date,
                                   False, fractional)


@dataclass(frozen=True)
class AdjustmentSignal:
    code: str
    as_of: date
    fore_factor: float
    back_factor: float
    coverage_start: date
    coverage_end: date
    source_operation_date: date | None
    anchor_source: str


@dataclass(frozen=True)
class ReconciledAction:
    security_id: str
    distribution_id: str
    event: CorporateActionEvent
    observations: tuple[tuple[str, CorporateActionEvent], ...]


def reconcile_corporate_actions(observations, identities) -> tuple[ReconciledAction, ...]:
    """Coalesce identical gross terms by explicit security and distribution identity.

    Observations are (source, distribution_id, event). The caller must establish
    each distribution identity from source evidence, never row index or amount.
    Distinct plans can pay on the same day; they remain separate entitlements.
    Preserve all source observations, including provider tax text. Conflicting
    terms within one plan fail; no provider selection or amount summation occurs.
    The caller owns source hashes and identity evidence; no identity fallback.
    """
    groups = {}
    for source, distribution_id, event in observations:
        security_id = identities[event.code]
        if not source or not security_id or not isinstance(distribution_id, str) or not distribution_id.strip():
            raise InvalidCorporateAction('source, security and distribution identity required')
        canonical = replace(event, code=security_id, cash_after_tax_text=None)
        key = (security_id, distribution_id)
        if key not in groups:
            groups[key] = (canonical, {})
        previous, evidence = groups[key]
        if previous != canonical:
            raise InvalidCorporateAction(f'conflicting corporate action terms: {key}')
        if source in evidence and evidence[source] != event:
            raise InvalidCorporateAction(f'conflicting source observation: {source}')
        evidence[source] = event
    return tuple(ReconciledAction(key[0], key[1], event, tuple(sorted(evidence.items())))
                 for key, (event, evidence) in sorted(groups.items()))


def normalize_dividend_records(records) -> tuple[CorporateActionEvent, ...]:
    """Normalize gross terms, not investor taxes.

    ``executable`` checks required settlement dates only; it does not certify
    after-tax ledger readiness. Every event retains ``tax_unverified``.
    Cash never contributes to ``share_ratio``.
    """
    out = []
    seen = set()
    for record in records:
        code = str(record.get("code", "")).strip()
        if not code:
            raise InvalidCorporateAction("missing code")
        operation = _date(record.get("dividOperateDate"), "dividOperateDate", True)
        key = (code, operation)
        if key in seen:
            raise InvalidCorporateAction(f"duplicate corporate action: {code} {operation}")
        seen.add(key)
        after_tax = record.get("dividCashPsAfterTax")
        cash = _number(record.get("dividCashPsBeforeTax"), "dividCashPsBeforeTax")
        stock = _number(record.get("dividStocksPs"), "dividStocksPs")
        reserve = _number(record.get("dividReserveToStockPs"), "dividReserveToStockPs")
        if min(cash, stock, reserve) < 0:
            raise InvalidCorporateAction(f"negative corporate action value: {key}")
        registration = _date(record.get("dividRegistDate"), "dividRegistDate")
        payment = _date(record.get("dividPayDate"), "dividPayDate")
        market = _date(record.get("dividStockMarketDate"), "dividStockMarketDate")
        if cash == 0 and stock == 0 and reserve == 0:
            raise InvalidCorporateAction(f"no economic terms: {key}")
        issues = ["tax_unverified"]
        if registration is None:
            issues.append("registration_date")
        elif registration >= operation:
            issues.append("registration_not_before_operation")
        if cash > 0 and payment is None:
            issues.append("payment_date")
        elif cash > 0 and payment < operation:
            issues.append("payment_before_operation")
        if stock + reserve > 0 and market is None:
            issues.append("stock_market_date")
        elif stock + reserve > 0 and market < operation:
            issues.append("stock_market_before_operation")
        issues = tuple(issues)
        blocking = tuple(issue for issue in issues if issue != "tax_unverified")
        out.append(CorporateActionEvent(
            code=code, registration_date=registration, operation_date=operation,
            payment_date=payment, stock_market_date=market,
            cash_per_share=cash, stock_dividend_per_share=stock,
            reserve_to_stock_per_share=reserve,
            share_ratio=1.0 + stock + reserve,
            cash_after_tax_text=None if after_tax in (None, "") else str(after_tax),
            executable=not blocking, issues=issues))
    return tuple(out)


def adjustment_factor_asof(code: str, records, as_of: date, coverage_start: date,
                           coverage_end: date, anchor_fore: float,
                           anchor_back: float) -> AdjustmentSignal:
    """Return the last factor effective on ``as_of``; no implicit factor-one anchor."""
    if not code or coverage_start is None or coverage_end is None or anchor_fore is None or anchor_back is None:
        raise InvalidCorporateAction("factor coverage and initial anchors are required")
    if coverage_start > coverage_end or not (coverage_start <= as_of <= coverage_end):
        raise InvalidCorporateAction("as_of is outside factor coverage")
    fore = _number(anchor_fore, "anchor_fore", default=float("nan"))
    back = _number(anchor_back, "anchor_back", default=float("nan"))
    if fore <= 0 or back <= 0:
        raise InvalidCorporateAction("factor anchors must be positive")
    selected = []
    seen = set()
    for record in records:
        record_code = str(record.get("code", "")).strip()
        if not record_code or record_code != code:
            raise InvalidCorporateAction("factor records must have one explicit matching code")
        operation = _date(record.get("dividOperateDate", record.get("date")), "dividOperateDate", True)
        key = (record_code, operation)
        if key in seen:
            raise InvalidCorporateAction(f"duplicate factor event: {key}")
        seen.add(key)
        if not (coverage_start <= operation <= coverage_end):
            raise InvalidCorporateAction(f"factor event outside coverage: {operation}")
        record_fore = _number(record.get("foreAdjustFactor"), "foreAdjustFactor", default=float("nan"))
        record_back = _number(record.get("backAdjustFactor"), "backAdjustFactor", default=float("nan"))
        if record_fore <= 0 or record_back <= 0:
            raise InvalidCorporateAction("adjustment factors must be positive")
        if operation <= as_of:
            selected.append((operation, record_fore, record_back))
    if selected:
        source, fore, back = sorted(selected)[-1]
    else:
        source = None
    return AdjustmentSignal(code=code, as_of=as_of, fore_factor=fore,
                            back_factor=back, coverage_start=coverage_start,
                            coverage_end=coverage_end,
                            source_operation_date=source,
                            anchor_source="explicit_input")
