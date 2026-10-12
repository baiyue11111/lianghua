import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest


class CliValidationGateTests(unittest.TestCase):
    def test_unvalidated_run_stops_before_loading_or_creating_artifacts(self):
        for tier in ('exploratory', 'strict'):
            with self.subTest(tier=tier), TemporaryDirectory() as folder:
                root=Path(folder); output=root/'results'
                result=subprocess.run([
                    sys.executable,'-m','frontend.cli','run',
                    '--data',str(root/'missing-unvalidated-input'),
                    '--output',str(output),'--strategy','breadth_low_amount_reversal',
                    '--research-tier',tier,
                ],capture_output=True,text=True)
                self.assertEqual(result.returncode,2)
                self.assertFalse(output.exists())
                self.assertEqual(result.stdout,'')
                self.assertNotIn('Traceback',result.stderr)


if __name__=='__main__': unittest.main()

class PluginCliTests(unittest.TestCase):
    def test_configured_empty_history_gate_creates_no_output(self):
        import json
        with TemporaryDirectory() as folder:
            root=Path(folder); config=root/'run.json'; output=root/'data/runs'
            config.write_text(json.dumps({'request':{'module':'plugins.strategies.breadth_low_amount_reversal',
                'params':{'positions':12},'initial_cash':100000,'start':'2020-01-01','end':'2020-12-31','dataset_id':''},
                'backtests':{'adapter':'history_data:HistoricalLake','datasets':[]}}))
            result=subprocess.run([sys.executable,'-m','frontend.cli','run','--config',str(config),'--root',str(root)],capture_output=True,text=True)
            self.assertEqual(result.returncode,2)
            self.assertFalse(output.exists())
            self.assertIn('未连接',result.stderr)
            self.assertNotIn('Traceback',result.stderr)
