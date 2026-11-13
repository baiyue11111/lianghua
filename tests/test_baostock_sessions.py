import sys
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from collect.methods import BaostockCalendar, BaostockDaily


class Rows:
    error_code = '0'
    error_msg = ''
    fields = ['date', 'code', 'close']

    def next(self):
        return False


class CalendarRows:
    error_code = '0'
    error_msg = ''
    fields = ['calendar_date', 'is_trading_day']

    def __init__(self):
        self.rows = iter([['2026-11-13', '1']])

    def next(self):
        try:
            self.row = next(self.rows)
            return True
        except StopIteration:
            return False

    def get_row_data(self):
        return self.row


class BaostockSessionTests(unittest.TestCase):
    def test_calendar_waits_for_daily_session_to_logout(self):
        daily_query = threading.Event()
        release_daily = threading.Event()
        calendar_started = threading.Event()
        calendar_login = threading.Event()
        events, errors = [], []

        def login():
            name = threading.current_thread().name
            events.append(('login', name))
            if name == 'calendar':
                calendar_login.set()
            return SimpleNamespace(error_code='0')

        def logout():
            events.append(('logout', threading.current_thread().name))

        def history(*args, **kwargs):
            daily_query.set()
            if not release_daily.wait(5):
                raise RuntimeError('daily query was not released')
            return Rows()

        provider = SimpleNamespace(login=login, logout=logout,
                                   query_history_k_data_plus=history,
                                   query_trade_dates=lambda **kwargs: CalendarRows())
        daily = BaostockDaily({})
        calendar = BaostockCalendar({})

        def run(fn):
            try:
                fn()
            except Exception as error:
                errors.append(error)

        def run_calendar():
            calendar_started.set()
            calendar.collect({'start': '2026-11-13', 'end': '2026-11-13'})

        daily_thread = threading.Thread(name='daily', target=run, args=(lambda: daily.collect({
            'codes': ['sh.600000'], 'start': '2026-11-13', 'end': '2026-11-13',
            'frequency': 'w', 'fields': ['close']}),))
        calendar_thread = threading.Thread(name='calendar', target=run, args=(run_calendar,))
        with patch.dict(sys.modules, {'baostock': provider}), \
             patch.object(BaostockDaily, '_call', lambda self, fn, *args, **kwargs: fn(*args, **kwargs)):
            try:
                daily_thread.start()
                self.assertTrue(daily_query.wait(5))
                calendar_thread.start()
                self.assertTrue(calendar_started.wait(5))
                overlapped = calendar_login.wait(0.2)
            finally:
                release_daily.set()
                daily_thread.join(5)
                if calendar_thread.ident is not None:
                    calendar_thread.join(5)
        self.assertFalse(daily_thread.is_alive())
        self.assertFalse(calendar_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertFalse(overlapped, 'calendar logged in before the daily session logged out')
        self.assertEqual(events, [('login', 'daily'), ('logout', 'daily'),
                                  ('login', 'calendar'), ('logout', 'calendar')])


if __name__ == '__main__':
    unittest.main()
