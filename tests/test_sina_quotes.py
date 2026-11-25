from unittest import TestCase
from unittest.mock import MagicMock, patch

from collect.methods import CollectionError, DEFAULT_METHODS, DEFAULT_TIMEOUT


def line(symbol, *, volume='1371750266', price='4.033'):
    fields = ['沪深300ETF', '4.065', '4.023', price, '4.084', '4.031', '4.033',
              '4.034', volume, '5567364542.000', *['0'] * 20,
              '2026-11-24', '15:34:59', '00', 'D|531300|2142732.90']
    return f'var hq_str_{symbol}="{",".join(fields)}";\n'


class SinaQuotesTests(TestCase):
    def quotes(self, transport=None):
        return DEFAULT_METHODS['sina_quotes']({}, transport)

    def test_public_request_decodes_chinese_and_keeps_units_and_provider_time(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = line('sh510300').encode('gb18030')
        with patch('collect.methods.urlopen', return_value=response) as get:
            packet = self.quotes().collect({'codes': ['sh.510300']})
        request = get.call_args.args[0]
        self.assertEqual(request.full_url, 'https://hq.sinajs.cn/list=sh510300')
        self.assertEqual(request.get_header('Referer'), 'https://finance.sina.com.cn/')
        self.assertIn('Mozilla', request.get_header('User-agent'))
        self.assertEqual(get.call_args.kwargs['timeout'], DEFAULT_TIMEOUT)
        row = packet['records'][0]
        self.assertEqual((row['code'], row['name'], row['price']), ('sh.510300', '沪深300ETF', 4.033))
        self.assertEqual((row['volume'], row['amount'], row['data_at'], row['raw_page']),
                         (1371750266, 5567364542.0, '2026-11-24T07:34:59+00:00', 0))
        self.assertAlmostEqual(row['change_pct'], (4.033 / 4.023 - 1) * 100)
        self.assertEqual(packet['data_as_of'], row['data_at'])
        self.assertEqual(packet['raw'][0]['provider'], 'sina')

    def test_valid_observation_is_kept_when_requested_quotes_are_empty_missing_or_invalid(self):
        text = line('sh510300') + 'var hq_str_sz159919="";\n' + line('sh510500', volume='-')
        packet = self.quotes(lambda _: text).collect({
            'codes': ['sh.510300', 'sz.159919', 'sh.510500', 'sz.159915'], 'fields': ['amount'],
        })
        self.assertEqual(packet['records'], [{'code': 'sh.510300', 'amount': 5567364542.0,
            'data_at': '2026-11-24T07:34:59+00:00', 'raw_page': 0}])
        self.assertEqual([item['code'] for item in packet['failures']],
                         ['sz.159919', 'sh.510500', 'sz.159915'])

    def test_unknown_identity_or_malformed_envelope_rejects_entire_batch(self):
        for suffix in (line('sz159919'), 'alert(1);', line('sh510300')):
            with self.subTest(suffix=suffix), self.assertRaises(CollectionError):
                self.quotes(lambda _: line('sh510300') + suffix).collect({'codes': ['sh.510300']})

    def test_invalid_or_empty_only_is_a_collection_error(self):
        for text in ('', 'var hq_str_sh510300="";', line('sh510300', price='NaN')):
            with self.subTest(text=text), self.assertRaises(CollectionError):
                self.quotes(lambda _: text).collect({'codes': ['sh.510300']})

    def test_batches_retain_raw_page_and_report_vendor_transport_failure(self):
        urls = []

        def transport(url):
            urls.append(url)
            symbols = url.split('=', 1)[1].split(',')
            if len(urls) == 3:
                raise OSError('provider disconnected')
            return ''.join(line(symbol) for symbol in symbols)

        codes = [f'sh.{510000 + index}' for index in range(51)]
        packet = self.quotes(transport).collect({'codes': codes})
        self.assertEqual(len(urls), 3)
        self.assertEqual([row['code'] for row in packet['records']], codes[:50])
        self.assertEqual([row['raw_page'] for row in packet['records']], [0] * 25 + [1] * 25)
        self.assertEqual([item['code'] for item in packet['failures']], [codes[-1]])
