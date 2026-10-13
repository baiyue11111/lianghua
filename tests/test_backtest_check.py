"""Hand-computed audit cases for the independent result checker."""
from pathlib import Path
import importlib.util
import unittest


class BacktestCheckTests(unittest.TestCase):
    def checker(self):
        path=Path(__file__).parents[1]/'tools/check_backtest.py'
        self.assertTrue(path.exists(),'independent account checker is required')
        spec=importlib.util.spec_from_file_location('check_backtest',path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        return module

    def trace(self):
        return {'fills':[{'status':'filled','symbol':'x','side':'buy','trade_date':'2024-01-03','shares':100,
                    'gross_amount':200,'commission':5,'stamp_duty':0}],
                'cash_events':[], 'holdings':[['2024-01-02',[]],['2024-01-03',[{'symbol':'x','shares':100}]]],
                'equity':[{'day':'2024-01-02','cash':'1000','market_value':'0','receivables':'0','liabilities':'0','equity':'1000'},
                          {'day':'2024-01-03','cash':'795','market_value':'300','receivables':'0','liabilities':'0','equity':'1095'}]}

    def test_recomputes_cash_and_marks_from_fills_and_source_prices(self):
        checker=self.checker();trace=self.trace()
        result=checker.account_check(trace,{('x','2024-01-03'):3},1000)
        self.assertEqual(result['checked_sessions'],2)
        self.assertEqual(result['maximum_error'],0.)

    def test_rejects_wrong_cash_or_valuation(self):
        checker=self.checker()
        for key in ['cash','market_value','equity']:
            trace=self.trace();trace['equity'][1][key]='999'
            with self.assertRaises(AssertionError):checker.account_check(trace,{('x','2024-01-03'):3},1000)

    def test_metrics_use_hand_calculated_drawdown_and_return(self):
        result=self.checker().metrics_check([100,120,90],{'total_return':-.1,'max_drawdown':-.25})
        self.assertAlmostEqual(result['total_return'],-.1)
        self.assertAlmostEqual(result['max_drawdown'],-.25)
