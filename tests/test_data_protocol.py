import hashlib
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch


class DataProtocolTests(unittest.TestCase):
    def test_query_reads_dataset_from_data_store_without_reader(self):
        from datetime import date
        from protocal import DataBus, DataQuery
        from storage import DataStore
        from tests.history_fixture import make_snapshot

        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            make_snapshot(root)
            bus = DataBus(DataStore(root))
            dataset = bus.query(DataQuery(
                "fixture", date(2020, 1, 31), date(2020, 2, 4),
                ("open", "high", "low", "close", "volume", "amount", "signal_close"),
                20, "adjusted",
            ))
            self.assertEqual(dataset.dataset, "fixture")
            self.assertEqual(dataset.records[-1].trade_date, date(2020, 2, 4))
            manifest = json.loads((root / "data/source/fixture/manifest.json").read_bytes())
            manifest["files"][0]["sha256"] = "0" * 64
            (root / "data/source/fixture/manifest.json").write_text(json.dumps(manifest))
            again = bus.query(DataQuery(
                "fixture", date(2020, 1, 31), date(2020, 2, 4),
                ("open", "high", "low", "close", "volume", "amount", "signal_close"),
                20, "adjusted",
            ))
            self.assertEqual(
                [record.trade_date for record in again.records],
                [record.trade_date for record in dataset.records],
            )

    def test_query_rejects_a_dataset_while_publication_is_incomplete(self):
        from datetime import date
        from protocal import DataError, DataBus, DataQuery
        from storage import DataStore
        from tests.history_fixture import make_snapshot

        with tempfile.TemporaryDirectory() as root:
            root = Path(root); make_snapshot(root)
            bus = DataBus(DataStore(root))
            manifest = bus.snapshot("fixture")
            bus.begin_publish("fixture")
            with self.assertRaisesRegex(DataError, "publication is incomplete"):
                bus.query(DataQuery("fixture", date(2020, 1, 31), date(2020, 2, 4),
                                    ("close",), 20, "adjusted"))
            bus.write("sources", "fixture", "daily/next.bin", b"new source version")
            bus.publish("fixture", manifest)
            dataset = bus.query(DataQuery("fixture", date(2020, 1, 31), date(2020, 2, 4),
                                          ("close",), 20, "adjusted"))
            self.assertEqual(dataset.records[-1].trade_date, date(2020, 2, 4))

    def test_failed_publication_remains_explicitly_unavailable(self):
        from datetime import date
        from protocal import DataError, DataBus, DataQuery
        from storage import DataStore
        from tests.history_fixture import make_snapshot

        with tempfile.TemporaryDirectory() as root:
            root = Path(root); make_snapshot(root)
            bus = DataBus(DataStore(root)); bus.begin_publish("fixture")
            bus.write("sources", "fixture", "daily/partial.tmp", b"partial")
            with self.assertRaisesRegex(DataError, "publication is incomplete"):
                bus.query(DataQuery("fixture", date(2020, 1, 31), date(2020, 2, 4),
                                    ("close",), 20, "adjusted"))

    def test_shared_source_publication_blocks_a_different_dataset_query(self):
        from datetime import date
        from protocal import DataError, DataBus, DataQuery
        from storage import DataStore
        from tests.history_fixture import make_snapshot

        with tempfile.TemporaryDirectory() as root:
            root = Path(root); make_snapshot(root)
            bus = DataBus(DataStore(root)); bus.begin_publish("other_dataset")
            with self.assertRaisesRegex(DataError, "publication is incomplete"):
                bus.query(DataQuery("fixture", date(2020, 1, 31), date(2020, 2, 4),
                                    ("close",), 20, "adjusted"))
            self.assertFalse((root / "data/.locks").exists())
            self.assertTrue((root / "data/source/.locks/source.lock").exists())

    def test_publication_owner_is_bound_to_the_writing_thread(self):
        from protocal import DataError, DataBus
        from storage import DataStore

        with tempfile.TemporaryDirectory() as root:
            root = Path(root); bus = DataBus(DataStore(root)); bus.begin_publish("fixture")
            errors = []

            def write_from_other_thread():
                for dataset in ("fixture", "other_dataset"):
                    try:
                        bus.write("sources", dataset, "daily/partial.bin", b"partial")
                    except DataError as error:
                        errors.append(error.code)

            thread = threading.Thread(target=write_from_other_thread)
            thread.start(); thread.join(2)
            self.assertEqual(errors, ["dataset_publishing", "dataset_publishing"])

    def test_source_delete_waits_for_the_same_source_lock_as_write(self):
        from protocal import DataBus
        from storage import DataStore

        with tempfile.TemporaryDirectory() as root:
            store = DataStore(Path(root)); bus = DataBus(store)
            bus.write("sources", "fixture", "daily/old.bin", b"old")
            deleted = threading.Event()
            with store.source_lock():
                thread = threading.Thread(
                    target=lambda: (bus.delete("sources", "fixture", "daily/old.bin"), deleted.set())
                )
                thread.start()
                time.sleep(0.1)
                self.assertFalse(deleted.is_set())
            thread.join(2)
            self.assertTrue(deleted.is_set())

    def test_unfinished_source_publication_guards_views_and_delete_but_not_state(self):
        from protocal import DataError, DataBus
        from storage import DataStore
        from tests.history_fixture import make_snapshot

        with tempfile.TemporaryDirectory() as root:
            root = Path(root); make_snapshot(root)
            bus = DataBus(DataStore(root))
            bus.write("state", "jobs", "job.json", b"{}")
            bus.begin_publish("fixture")
            errors = []

            def inspect_or_delete(dataset):
                for operation in (
                    lambda: bus.read("sources", dataset, "manifest.json"),
                    lambda: bus.list("sources", dataset),
                    lambda: bus.path("sources", dataset, "manifest.json"),
                    lambda: bus.delete("sources", dataset, "manifest.json"),
                ):
                    try:
                        operation()
                    except DataError as error:
                        errors.append(error.code)

            thread = threading.Thread(target=inspect_or_delete, args=("fixture",))
            other = threading.Thread(target=inspect_or_delete, args=("other_dataset",))
            thread.start(); other.start(); thread.join(2); other.join(2)
            self.assertEqual(errors.count("dataset_publishing"), 8)
            bus.delete("state", "jobs", "job.json")
            with self.assertRaises(DataError):
                bus.read("state", "jobs", "job.json")

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
            self.assertEqual(bus.snapshot('dataset'), manifest)

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

    def test_manifest_can_bind_another_source_identity_without_path_traversal(self):
        from protocal import build_data

        with tempfile.TemporaryDirectory() as root:
            bus = build_data(Path(root))
            ref = bus.write('sources', 'lake', 'daily/2025.parquet', b'price')
            manifest = {
                'schema': 2,
                'files': [{'file': 'source/lake/daily/2025.parquet',
                           'sha256': ref.sha256, 'role': 'market'}],
                'fields': {},
                'scope': {'symbols': ['600001'], 'universe': 'fixed_symbols'},
            }
            bus.write('sources', 'dataset', 'manifest.json',
                      json.dumps(manifest).encode())
            self.assertEqual(bus.snapshot('dataset'), manifest)
