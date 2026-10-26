import hashlib
import multiprocessing
import tempfile
import threading
import time
import unittest
from pathlib import Path


def hold_source_lock(root, ready, release):
    from storage import DataStore
    with DataStore(Path(root)).source_lock():
        ready.send(True)
        release.recv()


class DataStoreTest(unittest.TestCase):
    def test_folder_query_keeps_names_and_hashes_without_reading_siblings(self):
        from market import DataBus
        from storage import DataStore
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as directory:
            store = DataStore(Path(directory))
            bus = DataBus(store)
            for kind in ("state", "sources", "runs"):
                expected = store.write(kind, "fixture", "jobs/nested/one.json", b"{}")
                store.write(kind, "fixture", "raw/large.bin", b"unrelated")
                with patch.object(store, "_ref", wraps=store._ref) as read:
                    self.assertEqual(bus.list(kind, "fixture", "jobs"), (expected,))
                    self.assertEqual(read.call_count, 1)
                    self.assertEqual(read.call_args.args[2], "jobs/nested/one.json")
                self.assertEqual(bus.list(kind, "fixture", "missing"), ())

    def test_folder_query_rejects_escape_and_missing_identity(self):
        from market import DataBus
        from protocol import DataError
        from storage import DataStore

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            store = DataStore(project)
            store.write("state", "collect", "jobs/one.json", b"{}")
            outside = project / "outside"
            outside.mkdir()
            store.path("state", "collect", "link").symlink_to(outside, target_is_directory=True)
            for identity, folder in ((None, "jobs"), ("collect", "../outside"),
                                     ("collect", str(outside)), ("collect", "link")):
                with self.subTest(identity=identity, folder=folder):
                    with self.assertRaises(ValueError):
                        store.list("state", identity, folder)
                    with self.assertRaises(DataError):
                        DataBus(store).list("state", identity, folder)

    def test_one_root_reads_writes_and_lists_every_format(self):
        from storage import DataStore

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            store = DataStore(project)
            manifest = b'{"run_id":"r1"}'
            parquet = b"PAR1fixture"

            first = store.write("runs", "r1", "manifest.json", manifest)
            second = store.write("sources", "daily", "bars.parquet", parquet)

            self.assertEqual(store.root, project.resolve() / "data")
            self.assertEqual(store.read("runs", "r1", "manifest.json"), manifest)
            self.assertEqual(first.sha256, hashlib.sha256(manifest).hexdigest())
            self.assertEqual(first.size, len(manifest))
            self.assertEqual(first.file, "data/result/r1/manifest.json")
            self.assertEqual(second.file, "data/source/daily/bars.parquet")
            self.assertTrue((project / "data/result/r1/manifest.json").is_file())
            self.assertTrue((project / "data/source/daily/bars.parquet").is_file())
            self.assertEqual(
                [(item.identity, item.name) for item in store.list("runs")],
                [("r1", "manifest.json")],
            )
            self.assertEqual(list((project / "data").rglob("*.tmp")), [])

    def test_state_is_stored_in_the_independent_project_log_root(self):
        from storage import DataStore

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            store = DataStore(project)
            ref = store.write("state", "jobs", "j.json", b"{}")
            self.assertEqual(ref.file, "log/jobs/j.json")
            self.assertTrue((project / "log/jobs/j.json").is_file())
            self.assertFalse((project / "data/log").exists())

    def test_path_rejects_every_escape_segment(self):
        from storage import DataStore

        with tempfile.TemporaryDirectory() as directory:
            store = DataStore(Path(directory))
            for values in (("../runs", "r1", "x"), ("runs", "../r1", "x"), ("runs", "r1", "../x")):
                with self.subTest(values=values), self.assertRaises(ValueError):
                    store.path(*values)

    def test_identity_cannot_collapse_a_namespace(self):
        from storage import DataStore

        with tempfile.TemporaryDirectory() as directory:
            store = DataStore(Path(directory))
            for values in (("..", "r1", "x"), ("runs", "..", "x"), ("runs", ".", "x"), ("runs", "r1", ".")):
                with self.subTest(values=values), self.assertRaises(ValueError):
                    store.path(*values)

    def test_source_infrastructure_is_not_a_business_identity(self):
        from storage import DataStore

        with tempfile.TemporaryDirectory() as directory:
            store = DataStore(Path(directory))
            store.write("sources", "daily", "bars.parquet", b"bars")
            with store.source_lock():
                pass
            store.begin_publish("daily", "token")
            hidden = store.root / "source" / ".hiddencontrol"
            hidden.mkdir()
            (hidden / "internal.state").write_bytes(b"internal")

            self.assertEqual(store.publish_state("daily"), "token")
            self.assertEqual(
                [(item.identity, item.name) for item in store.list("sources")],
                [("daily", "bars.parquet")],
            )
            for identity, name in (
                (".locks", "source.lock"),
                (".publish", "daily.state"),
                (".hiddencontrol", "internal.state"),
            ):
                for operation in (
                    lambda: store.path("sources", identity, name),
                    lambda: store.read("sources", identity, name),
                    lambda: store.write("sources", identity, name, b"blocked"),
                    lambda: store.delete("sources", identity, name),
                    lambda: store.list("sources", identity),
                ):
                    with self.subTest(identity=identity, name=name, operation=operation):
                        with self.assertRaises(ValueError):
                            operation()

    def test_identity_symlink_cannot_cross_into_source_infrastructure(self):
        from market import DataBus
        from protocol import DataError
        from storage import DataStore

        with tempfile.TemporaryDirectory() as directory:
            store = DataStore(Path(directory))
            with store.source_lock():
                pass
            alias = store.root / "source" / "alias"
            alias.symlink_to(store.root / "source" / ".locks", target_is_directory=True)
            bus = DataBus(store)

            with self.assertRaises(ValueError):
                store.path("sources", "alias", "source.lock")
            with self.assertRaises(DataError):
                bus.path("sources", "alias", "source.lock")
            with self.assertRaises(DataError):
                bus.write("sources", "alias", "source.lock", b"blocked")

    def test_listing_rejects_symlinks_outside_the_store(self):
        from storage import DataStore

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            store = DataStore(project)
            store.write("runs", "r1", "manifest.json", b"{}")
            outside = project / "secret"
            outside.write_bytes(b"private")
            store.path("runs", "r1", "link").symlink_to(outside)
            with self.assertRaises(ValueError):
                store.list("runs")

    def test_source_lock_blocks_another_process_until_release(self):
        with tempfile.TemporaryDirectory() as directory:
            context = multiprocessing.get_context("fork")
            ready_parent, ready_child = context.Pipe()
            release_parent, release_child = context.Pipe()
            process = context.Process(target=hold_source_lock,
                                      args=(directory, ready_child, release_child))
            process.start()
            self.assertTrue(ready_parent.recv())
            acquired = threading.Event()

            def acquire():
                from storage import DataStore
                with DataStore(Path(directory)).source_lock():
                    acquired.set()

            thread = threading.Thread(target=acquire)
            thread.start()
            time.sleep(0.1)
            self.assertFalse(acquired.is_set())
            release_parent.send(True)
            thread.join(2)
            process.join(2)
            self.assertTrue(acquired.is_set())
            self.assertEqual(process.exitcode, 0)


if __name__ == "__main__":
    unittest.main()
