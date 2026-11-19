from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from collect.methods import collect_announcements as collect
from market import build_data
from protocol import Filter, TableQuery


PREFIX = "collect:announcement_index:"


def page(total, identifiers, more):
    return json.dumps({"totalRecordNum": total, "hasMore": more,
                       "announcements": [{"announcementId": key, "orgId": "issuer",
                                          "secCode": "security", "adjunctUrl": "document.pdf"}
                                         for key in identifiers]}).encode()


def page_receipts(data):
    return [json.loads(row["settings"])
            for row in data.scan(TableQuery("configurations")).table.to_pylist()
            if row["id"].startswith(PREFIX + "index:")]


class AnnouncementFetchTests(unittest.TestCase):
    def query(self):
        return {"pageNum": "1", "pageSize": "2", "seDate": "2013-01-01~2013-02-02",
                "stock": "", "secid": "", "column": "", "plate": "", "searchkey": "实施"}

    def test_failed_month_does_not_skip_other_independent_months_or_release_batch(self):
        responses = {
            ("2013-01-01~2013-01-31", "1"): page(3, ["a", "b"], True),
            ("2013-01-01~2013-01-31", "2"): page(4, ["c", "d"], False),
            ("2013-02-01~2013-02-02", "1"): page(1, ["e"], False),
        }
        with TemporaryDirectory() as directory, closing(build_data(Path(directory))) as data:
            with self.assertRaisesRegex(ValueError, "total changed"):
                collect(self.query(), data, fetcher=lambda r: responses[(r["seDate"], r["pageNum"])])
            report = data.config(PREFIX + "manifest")
            self.assertIn("2013-02-01~2013-02-02",
                          {receipt["request"]["seDate"] for receipt in page_receipts(data)})
            self.assertFalse(report["complete"])
            self.assertEqual(report["attempted_windows"], 2)
            self.assertEqual(report["windows"], 1)
            failure, = report["failures"]
            self.assertEqual(failure["request"]["pageNum"], "2")
            with self.assertRaisesRegex(ValueError, "total changed"):
                collect(self.query(), data, fetcher=lambda _: self.fail("replay must not refetch"))
            self.assertEqual(data.config(PREFIX + "manifest"), report)

    def test_every_month_and_page_is_retained_and_cache_replay_is_identical(self):
        responses = {
            ("2013-01-01~2013-01-31", "1"): page(3, ["a", "b"], True),
            ("2013-01-01~2013-01-31", "2"): page(3, ["c"], False),
            ("2013-02-01~2013-02-02", "1"): page(1, ["d"], False),
        }
        with TemporaryDirectory() as directory, closing(build_data(Path(directory))) as data:
            first = collect(self.query(), data, fetcher=lambda r: responses[(r["seDate"], r["pageNum"])])
            self.assertEqual((first["records"], first["pages"], first["windows"]), (4, 3, 2))
            self.assertTrue(first["complete"])
            self.assertEqual(first["release_status"], "NOT_RELEASED")

            second = collect(self.query(), data, fetcher=lambda _: self.fail("replay must not refetch"))
            self.assertEqual(first, second)
            self.assertEqual(data.config(PREFIX + "manifest"), first)
            for receipt in page_receipts(data):
                self.assertEqual(receipt["request"]["stock"], "")
            for source in first["sources"]:
                raw = data.object("attachment:" + source["id"])
                self.assertEqual(sha256(raw).hexdigest(), source["sha256"])

    def test_changed_total_stops_without_declaring_complete_or_dropping_raw_page(self):
        query = {**self.query(), "seDate": "2013-01-01~2013-01-31"}
        responses = {"1": page(3, ["a", "b"], True), "2": page(4, ["c", "d"], False)}
        with TemporaryDirectory() as directory, closing(build_data(Path(directory))) as data:
            with self.assertRaisesRegex(ValueError, "total changed"):
                collect(query, data, fetcher=lambda r: responses[r["pageNum"]])
            self.assertFalse(data.config(PREFIX + "manifest")["complete"])
            self.assertEqual(len(page_receipts(data)), 2)
            self.assertEqual(data.scan(TableQuery("attachments")).table.num_rows, 2)

    def test_corrupted_cache_is_rejected_without_refresh_fallback(self):
        query = {**self.query(), "seDate": "2013-01-01~2013-01-02"}
        with TemporaryDirectory() as directory, closing(build_data(Path(directory))) as data:
            first = collect(query, data, fetcher=lambda _: page(1, ["a"], False))
            source, = first["sources"]
            stored = TableQuery("attachments", (Filter("id", "eq", source["id"]),))
            row, = data.scan(stored).table.to_pylist()
            data.delete(stored)
            data.put_rows("attachments", [{**row, "content": page(1, ["b"], False)}], keys=("id",))
            with self.assertRaisesRegex(ValueError, "response hash"):
                collect(query, data, fetcher=lambda _: self.fail("corrupt evidence must not refetch"))

    def test_security_filter_is_refused(self):
        query = {**self.query(), "stock": "individual-security"}
        with TemporaryDirectory() as directory, closing(build_data(Path(directory))) as data:
            with self.assertRaisesRegex(ValueError, "unfiltered"):
                collect(query, data, fetcher=lambda _: self.fail("validation must precede network work"))


if __name__ == "__main__":
    unittest.main()
