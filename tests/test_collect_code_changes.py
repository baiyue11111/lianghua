"""Collection reads the same code-change objects as the data module."""
import tempfile
import unittest
from pathlib import Path

from collect.methods import Methods
from market import build_data
from protocol import DataError


class CodeChangeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.bus = build_data(Path(self.temp.name))
        self.addCleanup(self.bus.close)
        self.methods = Methods(self.bus)

    def test_imported_config_events_are_read(self):
        events = [{'exchange': 'SZ', 'old_code': '000001', 'new_code': '000002'}]
        reference = self.bus.save_config('facts/security_code_changes.json',
                                         {'schema': 1, 'events': events})
        try:
            actual = self.methods._changes({'inputs': {'code_changes': reference}})
        except DataError as error:
            self.fail(f'valid imported code-change object rejected: {error}')
        self.assertEqual(actual, events)

    def test_invalid_event_object_is_rejected(self):
        reference = self.bus.save_config('facts/bad_changes.json', {'events': 'invalid'})
        with self.assertRaises(DataError):
            self.methods._changes({'inputs': {'code_changes': reference}})

    def test_native_event_tables_remain_readable(self):
        events = [{'exchange': 'SZ', 'old_code': '000001', 'new_code': '000002'}]
        self.bus.put_rows('code_changes', events, keys=('exchange', 'old_code'))
        for reference in ('lake:code_changes', 'changes:code_changes'):
            with self.subTest(reference=reference):
                self.assertEqual(self.methods._changes({'inputs': {'code_changes': reference}}), events)


if __name__ == '__main__':
    unittest.main()
