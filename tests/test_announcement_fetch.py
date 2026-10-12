import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from protocal import build_data

try:
    from collect.index import collect
except ImportError:
    collect = None


def page(total, identifiers, more):
    return json.dumps({"totalRecordNum": total, "hasMore": more,
                       "announcements": [{"announcementId": key, "orgId": "issuer",
                                          "secCode": "security", "adjunctUrl": "document.pdf"}
                                         for key in identifiers]}).encode()


class AnnouncementFetchTests(unittest.TestCase):
    def test_failed_month_does_not_skip_other_independent_months_or_release_batch(self):
        responses={('2013-01-01~2013-01-31','1'):page(3,['a','b'],True),
                   ('2013-01-01~2013-01-31','2'):page(4,['c','d'],False),
                   ('2013-02-01~2013-02-02','1'):page(1,['e'],False)}
        with tempfile.TemporaryDirectory() as directory:
            root=build_data(Path(directory)).path("sources", "announcement_index", "manifest.json").parent
            with self.assertRaisesRegex(ValueError,'total changed'):
                collect(self.query(),build_data(Path(directory)),fetcher=lambda r:responses[(r['seDate'],r['pageNum'])])
            report=json.loads((root/'manifest.json').read_text())
            captured=[json.loads(p.read_text()) for p in root.glob('index_*.json')]
            self.assertIn('2013-02-01~2013-02-02',{p['request']['seDate'] for p in captured})
            self.assertFalse(report['complete'])
            self.assertEqual(report['attempted_windows'],2)
            self.assertEqual(report['windows'],1)
            self.assertEqual(len(report['failures']),1)
            self.assertEqual(report['failures'][0]['request']['pageNum'],'2')
            before=(root/'manifest.json').read_bytes()
            def forbidden(request): self.fail('independent failure replay must not refetch or refresh')
            with self.assertRaisesRegex(ValueError,'total changed'): collect(self.query(),build_data(Path(directory)),fetcher=forbidden)
            self.assertEqual((root/'manifest.json').read_bytes(),before)

    def query(self):
        return {"pageNum": "1", "pageSize": "2", "seDate": "2013-01-01~2013-02-02",
                "stock": "", "secid": "", "column": "", "plate": "", "searchkey": "实施"}

    def test_every_month_and_page_is_retained_and_cache_replay_is_identical(self):
        self.assertIsNotNone(collect, "uniform announcement collector is missing")
        responses = {("2013-01-01~2013-01-31", "1"): page(3, ["a", "b"], True),
                     ("2013-01-01~2013-01-31", "2"): page(3, ["c"], False),
                     ("2013-02-01~2013-02-02", "1"): page(1, ["d"], False)}
        def fetch(request):
            return responses[(request["seDate"], request["pageNum"])]
        with tempfile.TemporaryDirectory() as directory:
            first = collect(self.query(), build_data(Path(directory)), fetcher=fetch)
            self.assertEqual(first["records"], 4)
            self.assertEqual(first["pages"], 3)
            self.assertEqual(first["windows"], 2)
            self.assertTrue(first["complete"])
            self.assertEqual(first["release_status"], "NOT_RELEASED")
            before = (build_data(Path(directory)).path("sources", "announcement_index", "manifest.json")).read_bytes()
            def forbidden(request):
                self.fail("verified captured inputs must not be fetched again")
            second = collect(self.query(), build_data(Path(directory)), fetcher=forbidden)
            self.assertEqual(first, second)
            self.assertEqual(before, (build_data(Path(directory)).path("sources", "announcement_index", "manifest.json")).read_bytes())
            for source in first["sources"]:
                payload = json.loads((build_data(Path(directory)).path("sources", "announcement_index", source["file"])).read_text())
                self.assertEqual(payload["request"]["stock"], "")
                self.assertEqual(hashlib.sha256(payload["raw_response"].encode()).hexdigest(),
                                 payload["raw_response_sha256"])

    def test_changed_total_stops_without_declaring_complete_or_dropping_raw_page(self):
        self.assertIsNotNone(collect, "uniform announcement collector is missing")
        query = self.query(); query["seDate"] = "2013-01-01~2013-01-31"
        responses = {"1": page(3, ["a", "b"], True), "2": page(4, ["c", "d"], False)}
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "total changed"):
                collect(query, build_data(Path(directory)), fetcher=lambda r: responses[r["pageNum"]])
            report = json.loads((build_data(Path(directory)).path("sources", "announcement_index", "manifest.json")).read_text())
            self.assertFalse(report["complete"])
            self.assertEqual(len(list(build_data(Path(directory)).path("sources", "announcement_index", "manifest.json").parent.glob("index_*.json"))), 2)

    def test_corrupted_cache_is_rejected_without_refresh_fallback(self):
        self.assertIsNotNone(collect, "uniform announcement collector is missing")
        query = self.query(); query["seDate"] = "2013-01-01~2013-01-02"
        with tempfile.TemporaryDirectory() as directory:
            root = build_data(Path(directory)).path("sources", "announcement_index", "manifest.json").parent
            collect(query, build_data(Path(directory)), fetcher=lambda r: page(1, ["a"], False))
            path = next(root.glob("index_*.json")); payload = json.loads(path.read_text())
            payload["raw_response"] = payload["raw_response"].replace('"a"', '"b"')
            path.write_text(json.dumps(payload))
            def forbidden(request):
                self.fail("corrupt evidence must not trigger source refresh")
            with self.assertRaisesRegex(ValueError, "response hash"):
                collect(query, build_data(Path(directory)), fetcher=forbidden)

    def test_security_filter_is_refused(self):
        self.assertIsNotNone(collect, "uniform announcement collector is missing")
        query = self.query(); query["stock"] = "individual-security"
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "unfiltered"):
                collect(query, build_data(Path(directory)), fetcher=lambda r: page(0, [], False))


if __name__ == "__main__":
    unittest.main()
