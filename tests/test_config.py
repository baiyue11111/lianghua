import unittest
from config import RunConfig
from protocal import parse_backtest_request
from strategy import load_strategy

class RunConfigTests(unittest.TestCase):
    def request(self):
        return {'run_id':'config','initial_cash':'100000','module':'plugins.strategies.breadth_low_amount_reversal',
                'params':{},'dataset_id':'snapshot','start':'2020-01-01','end':'2020-12-31'}
    def config(self, value=None):
        request = parse_backtest_request(value or self.request())
        return RunConfig.from_request(request, load_strategy(request.strategy_id, dict(request.parameters)))
    def test_hash_is_stable_when_mapping_order_changes(self):
        left=self.config()
        right=self.config(dict(reversed(list(self.request().items()))))
        self.assertEqual(left.config_hash,right.config_hash)
        self.assertEqual(left.initial_cash,100000)
    def test_hash_covers_parameters_and_execution_settings(self):
        raw=self.request(); left=self.config(raw)
        self.assertNotEqual(left.config_hash,self.config({**raw,'params':{'positions':12}}).config_hash)
        self.assertNotEqual(left.config_hash,self.config({**raw,'params':{'positions':4}}).config_hash)
    def test_rejects_wrong_cash_and_bad_parameter(self):
        self.assertEqual(self.config().initial_cash, 100000)
        for change in ({'initial_cash':'-1'},{'initial_cash':'NaN'},
                       {'initial_cash':True},{'params':{'positions':'8'}}):
            with self.assertRaises(ValueError):self.config({**self.request(),**change})
