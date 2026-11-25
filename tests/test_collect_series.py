"""Public close-only series use the existing daily store and comparison port."""
from contextlib import closing
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from collect import methods
from market import build_data
from protocol import DataError, normalize_security_id


class SeriesTests(unittest.TestCase):
    def provider(self, text):
        return methods.FredSeries({}, transport=lambda url: text)

    def test_identity_dates_missing_observations_and_raw_evidence(self):
        text=('observation_date,OTHER\n2020-01-30,100\n2020-01-31,.\n'
              '2020-02-03,102\n2020-02-04,.\n')
        request={'codes':['fred.other'],'fields':['close'],'start':'2020-01-30','end':'2020-02-04'}
        packet=self.provider(text).collect(request)
        self.assertEqual(packet['records'],[
            {'code':'fred.other','date':'2020-01-30','close':100.},
            {'code':'fred.other','date':'2020-02-03','close':102.}])
        self.assertEqual(packet['calendar']['sessions'],['2020-01-30','2020-02-03'])
        self.assertEqual(packet['calendar']['coverage'], [{'start': '2020-01-30', 'end': '2020-02-03'}])
        self.assertEqual(packet['raw'][0]['response'],text)
        self.assertIn('id=OTHER',packet['raw'][0]['request']['url'])
        self.assertEqual(normalize_security_id('FRED.NASDAQCOM'),'fred.nasdaqcom')

    def test_wrong_identity_duplicates_bad_prices_and_dates_are_rejected(self):
        request={'codes':['fred.other'],'fields':['close'],'start':'2020-01-30','end':'2020-02-03'}
        for text in ('observation_date,WRONG\n2020-01-30,100\n',
                     'observation_date,OTHER\n2020-01-30,100\n2020-01-30,100\n',
                     'observation_date,OTHER\n2020-02-03,102\n2020-01-30,100\n',
                     'observation_date,OTHER\n2020-01-30,NaN\n',
                     'observation_date,OTHER\n2020-01-30,0\n',
                     'observation_date,OTHER\n2020-02-30,100\n',
                     'observation_date,OTHER\n2020-02-04,100\n',
                     'observation_date,OTHER\n2020-01-30,.\n'):
            with self.subTest(text=text),self.assertRaises(ValueError):
                self.provider(text).collect(request)

    def test_close_only_publication_is_readable_but_not_tradable(self):
        from protocol import DataQuery

        with tempfile.TemporaryDirectory() as folder, closing(build_data(Path(folder))) as bus:
            days = ["2020-01-31", "2020-02-03", "2020-02-04"]
            bus.save_calendar("series", {
                "sessions": [*days, "2020-02-05"],
                "coverage": [{"start": days[0], "end": "2020-02-05"}],
            })
            source = {"id": "series", "method": "fred_series", "data_type": "daily",
                      "dataset_id": "series", "storage_id": "series", "fields": ["close"],
                      "defaults": {"codes": ["fred.other"]}}
            fetch = methods.Methods(bus, {"fred_series": lambda source, request: self.provider(
                "observation_date,OTHER\n2020-01-31,100\n2020-02-03,102\n2020-02-04,103\n",
            ).collect(request)})
            request = fetch.prepare_request(source, {"codes": ["fred.other"],
                                                    "start": days[0], "end": days[-1]})
            packet = fetch.fetch(source, request)
            fetch.publish(source, packet)
            reference = bus.source("series", "bars")
            bus.save_manifest("series", {
                "schema": 3, "calendar": "calendar:series", "fields": {},
                "inputs": {"bars": reference, "calendar": "calendar:series"},
                "scope": {"symbols": ["fred.other"], "universe": "benchmark"},
            })
            view = fetch.records(source, 0, 10)
            self.assertEqual(view["records_count"], 3)
            self.assertEqual({k for k, v in view["records"][0].items() if v is not None},
                             {"code", "date", "close"})

            query = DataQuery("series", date(2020, 1, 31), date(2020, 2, 3), ("close",), 1, "raw",
                              symbols=("fred.other",), universe="dataset", purpose="compare")
            result = bus.query(query)
            self.assertEqual([r.frame.fields["fred.other"]["close"] for r in result.records],
                             [100., 102.])
            with self.assertRaisesRegex(DataError, "unknown_trade_state"):
                bus.query(DataQuery("series", query.start, query.end, ("close",), 1, "raw",
                                    symbols=("fred.other",), universe="dataset", purpose="trade"))
            before = bus.manifest("series")
            result = fetch.publish({**source, "fields": ["open", "high", "low", "close", "volume", "amount"]},
                                   packet)
            self.assertEqual(result["records"], 3)
            self.assertEqual(bus.manifest("series"), before)

    def test_unbounded_index_uses_observed_closes_and_covered_fx(self):
        from backend import Benchmarks

        sources = [
            ("index", "fred.other", "OTHER", [
                ("2020-01-30", 100), ("2020-01-31", 110), ("2020-02-03", 120),
                ("2020-02-04", 130), ("2020-02-05", 140),
            ]),
            ("fx", "fred.rate", "RATE", [("2020-01-31", 7), ("2020-02-04", 8)]),
        ]
        with tempfile.TemporaryDirectory() as folder, closing(build_data(Path(folder))) as bus:
            for dataset, code, series, observations in sources:
                source = {"id": dataset, "method": "fred_series", "data_type": "daily",
                          "dataset_id": dataset, "storage_id": dataset, "fields": ["close"],
                          "defaults": {"codes": [code]}}
                text = f"observation_date,{series}\n" + "".join(
                    f"{day},{close}\n" for day, close in observations)
                provider = self.provider(text)
                fetch = methods.Methods(bus, {"fred_series": lambda _source, request:
                                              provider.collect(request)})
                first, second = (day for day, _close in observations[:2])
                bus.save_calendar(dataset, {"sessions": [first, second],
                                            "coverage": [{"start": first, "end": second}]})
                request = fetch.prepare_request(source, {"codes": [code], "start": first,
                                                        "end": observations[-1][0]})
                fetch.publish(source, fetch.fetch(source, request))
                bus.save_manifest(dataset, {
                    "schema": 3, "calendar": "calendar:" + dataset, "fields": {},
                    "inputs": {"bars": bus.source(dataset, "bars")},
                    "scope": {"symbols": [code], "universe": "benchmark"},
                })
            alignment = {"alignment": "previous_close", "timezone": "America/New_York",
                         "comparison_timezone": "Asia/Shanghai", "comparison_close": "15:00"}
            index = {**alignment, "id": "index", "dataset_id": "index", "code": "fred.other",
                     "currency": "USD", "session_close": "16:00"}
            cny = {**index, "id": "index_cny", "currency": "CNY", "quote_currency": "USD", "fx_id": "fx"}
            fx = {**alignment, "id": "fx", "dataset_id": "fx", "code": "fred.rate",
                  "currency": "CNY", "base_currency": "USD", "session_close": "12:00"}
            service = Benchmarks(bus, [index, cny, fx])

            with self.subTest("USD chart includes native latest observation"):
                result = service.series("index", None, None)
                self.assertEqual(result["end"], "2020-02-05")
                self.assertEqual(result["points"][-1], {
                    "at": "2020-02-05", "close": 140, "observed_at": "2020-02-05",
                })

            with self.subTest("CNY chart is limited to FX coverage and records source dates"):
                result = service.series("index_cny", None, None)
                self.assertEqual((result["start"], result["end"]), ("2020-01-31", "2020-02-04"))
                self.assertEqual(result["points"], [
                    {"at": "2020-01-31", "close": 770, "observed_at": "2020-01-31",
                     "fx_observed_at": "2020-01-31"},
                    {"at": "2020-02-03", "close": 840, "observed_at": "2020-02-03",
                     "fx_observed_at": "2020-01-31"},
                    {"at": "2020-02-04", "close": 1040, "observed_at": "2020-02-04",
                     "fx_observed_at": "2020-02-04"},
                ])
                self.assertEqual(set(result["source_hashes"]), {"index_cny", "fx"})
                self.assertRegex(result["snapshot_id"], "^[a-f0-9]{64}$")


