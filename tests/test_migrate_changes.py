import json
import tempfile
import unittest
from pathlib import Path

from market import build_data, table_name
from collect.methods import Methods
from protocol import DataError, TableDef
from tools.migrate_changes import migrate

KEYS = ('exchange', 'effective_date', 'old_code', 'new_code')
EVENT = {'exchange': 'SZ', 'effective_date': '2019-12-16', 'old_code': '000043',
         'new_code': '001914', 'announcement_date': '2019-12-16',
         'factor_basis_continuity_verified': True,
         'factor_basis_evidence': {'back_factor': 9.5, 'method': 'proof'},
         'holder_share_ratio': 1, 'holding_period_continues': True,
         'source_url': 'https://example.com/proof.pdf'}

class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = build_data(Path(self.tmp.name))
        self.addCleanup(self.data.close)
        self.data.update_registry(lambda registry: {**registry, 'markets': [
            {'scope': scope, 'collect': {'default': ['custom'],
                                        'data': {'custom': {'source': 'custom'}}}}
            for scope in ('CN_A', 'CN_A_SH_SZ')]})
        self.data.define(TableDef('bars', {'code': 'VARCHAR', 'date': 'VARCHAR'}, ('code', 'date')))
        self.old = self.data.save_config('old_changes', {'schema': 1, 'events': [EVENT]})
        self.manifest = {'schema': 3, 'scope': {'universe': 'test', 'symbols': ['000043']},
                         'fields': {}, 'inputs': {'bars': 'lake:bars', 'code_changes': self.old}}
        self.data.save_manifest('cn_a', self.manifest)

    def test_preserves_and_binds_events_idempotently(self):
        migrate(self.data, 'cn_a', 'security_changes')
        ref = self.data.source('security_changes')
        registry = self.data.registry()
        self.assertEqual(len([s for s in registry['sources'] if s['id'] == 'stock_adjust']), 1)
        self.assertTrue(all('custom' in m['collect']['default'] and
                            'stock_adjust' in m['collect']['default'] for m in registry['markets']))
        self.assertEqual(self.data.manifest('cn_a')['inputs']['code_changes'], 'changes:' + ref[5:])
        event = self.data.object('changes:' + ref[5:])['events'][0]
        self.assertEqual(json.loads(event['factor_basis_evidence']), EVENT['factor_basis_evidence'])
        self.assertTrue(event['factor_basis_continuity_verified'])
        migrate(self.data, 'cn_a', 'security_changes')
        self.assertEqual(len(self.data.object('changes:' + ref[5:])['events']), 1)

    def test_conflict_rolls_back_manifest_and_binding(self):
        cols = {key: 'VARCHAR' for key in KEYS}
        cols['source_url'] = 'VARCHAR'
        name = table_name('security_changes/records')
        self.data.define(TableDef(name, cols, KEYS))
        self.data.bind('security_changes', 'records', 'lake:' + name)
        self.data.put_rows(name, [{key: EVENT[key] for key in KEYS} | {'source_url': 'https://wrong'}], keys=KEYS)
        with self.assertRaises(ValueError):
            migrate(self.data, 'cn_a', 'security_changes')
        self.assertEqual(self.data.manifest('cn_a')['inputs']['code_changes'], self.old)
        self.assertEqual(self.data.table('lake:' + name).to_pylist()[0]['source_url'], 'https://wrong')
        self.assertEqual(self.data.registry()['sources'], [])

    def test_conflicting_registry_rolls_back_event_migration(self):
        self.data.update_registry(lambda registry: {**registry, 'sources': [
            {'id': 'stock_adjust', 'name': 'different'}]})
        with self.assertRaisesRegex(ValueError, 'conflicting source'):
            migrate(self.data, 'cn_a', 'security_changes')
        self.assertEqual(self.data.manifest('cn_a')['inputs']['code_changes'], self.old)
        with self.assertRaises(DataError):
            self.data.source('security_changes')

    def test_normal_collection_appends_to_migrated_table(self):
        migrate(self.data, 'cn_a', 'security_changes')
        source = {'id': 'security_changes', 'method': 'fixture',
                  'params': {'key_fields': list(KEYS)}}
        event = {**EVENT, 'old_code': '000044', 'new_code': '001915',
                 'effective_date': '2020-01-01', 'factor_basis_evidence': None}
        packet = {'records': [event], 'raw': [], 'scope': {}, 'request': {}}
        Methods(self.data).publish(source, packet)
        ref = self.data.manifest('cn_a')['inputs']['code_changes']
        self.assertEqual(len(self.data.object(ref)['events']), 2)

    def test_ambiguous_change_graph_rolls_back(self):
        self.data.save_config('old_changes', {'schema': 1, 'events': [EVENT,
            {**EVENT, 'new_code': '009999'}]})
        with self.assertRaisesRegex(ValueError, 'conflicting code-change graph'):
            migrate(self.data, 'cn_a', 'security_changes')
        self.assertEqual(self.data.manifest('cn_a')['inputs']['code_changes'], self.old)
        self.assertEqual(self.data.registry()['sources'], [])

if __name__ == '__main__': unittest.main()
