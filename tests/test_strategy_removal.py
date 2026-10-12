"""A removed strategy must not remain runnable through Python's import cache."""
import importlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from strategy import catalog, load_strategy


class StrategyRemovalTests(unittest.TestCase):
    def test_removed_cached_plugin_disappears_and_cannot_load(self):
        package=importlib.import_module('plugins.strategies')
        importlib.import_module('plugins.strategies.moving_average_cross')
        name='plugins.strategies.removal_probe'
        try:
            with tempfile.TemporaryDirectory() as temp:
                source=Path(temp)/'removal_probe.py'
                source.write_text('from plugins.strategies.moving_average_cross import NAME, Params, Strategy\n')
                with patch.object(package,'__path__',[temp]):
                    self.assertEqual(load_strategy(name,{}).strategy_id,'moving_average_cross')
                    self.assertEqual([item['module'] for item in catalog()],[name])
                    source.unlink()
                    self.assertEqual(catalog(),[])
                    with self.assertRaises(ValueError):
                        load_strategy(name,{})
        finally:
            sys.modules.pop(name,None)
