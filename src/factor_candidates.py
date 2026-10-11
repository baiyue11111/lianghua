"""Frozen, point-in-time cross-sectional factor-candidate scoring.

This module deliberately stops at research-candidate scores.  It does not
load data, inspect a clock, choose positions, or execute trades.  The caller
supplies already-adjusted features for one signal timestamp.

Percentiles use average one-based ranks, normalised to ``[0, 1]`` by
``(average_rank - 1) / (n - 1)``.  A one-name cross-section receives ``0.5``.
Higher-is-better factors use that percentile; lower-is-better factors use
``1 - percentile``.  Composite factors rank each component on the same
jointly valid cross-section before taking their fixed 50/50 mean.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from collections.abc import Mapping
from typing import Any


STATUS = "UNTESTED_CANDIDATE"
_SPEC_RULES: tuple[tuple[str, object], ...] = (
    ("percentile", "average_one_based_rank_normalized"),
    ("tie_policy", "exact_value_average_rank"),
    ("single_name_percentile", 0.5),
    ("missing_policy", "invalid_no_score"),
    ("non_finite_policy", "invalid_no_score"),
    ("composite_validity", "joint_common_valid_only"),
    ("ordering", "valid_score_desc_symbol_asc"),
    ("future_data", "not_used"),
)


@dataclass(frozen=True)
class FactorDefinition:
    """Immutable definition of one frozen research candidate."""

    name: str
    inputs: tuple[str, ...]
    directions: tuple[str, ...]
    weights: tuple[float, ...]

    def __post_init__(self) -> None:
        if not self.inputs or not (
            len(self.inputs) == len(self.directions) == len(self.weights)
        ):
            raise ValueError("factor definition inputs, directions, and weights must align")
        if any(direction not in {"higher", "lower"} for direction in self.directions):
            raise ValueError("factor direction must be higher or lower")
        if any(not math.isfinite(weight) or weight < 0 for weight in self.weights):
            raise ValueError("factor weights must be finite and non-negative")
        if not math.isclose(sum(self.weights), 1.0):
            raise ValueError("factor weights must sum to one")

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "inputs": list(self.inputs),
            "directions": list(self.directions),
            "weights": list(self.weights),
        }


_FACTOR_DEFINITIONS: tuple[FactorDefinition, ...] = (
    FactorDefinition("momentum_20", ("momentum_20",), ("higher",), (1.0,)),
    FactorDefinition("momentum_60", ("momentum_60",), ("higher",), (1.0,)),
    FactorDefinition("momentum_120", ("momentum_120",), ("higher",), (1.0,)),
    FactorDefinition("momentum_252", ("momentum_252",), ("higher",), (1.0,)),
    FactorDefinition("reversal_5", ("reversal_5",), ("lower",), (1.0,)),
    FactorDefinition("reversal_20", ("reversal_20",), ("lower",), (1.0,)),
    FactorDefinition("reversal_60", ("reversal_60",), ("lower",), (1.0,)),
    FactorDefinition("low_volatility_20", ("low_volatility_20",), ("lower",), (1.0,)),
    FactorDefinition("low_volatility_60", ("low_volatility_60",), ("lower",), (1.0,)),
    FactorDefinition("low_amount_20", ("low_amount_20",), ("lower",), (1.0,)),
    FactorDefinition(
        "momentum120_lowvol60",
        ("momentum_120", "low_volatility_60"),
        ("higher", "lower"),
        (0.5, 0.5),
    ),
    FactorDefinition(
        "reversal20_lowamount20",
        ("reversal_20", "low_amount_20"),
        ("lower", "lower"),
        (0.5, 0.5),
    ),
)

FACTOR_SPEC: tuple[FactorDefinition, ...] = _FACTOR_DEFINITIONS
FACTOR_NAMES: tuple[str, ...] = tuple(definition.name for definition in _FACTOR_DEFINITIONS)


def _spec_payload() -> list[dict[str, Any]]:
    return [definition.as_dict() for definition in _FACTOR_DEFINITIONS]


def factor_spec_json() -> str:
    """Return the canonical, deterministic JSON representation of the spec."""

    return json.dumps(
        {
            "version": 1,
            "status": STATUS,
            "rules": dict(_SPEC_RULES),
            "factors": _spec_payload(),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def factor_spec_hash() -> str:
    """Return the SHA-256 hash of :func:`factor_spec_json`."""

    return hashlib.sha256(factor_spec_json().encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ScoreRow:
    symbol: str
    score: float | None
    valid: bool
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "score": self.score,
            "valid": self.valid,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class FactorScores:
    """Scores for one factor, ordered by valid score descending then symbol."""

    name: str
    rows: tuple[ScoreRow, ...]

    @property
    def ordered_symbols(self) -> tuple[str, ...]:
        return tuple(row.symbol for row in self.rows)

    def _row(self, symbol: str) -> ScoreRow:
        for row in self.rows:
            if row.symbol == symbol:
                return row
        raise KeyError(symbol)

    def score(self, symbol: str) -> float | None:
        return self._row(symbol).score

    def valid(self, symbol: str) -> bool:
        return self._row(symbol).valid

    def reason(self, symbol: str) -> str:
        return self._row(symbol).reason

    def as_dict(self) -> list[dict[str, Any]]:
        return [row.as_dict() for row in self.rows]


@dataclass(frozen=True)
class FactorCandidateScores:
    """Complete immutable output for one cross-sectional scoring call."""

    factors: tuple[FactorScores, ...]
    status: str = STATUS
    spec_hash: str = factor_spec_hash()

    def factor(self, name: str) -> FactorScores:
        for factor in self.factors:
            if factor.name == name:
                return factor
        raise KeyError(name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "spec_hash": self.spec_hash,
            "spec_json": factor_spec_json(),
            "spec": _spec_payload(),
            "factors": {factor.name: factor.as_dict() for factor in self.factors},
        }


def _read_feature(row: Mapping[str, Any], name: str) -> tuple[float | None, str]:
    if name not in row:
        return None, f"missing:{name}"
    raw = row[name]
    if raw is None:
        return None, f"missing:{name}"
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None, f"non_numeric:{name}"
    if not math.isfinite(value):
        return None, f"non_finite:{name}"
    return value, "ok"


def _average_percentiles(values: Mapping[str, float]) -> dict[str, float]:
    """Compute ascending percentiles using average ranks and exact ties."""

    ordered = sorted(values.items(), key=lambda item: (item[1], item[0]))
    count = len(ordered)
    if count == 0:
        return {}
    percentiles: dict[str, float] = {}
    index = 0
    while index < count:
        end = index + 1
        value = ordered[index][1]
        while end < count and ordered[end][1] == value:
            end += 1
        average_rank = ((index + 1) + end) / 2.0
        percentile = 0.5 if count == 1 else (average_rank - 1.0) / (count - 1.0)
        for tie_index in range(index, end):
            percentiles[ordered[tie_index][0]] = percentile
        index = end
    return percentiles


def _ordered_rows(
    symbols: tuple[str, ...],
    scores: Mapping[str, float],
    reasons: Mapping[str, str],
) -> tuple[ScoreRow, ...]:
    rows = [
        ScoreRow(symbol, scores.get(symbol), symbol in scores, reasons.get(symbol, "ok"))
        for symbol in symbols
    ]
    return tuple(
        sorted(
            rows,
            key=lambda row: (
                0 if row.valid else 1,
                -row.score if row.score is not None else 0.0,
                row.symbol,
            ),
        )
    )


def score_factor_candidates(
    features: Mapping[str, Mapping[str, Any]],
) -> FactorCandidateScores:
    """Score the frozen candidates from one immutable cross-section.

    ``features`` is expected to contain already-adjusted, point-in-time
    values.  This function never mutates it, creates missing values, or
    performs I/O.  Every input symbol is retained in every factor's output;
    invalid values are represented by ``valid=False`` and ``score=None``.
    """

    symbols = tuple(sorted(features))
    if any(not isinstance(symbol, str) for symbol in symbols):
        raise TypeError("feature symbols must be strings")
    for symbol in symbols:
        if not isinstance(features[symbol], Mapping):
            raise TypeError(f"features[{symbol!r}] must be a mapping")

    states: dict[str, dict[str, tuple[float | None, str]]] = {
        name: {
            symbol: _read_feature(features[symbol], name)
            for symbol in symbols
        }
        for name in {input_name for definition in _FACTOR_DEFINITIONS for input_name in definition.inputs}
    }

    outputs: list[FactorScores] = []
    for definition in _FACTOR_DEFINITIONS:
        reasons: dict[str, str] = {}
        common_symbols: list[str] = []
        for symbol in symbols:
            problems = [
                states[input_name][symbol][1]
                for input_name in definition.inputs
                if states[input_name][symbol][1] != "ok"
            ]
            if problems:
                reasons[symbol] = problems[0]
            else:
                common_symbols.append(symbol)

        component_percentiles: list[dict[str, float]] = []
        for input_name, direction in zip(definition.inputs, definition.directions):
            raw_values = {
                symbol: states[input_name][symbol][0]
                for symbol in common_symbols
            }
            ascending = _average_percentiles(raw_values)  # type: ignore[arg-type]
            component_percentiles.append(
                {
                    symbol: value if direction == "higher" else 1.0 - value
                    for symbol, value in ascending.items()
                }
            )

        scores = {
            symbol: sum(
                weight * component[symbol]
                for weight, component in zip(definition.weights, component_percentiles)
            )
            for symbol in common_symbols
        }
        outputs.append(FactorScores(definition.name, _ordered_rows(symbols, scores, reasons)))

    return FactorCandidateScores(tuple(outputs))
