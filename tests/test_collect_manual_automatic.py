"""Manual controls must keep their task while automatic collection runs."""
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from collect.core import Collect
from collect.methods import Methods
from collect.sources import Sources
from market import build_data


class ManualAutomaticTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.bus = build_data(Path(self.folder.name))
        self.sources = Sources(self.bus)
        for name in ('manual', 'automatic'):
            self.sources.register({'id': name, 'name': name, 'method': 'fixture',
                                   'frequency': 'daily', 'defaults': {}})
        self.bus.update_registry(lambda registry: {**registry, 'markets': [{
            'scope': 'TEST', 'timezone': 'Asia/Shanghai', 'session_close': '15:00',
            'calendar': 'calendar:test', 'collect': {'default': ['daily'],
                'data': {'daily': {'source': 'automatic', 'codes': ['A']}}},
        }]})
        self.bus.save_calendar('test', {'sessions': ['2026-11-11', '2026-11-12'],
                               'coverage': [{'start': '2026-11-11', 'end': '2026-11-12'}]})
        self.collect = Collect(self.sources, Methods(self.bus))
        self.manual_entered, self.manual_release = threading.Event(), threading.Event()
        self.auto_entered, self.auto_release = threading.Event(), threading.Event()
        self.now = datetime(2026, 11, 11, 15, 30, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        self.patches = [
            patch.object(self.collect.methods, 'fetch', side_effect=self.fetch),
            patch.object(self.collect.methods, 'validate'),
            patch.object(self.collect.methods, 'prepare', return_value={}),
            patch.object(self.collect.methods, 'publish', return_value={'records': 0, 'failures': [], 'ref': None}),
            patch.object(self.collect.methods, 'log'),
        ]
        for item in self.patches:
            item.start()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.manual_release.set()
        self.auto_release.set()
        if hasattr(self, 'worker'):
            self.worker.join(5)
        self.collect.close()
        for item in reversed(self.patches):
            item.stop()
        self.bus.close()
        self.folder.cleanup()

    def fetch(self, source, request):
        entered, release = ((self.manual_entered, self.manual_release) if source['id'] == 'manual'
                            else (self.auto_entered, self.auto_release))
        entered.set()
        if not release.wait(5):
            raise RuntimeError('test timed out')
        return {'records': [], 'raw': [], 'scope': {}, 'request': request}

    def start_manual(self):
        self.collect.handle('collection.start', {'source_id': 'manual'})
        self.assertTrue(self.manual_entered.wait(5))
        return self.collect.handle('collection.current', {})['job_id']

    def start_automatic(self):
        with patch('collect.core.time.time', return_value=self.now):
            self.collect.handle('collection.set_automatic', {'market': 'TEST', 'enabled': True})
        def run_market(_request):
            job = self.collect._run(self.sources.get('automatic'), {'codes': ['A']})
            return {'market': 'TEST', 'outcome': job['outcome'], 'jobs': [job],
                    'failures': job['failures'], 'error': job['error']}
        self.market_patch = patch.object(self.collect, 'run', side_effect=run_market)
        self.market_patch.start()
        self.addCleanup(self.market_patch.stop)
        self.worker = threading.Thread(target=self.collect.tick, args=(self.now,))
        self.worker.start()
        self.assertTrue(self.auto_entered.wait(5))
        state = self.collect.handle('collection.automatic', {'market': 'TEST'})
        self.assertTrue(state['running'])
        self.assertEqual(state['job']['source_id'], 'automatic')
        return state['job']['job_id']

    def test_stop_keeps_targeting_manual_task_while_automatic_is_running(self):
        manual = self.start_manual()
        automatic = self.start_automatic()
        self.assertNotEqual(manual, automatic)
        self.assertEqual(self.collect.handle('collection.current', {})['job_id'], manual)
        self.collect.handle('collection.stop', {})
        self.manual_release.set()
        self.collect._fetch_thread.join(5)
        self.assertEqual(self.collect.status(manual)['outcome'], 'cancelled')
        self.assertEqual(self.collect.status(automatic)['status'], 'running')

    def test_merge_keeps_targeting_manual_task_while_automatic_is_running(self):
        manual = self.start_manual()
        self.manual_release.set()
        self.collect._fetch_thread.join(5)
        self.assertEqual(self.collect.handle('collection.current', {})['status'], 'awaiting_publish')
        self.start_automatic()
        merged = self.collect.handle('collection.merge', {'dryRun': False})
        self.assertEqual(merged['job_id'], manual)
        self.collect._merge_thread.join(5)
        self.assertEqual(self.collect.status(manual)['outcome'], 'succeeded')


if __name__ == '__main__':
    unittest.main()
