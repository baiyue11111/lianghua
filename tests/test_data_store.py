import hashlib
import tempfile
import unittest
from pathlib import Path


class DataStoreTest(unittest.TestCase):
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
            self.assertEqual(first.file, "data/runs/r1/manifest.json")
            self.assertEqual(second.file, "data/sources/daily/bars.parquet")
            self.assertEqual(
                [(item.identity, item.name) for item in store.list("runs")],
                [("r1", "manifest.json")],
            )
            self.assertEqual(list((project / "data").rglob("*.tmp")), [])

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


if __name__ == "__main__":
    unittest.main()
