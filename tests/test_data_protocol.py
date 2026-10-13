import hashlib
import json
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

    def test_dataset_snapshot_and_publish_share_manifest_contract(self):
        from protocal import DataError, build_data

        with tempfile.TemporaryDirectory() as root:
            bus = build_data(Path(root))
            content = b'price'
            ref = bus.write('sources', 'dataset', 'daily/bars.parquet', content)
            manifest = {
                'schema': 2,
                'files': [{'file': 'daily/bars.parquet', 'sha256': ref.sha256, 'role': 'market'}],
                'fields': {},
                'scope': {'symbols': ['600001'], 'universe': 'fixed_symbols'},
            }

            published = bus.publish('dataset', manifest)

            self.assertEqual(published.name, 'manifest.json')
            self.assertEqual(bus.snapshot('dataset'), manifest)
            bus.write('sources', 'dataset', 'daily/bars.parquet', b'changed')
            with self.assertRaisesRegex(DataError, 'hash mismatch'):
                bus.snapshot('dataset')

    def test_publish_rejects_unbound_files_and_invalid_scope(self):
        from protocal import DataError, build_data

        with tempfile.TemporaryDirectory() as root:
            bus = build_data(Path(root))
            for manifest in (
                {'schema': 2, 'files': [], 'fields': {}, 'scope': {'symbols': [], 'universe': 'fixed'}},
                {'schema': 2, 'files': [{'file': 'missing.json', 'sha256': '0' * 64, 'role': 'market'}],
                 'fields': {}, 'scope': {'symbols': ['600001'], 'universe': 'fixed'}},
            ):
                with self.subTest(manifest=manifest), self.assertRaises(DataError):
                    bus.publish('dataset', manifest)
