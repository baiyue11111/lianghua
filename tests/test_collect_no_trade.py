"""Historical state evidence remains readable without coupling ordinary collection to it."""
import hashlib
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from collect.methods import Methods
from market import build_data
from protocol import DataError
from tests.history_fixture import make_snapshot

class NoTradeTests(unittest.TestCase):
    def test_stored_evidence_closes_check_only_and_rejects_corruption(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);make_snapshot(root,symbols=('600003',));bus=build_data(root);m=Methods(bus,{})
            payload=json.dumps({'records':[{'code':'sh.600003','start':'2020-01-31','end':'2020-02-03','evidence':{'url':'https://example.org/status'}}]}).encode()
            ref=bus.write('sources','fixture','no_trade.json',payload)
            manifest=bus.manifest('fixture');manifest['files'].append({'file':'source/fixture/no_trade.json','sha256':ref.sha256,'role':'market'})
            bus.write('sources','fixture','manifest.json',json.dumps(manifest).encode())
            query=lambda:m.daily_coverage('fixture',('sh.600003',),date(2020,1,31),date(2020,2,3),('close',))
            self.assertTrue(query()['complete'])
            bus.write('sources','fixture','no_trade.json',b'{"records":[]}')
            with self.assertRaises(DataError):query()
