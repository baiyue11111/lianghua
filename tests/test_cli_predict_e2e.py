"""Public CLI, real backend and synthetic market data for prediction."""
import contextlib
import asyncio
import importlib
from datetime import datetime
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from frontend import cli
from market import build_data
from tests.history_fixture import make_snapshot


class PredictionCliEndToEndTests(unittest.TestCase):
    def test_account_drives_orders_and_matches_backtest_signal(self):
        from backend import Research, build_backend

        with TemporaryDirectory() as folder, contextlib.ExitStack() as stack:
            root = Path(folder)
            make_snapshot(root, symbols=('600003',))
            from market.ingest import import_legacy
            data = build_data(root)
            try:
                import_legacy(data, root, datasets=('fixture',))
                data.update_registry(lambda registry: {
                    **registry, 'sources': [], 'datasets': [{'id': 'fixture'}], 'benchmarks': [],
                })
            finally:
                data.close()
            backend = build_backend(root)
            data = build_data(root)
            stack.callback(data.close)
            stack.callback(backend.close)
            ui = importlib.import_module('frontend.ui-protocol')
            def send(request):
                if request.operation == 'service.info':
                    return [ui.UiMessage('service', 'resource', 'urn:service',
                        'urn:lianghua:ui:output:service.info:v1', {'root': str(root)},
                        correlation_id=request.request_id)]
                return asyncio.run(backend.send(request))
            stack.enter_context(patch.object(ui, '_send_backend', side_effect=send))
            config = root / 'config.json'
            config.write_text(json.dumps({
                'run_id': 'predict-e2e',
                'module': 'plugins.strategies.moving_average_cross',
                'params': {'fast_window': 1, 'slow_window': 2},
                'dataset_id': 'fixture', 'initial_cash': '10000',
                'market': {'scope': 'CN_A', 'codes': ['600003']},
                'intervals': [{'start': '2020-01-31', 'end': '2020-01-31'}],
            }))
            calendar = json.loads((root / 'data/source/fixture/calendar.json').read_text())['sessions']
            end = calendar.index('2020-01-31')
            days = calendar[end - 20:end + 1]
            account_path = root / 'account.json'

            def account(held):
                rows = []
                for day in days:
                    position = {'600003': 100} if held else {}
                    price = 11 if day >= '2020-01-31' else 10 if day >= '2020-01-20' else 9
                    rows.append({'at': day + 'T15:00:00+08:00',
                                 'cash': '8900' if held else '10000',
                                 'equity': str(8900 + 100 * price) if held else '10000',
                                 'positions': position, 'sellable': position})
                account_path.write_text(json.dumps({'start': '2020-01-31', 'end': '2020-01-31',
                                                    'pending': [], 'days': rows}))

            def run(args):
                output, errors = io.StringIO(), io.StringIO()
                with contextlib.chdir(root), contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                    code = cli.main(args)
                return code, output.getvalue(), errors.getvalue()

            account(False)
            with patch('core.kernel.datetime', wraps=datetime) as clock:
                clock.now.return_value = datetime(2020, 1, 31, 16, tzinfo=ZoneInfo('Asia/Shanghai'))
                code, output, error = run(['--predict', str(config), '--account', str(account_path)])
                self.assertEqual(code, 0, error)
                prediction = json.loads(output)
                self.assertIsNone(prediction['provenance']['as_of'])
                self.assertEqual(prediction['provenance']['query_semantics'], 'snapshot_replay')
                code, output, error = run(['--backtest', str(config)])
                self.assertEqual(code, 0, error)
                run_id = json.loads(output)['intervals'][0]['run_id']
                trace = Research(data).result(run_id)['trace']
                self.assertEqual(prediction['target'], trace['targets'][-1])
                self.assertEqual(prediction['allocation']['orders'], trace['allocations'][-1]['orders'])
                self.assertEqual([(item['side'], item['requested_shares'])
                                  for item in prediction['allocation']['orders']], [('buy', 900)])
                self.assertEqual((prediction['signal_date'], prediction['next_session']),
                                 ('2020-01-31', '2020-02-03'))

                account(True)
                code, output, error = run(['--predict', str(config), '--account', str(account_path)])
                self.assertEqual(code, 0, error)
                held_prediction = json.loads(output)
                self.assertEqual(held_prediction['target'], prediction['target'])
                self.assertEqual(held_prediction['allocation']['orders'][0]['requested_shares'], 800)

                clock.now.return_value = datetime(2020, 2, 3, 9, 25, tzinfo=ZoneInfo('Asia/Shanghai'))
                code, output, error = run(['--predict', str(config), '--account', str(account_path)])
                self.assertEqual(code, 2)
                self.assertEqual(output, '')
                self.assertIn('passed', error)
            code, output, error = run(['--predict', str(config), '--account', str(account_path)])
            self.assertEqual(code, 2)
            self.assertEqual(output, '')
            self.assertIn('passed', error)


if __name__ == '__main__':
    unittest.main()
