import unittest
from config import RunConfig

class RunConfigTests(unittest.TestCase):
    def request(self):
        return {'initial_cash':100000,'module':'plugins.strategies.breadth_low_amount_reversal',
                'params':{},'dataset_id':'snapshot','start':'2020-01-01','end':'2020-12-31'}
    def test_hash_is_stable_when_mapping_order_changes(self):
        left=RunConfig.from_mapping(self.request())
        right=RunConfig.from_mapping(dict(reversed(list(self.request().items()))))
        self.assertEqual(left.config_hash,right.config_hash)
        self.assertEqual(left.initial_cash,100000)
    def test_hash_covers_parameters_and_execution_settings(self):
        raw=self.request(); left=RunConfig.from_mapping(raw)
        self.assertNotEqual(left.config_hash,RunConfig.from_mapping({**raw,'params':{'positions':12}}).config_hash)
        self.assertNotEqual(left.config_hash,RunConfig.from_mapping({**raw,'execution':{'lot_size':200}}).config_hash)
    def test_rejects_wrong_cash_and_bad_parameter(self):
        for change in ({'initial_cash':'100000'},{'initial_cash':True},{'params':{'positions':'8'}}):
            with self.assertRaises(ValueError):RunConfig.from_mapping({**self.request(),**change})
