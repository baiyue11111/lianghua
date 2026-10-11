from __future__ import annotations

from math import isfinite


def histories(frame, window):
    result = {}
    for symbol, rows in sorted(frame.history.items()):
        prices = [row.fields.get('signal_close') for row in rows]
        if len(prices) < window or any(type(x) not in (int, float) or not isfinite(x) or x <= 0 for x in prices[-window:]):
            continue
        result[symbol] = prices
    return result


def weights(symbols, exposure):
    return {symbol: exposure / len(symbols) for symbol in symbols} if symbols and exposure else {}