class CrossMarketBenchmarkTests(unittest.TestCase):
    def service(self, *, currency=False):
        from backend import Benchmarks
        days=['2020-01-30','2020-01-31','2020-02-03','2020-02-04']
        class Store:
            def calendar(self, dataset):
                return {'sessions': days, 'coverage': [{'start': days[0], 'end': days[-1]}]}

            def manifest(self, dataset):
                return {'calendar': 'calendar:' + dataset}
            def query(self,query):
                prices=[100.,110.,120.,130.] if query.dataset=='index' else [7.,7.1,7.2,7.3]
                return SimpleNamespace(records=[SimpleNamespace(trade_date=date.fromisoformat(day),
                    frame=SimpleNamespace(fields={query.symbols[0]:{'close':price}}))
                    for day,price in zip(days,prices) if query.start<=date.fromisoformat(day)<=query.end],
                    provenance={'data_hash':query.dataset+'-hash'})
        specs=[{'id':'composite','dataset_id':'index','code':'fred.other','currency':'USD',
                'return_type':'total','alignment':'previous_close','timezone':'America/New_York',
                'session_close':'16:00','comparison_timezone':'Asia/Shanghai','comparison_close':'15:00'}]
        if currency:
            specs[0].update(currency='CNY',quote_currency='USD',fx_id='fx')
            specs.append({**specs[0],'id':'fx','dataset_id':'fx','code':'fred.fx','currency':'CNY',
                          'fx_id':None,'quote_currency':'CNY','base_currency':'USD'})
        return Benchmarks(Store(),specs)

    def test_us_same_day_close_cannot_enter_china_close_and_holidays_keep_last_close(self):
        result=self.service().series('composite','2020-01-31','2020-02-03',
                                    dates=('2020-01-31','2020-02-01','2020-02-03'))
        self.assertEqual([p['close'] for p in result['points']],[100.,110.,110.])
        self.assertEqual([p['observed_at'] for p in result['points']],['2020-01-30','2020-01-31','2020-01-31'])
        self.assertEqual(result['return_type'],'total')
        with self.assertRaises(ValueError):
            self.service().series('composite','2020-01-30','2020-01-31',dates=('2020-01-30','2020-01-31'))

    def test_rmb_valuation_keeps_both_source_hashes(self):
        result=self.service(currency=True).series('composite','2020-01-31','2020-02-03',
                                                 dates=('2020-01-31','2020-02-03'))
        self.assertEqual([p['close'] for p in result['points']],[700.,781.])
        self.assertEqual(result['currency'],'CNY')
        self.assertEqual(result['source_hashes'],{'composite':'index-hash','fx':'fx-hash'})

    def test_inverse_fx_rate_cannot_be_silently_multiplied(self):
        service=self.service(currency=True)
        service.specs['fx']['base_currency']='CNY'
        with self.assertRaisesRegex(ValueError,'direction'):
            service.series('composite','2020-01-31','2020-02-03',dates=('2020-01-31','2020-02-03'))
