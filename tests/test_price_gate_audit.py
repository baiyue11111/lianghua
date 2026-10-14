from pathlib import Path
import tempfile
import unittest

from tools import price_gate_audit


class PriceGateAuditTests(unittest.TestCase):
    def test_actual_loader_errors_and_master_gaps_are_retained_in_repeatable_readonly_report(self):
        with tempfile.TemporaryDirectory() as directory:
            from tests.history_fixture import make_snapshot
            root = Path(directory)
            make_snapshot(root)
            self.assertEqual(price_gate_audit.main([
                '--root', str(root), '--dataset', 'fixture',
                '--start', '2020-01-31', '--end', '2020-02-04']), 0)
