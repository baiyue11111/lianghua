"""Pure equal weights; the strategy declares its slot denominator."""
from math import isfinite


def equal_weights(symbols, slots: int, gross: float = 1.0) -> dict[str, float]:
    symbols = tuple(symbols)
    if type(slots) is not int or slots < 1 or len(set(symbols)) != len(symbols) or len(symbols) > slots:
        raise ValueError("unique symbols within positive slots required")
    if type(gross) not in (int, float) or not isfinite(gross) or not 0 <= gross <= 1:
        raise ValueError("gross must be finite in [0, 1]")
    return {symbol: gross / slots for symbol in symbols}
