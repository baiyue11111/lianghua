import unittest
from types import SimpleNamespace

from protocol import build_effective_config, parse_backtest_request, required_fields
from strategy import load_strategy


class EffectiveConfigTests(unittest.TestCase):
    def request(self):
        return {'run_id':'config','initial_cash':'100000','module':'plugins.strategies.breadth_low_amount_reversal',
                'params':{},'dataset_id':'snapshot','start':'2020-01-01','end':'2020-12-31'}
    def config(self, value=None):
        request = parse_backtest_request(value or self.request())
        strategy = load_strategy(request.strategy_id, dict(request.parameters))
        requirements = strategy.requirements()
        dataset = SimpleNamespace(
            fields=required_fields(requirements.fields),
            history=requirements.history_bars,
            scope={"market": {"selection": {"scope": "CN_A"}}},
        )
        return build_effective_config(
            request, strategy, requirements, dataset,
            {"module": "plugins.data:Settlement", "params": {}},
        )
    def test_hash_is_stable_when_mapping_order_changes(self):
        left, left_hash = self.config()
        right, right_hash = self.config(dict(reversed(list(self.request().items()))))
        self.assertEqual(left_hash, right_hash)
        self.assertEqual(left["initial_cash"], "100000")
        self.assertEqual(left["params"]["positions"], 8)
    def test_hash_covers_parameters_and_execution_settings(self):
        raw=self.request(); _, left_hash=self.config(raw)
        self.assertNotEqual(left_hash,self.config({**raw,'params':{'positions':12}})[1])
        self.assertNotEqual(left_hash,self.config({**raw,'params':{'positions':4}})[1])
    def test_rejects_wrong_cash_and_bad_parameter(self):
        self.assertEqual(self.config()[0]["initial_cash"], "100000")
        for change in ({'initial_cash':'-1'},{'initial_cash':'NaN'},
                       {'initial_cash':True},{'params':{'positions':'8'}}):
            with self.assertRaises(ValueError):self.config({**self.request(),**change})
