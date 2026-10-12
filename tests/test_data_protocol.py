import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class DataProtocolTests(unittest.TestCase):
    def test_delete_uses_the_same_identity_checks(self):
        from protocal import build_data, DataError
        with tempfile.TemporaryDirectory() as root:
            bus = build_data(Path(root))
            bus.write('state', 'lake', 'profile.json', b'{}')
            bus.delete('state', 'lake', 'profile.json')
            with self.assertRaises(DataError):
                bus.read('state', 'lake', 'profile.json')
            with self.assertRaises(DataError):
                bus.delete('state', 'lake', '../../outside')

    def test_protocol_is_the_read_write_boundary(self):
        from protocal import DataBus
        from storage import DataStore
        with tempfile.TemporaryDirectory() as root:
            bus = DataBus(DataStore(Path(root)))
            ref = bus.write('sources', 'quotes', 'snapshot.json', b'{"price":10.12}')
            self.assertEqual(bus.read('sources', 'quotes', 'snapshot.json'), b'{"price":10.12}')
            self.assertEqual(ref.sha256, hashlib.sha256(b'{"price":10.12}').hexdigest())
            self.assertEqual(bus.list('sources', 'quotes'), (ref,))

    def test_missing_invalid_and_io_errors_are_distinct(self):
        from protocal import DataBus, DataError
        from storage import DataStore
        with tempfile.TemporaryDirectory() as root:
            store = DataStore(Path(root)); bus = DataBus(store)
            for args in [('sources', '..', 'x'), ('archive', 'x', 'x')]:
                with self.assertRaises(DataError) as caught:
                    bus.read(*args)
                self.assertEqual(caught.exception.code, 'invalid_identity')
            with self.assertRaises(DataError) as caught:
                bus.read('sources', 'quotes', 'missing.json')
            self.assertEqual(caught.exception.code, 'missing')
            with patch.object(store, 'write', side_effect=OSError('disk full')):
                with self.assertRaises(DataError) as caught:
                    bus.write('state', 'jobs', 'j.json', b'{}')
                self.assertEqual(caught.exception.code, 'io_failed')

    def test_identity_listing_does_not_read_other_groups(self):
        from protocal import DataBus
        from storage import DataStore
        with tempfile.TemporaryDirectory() as root:
            store = DataStore(Path(root)); bus = DataBus(store)
            bus.write('sources', 'quotes', 'q.json', b'{}')
            other = store.path('sources', 'large', 'other.json')
            other.parent.mkdir(parents=True)
            other.symlink_to(Path(root) / 'outside')
            self.assertEqual(len(bus.list('sources', 'quotes')), 1)
