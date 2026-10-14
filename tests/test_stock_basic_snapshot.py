"""Provider listing evidence must be complete and immutable before use as a factor anchor."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import json
from protocal import build_data

from collect.methods import (
    StockBasicEvidenceError,
    collect_stock_basic,
    read_stock_basic,
    write_stock_basic,
)


class _Response:
    error_code = "0"
    error_msg = "success"
    fields = ["code", "code_name", "ipoDate", "outDate", "type", "status"]

    def __init__(self, rows):
        self.rows = list(rows)
        self.index = 0

    def next(self):
        if self.index >= len(self.rows):
            return False
        self.index += 1
        return True

    def get_row_data(self):
        return self.rows[self.index - 1]


class _Provider:
    def __init__(self, rows):
        self.rows = rows
        self.queries = []

    def query_stock_basic(self, code=None):
        self.queries.append(code)
        return _Response(self.rows)


class StockBasicSnapshotTests(unittest.TestCase):
    def test_collect_and_read_preserve_provider_ipo_evidence(self):
        rows = [["sz.301116", "fixture", "2022-01-18", "", "1", "1"]]
        with TemporaryDirectory() as directory:
            data = build_data(Path(directory))
            ref = write_stock_basic(data, collect_stock_basic(_Provider(rows), timeout=0, code="301116"))
            snapshot = read_stock_basic(json.loads(data.read(ref.kind, ref.identity, ref.name)))
            self.assertEqual(snapshot["raw_response"]["rows"], rows)
            self.assertEqual(snapshot["schema"], 1)
            self.assertEqual(len(snapshot["raw_response_sha256"]), 64)
            write_stock_basic(data, collect_stock_basic(_Provider(rows), timeout=0, code="301116"))
            self.assertEqual(read_stock_basic(json.loads(data.read(ref.kind, ref.identity, ref.name))), snapshot)

    def test_tamper_duplicate_and_missing_ipo_fail_closed(self):
        rows = [["sz.301116", "fixture", "2022-01-18", "", "1", "1"]]
        with TemporaryDirectory() as directory:
            data = build_data(Path(directory))
            ref = write_stock_basic(data, collect_stock_basic(_Provider(rows), timeout=0, code="301116"))
            payload = read_stock_basic(json.loads(data.read(ref.kind, ref.identity, ref.name)))
            payload["raw_response"]["rows"][0][2] = "2021-01-18"
            with self.assertRaisesRegex(StockBasicEvidenceError, "hash"):
                read_stock_basic(payload)
        for bad_rows in (
            rows + rows,
            [["sz.301116", "fixture", "", "", "1", "1"]],
            [["sz.301116", "fixture", "2022-02-30", "", "1", "1"]],
        ):
            with self.subTest(rows=bad_rows), self.assertRaises(StockBasicEvidenceError):
                collect_stock_basic(_Provider(bad_rows), timeout=0, code="301116")

    def test_targeted_query_binds_exact_code_and_rejects_empty_or_wrong_response(self):
        matching = [["sz.301116", "fixture", "2022-01-18", "", "1", "1"]]
        provider = _Provider(matching)
        snapshot = collect_stock_basic(provider, timeout=0, code="301116")
        self.assertEqual(provider.queries, ["sz.301116"])
        self.assertEqual(snapshot["request"], "query_stock_basic(sz.301116)")
        self.assertEqual(read_stock_basic(snapshot), snapshot)
        for rows in ([], [["sz.301117", "other", "2022-01-17", "", "1", "1"]]):
            with self.subTest(rows=rows), self.assertRaises(StockBasicEvidenceError):
                collect_stock_basic(_Provider(rows), timeout=0, code="301116")


if __name__ == "__main__":
    unittest.main()
