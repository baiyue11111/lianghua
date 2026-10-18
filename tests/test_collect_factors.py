import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from market import build_data
from protocol import DataError
from collect import methods as factors


class FactorStorageTests(unittest.TestCase):
    def test_evidence_is_written_and_verified_through_protocol(self):
        request = factors.Request('adjust', '600000', '2020-01-01', '2020-12-31', None)
        response = SimpleNamespace(error_code='0', error_msg='', code='sh.600000',
                                   fields=['code', 'dividOperateDate', 'foreAdjustFactor', 'backAdjustFactor'],
                                   data=[['sh.600000', '2020-01-01', '1.2', '1.1']])
        response.next = Mock(side_effect=[True, False])
        response.get_row_data = Mock(return_value=response.data[0])
        provider = SimpleNamespace(query_adjust_factor=Mock(return_value=response))
        with tempfile.TemporaryDirectory() as root:
            bus = build_data(Path(root))
            ref = factors.fetch_and_cache(request, provider, bus, timeout=0)
            self.assertEqual((ref.identity, ref.name), ('facts', f'{request.key}.json'))
            self.assertEqual(factors.pending_requests([request], bus), [])
            payload = json.loads(bus.read(ref.kind, ref.identity, ref.name))
            raw_log = json.loads(bus.read('state', 'collect', bus.list('state', 'collect')[0].name))
            self.assertEqual(payload['source_hash'], raw_log['raw_response_sha256'])
            payload['records'][0]['forward'] = -9
            bus.write(ref.kind, ref.identity, ref.name, json.dumps(payload).encode())
            with self.assertRaises(factors.CacheIntegrityError):
                factors.pending_requests([request], bus)

    def test_failed_provider_creates_no_cache(self):
        request = factors.Request('adjust', '600000', '2020-01-01', '2020-12-31', None)
        provider = SimpleNamespace(query_adjust_factor=Mock(return_value=SimpleNamespace(error_code='10002007', error_msg='failed')))
        with tempfile.TemporaryDirectory() as root:
            bus = build_data(Path(root))
            with self.assertRaises(factors.ProviderError):
                factors.fetch_and_cache(request, provider, bus, timeout=0)
            self.assertEqual(bus.list('sources', 'facts'), ())
            provider.query_adjust_factor.assert_called_once()
