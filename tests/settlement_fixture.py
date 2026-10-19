from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D

from plugins.strategies.breadth_low_amount_reversal import Params, Strategy
from protocol import Bar, HistoryBar, HistoryDay, MarketFrame
from tests.fee_fixture import settlement as make_settlement


class SettlementFixture:
    calendar = (date(2020, 1, 31), date(2020, 2, 3), date(2020, 2, 4),
                date(2020, 2, 5), date(2020, 2, 6))

    def inputs(self, exit_market=True):
        symbols = tuple(f"{index:06d}" for index in range(1, 81))
        records = []
        for index, day in enumerate(self.calendar[:-1]):
            on = not exit_market or index < 2
            at = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
            history = {
                symbol: tuple(HistoryBar(at - timedelta(days=125 - row), {
                    "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.0,
                    "volume": 6_000_000.0,
                    "amount": 60_000_000.0 if int(symbol) <= 8 else 100_000_000.0,
                    "signal_close": 10.0 if row < 125 else
                    12.0 + int(symbol) / 10000 if on else 8.0,
                }) for row in range(126)) for symbol in symbols
            }
            frame = MarketFrame(at, "CN_EQUITY", {}, {"next_session": "2099-01-01"}, history)
            records.append(HistoryDay(
                day, frame,
                {symbol: Bar(symbol, day, 10, 11, 9, 10, 6_000_000, 60_000_000)
                 for symbol in symbols},
                {symbol: 10 for symbol in symbols},
                {symbol: D(10) for symbol in symbols},
                {symbol: D(10) for symbol in symbols},
                {symbol: D("60000000") for symbol in symbols},
                {symbol: D("60000000") for symbol in symbols},
                date(2020, 1, 30) if index == 0 else self.calendar[index - 1],
                (),
            ))
        return tuple(records)

    def run_settlement(self, records, calendar=None, *, strategy=None, trade_start=None):
        from plugins.data import Settlement

        settlement = make_settlement(D("100000"))
        strategy = strategy or Strategy(Params(breadth_window=1))
        calendar = self.calendar if calendar is None else calendar
        if len(calendar) != len(records) + 1:
            raise ValueError("calendar must contain one following-session sentinel")
        for index, record in enumerate(records):
            frame, account = settlement.apply(record, calendar[index + 1], trade_start)
            settlement.plan(strategy.generate(frame, account), strategy.strategy_id)
        return settlement.result()
