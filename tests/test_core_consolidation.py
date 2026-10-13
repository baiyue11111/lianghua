"""Pin the requested physical boundaries, not just a facade over old files."""
import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
OLD = {
    "application", "config", "workbench", "account_allocation",
    "account_execution", "account_ledger", "execution", "execution_risk",
    "fees", "corporate_actions", "distribution_ledger", "dividend_tax",
    "tax_ledger", "adjusted_panel", "panel_features", "features",
}


class CoreConsolidationTests(unittest.TestCase):
    def test_retired_files_are_removed_not_forwarding_shells(self):
        remaining = [name for name in sorted(OLD)
                     if (ROOT / "src" / name).exists()
                     or (ROOT / "src" / f"{name}.py").exists()]
        self.assertEqual(remaining, [])

    def test_runtime_and_tools_have_no_retired_imports(self):
        found = []
        for folder in ("src", "tools"):
            for path in sorted((ROOT / folder).rglob("*.py")):
                for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                    names = []
                    if isinstance(node, ast.Import):
                        names = [alias.name for alias in node.names]
                    elif isinstance(node, ast.ImportFrom) and node.level == 0:
                        names = [node.module or ""]
                    for name in names:
                        if name.split(".")[0] in OLD:
                            found.append((str(path.relative_to(ROOT)), node.lineno, name))
        self.assertEqual(found, [])

    def test_kernel_owns_a_shared_prepare_method(self):
        from core.kernel import Kernel
        self.assertTrue(callable(getattr(Kernel, "prepare", None)))

    def test_moved_history_reader_does_not_bypass_data_port(self):
        path = ROOT / "src" / "collect" / "data.py"
        forbidden = {"read_bytes", "read_text", "write_bytes", "write_text", "open"}
        found = []
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in forbidden:
                    found.append((node.lineno, node.func.attr))
        self.assertEqual(found, [])

    def test_history_assembly_discovers_files_through_data_port(self):
        path = ROOT / "src" / "history_data.py"
        forbidden = {"iterdir", "glob", "rglob", "read_bytes", "read_text"}
        found = []
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in forbidden):
                found.append((node.lineno, node.func.attr))
        self.assertEqual(found, [])

    def test_prepare_does_not_trade_and_run_uses_its_config(self):
        from core.kernel import Kernel
        from protocal import DataQuery
        from tests.test_kernel_search import (
            SearchData, SearchSettlement, SearchStrategy, ScoreCalculation, request,
        )
        SearchSettlement.instances = []
        data = SearchData()
        kernel = Kernel(
            data, lambda _name, parameters: SearchStrategy(parameters),
            lambda req, requirements: DataQuery(
                req.dataset_id, req.start.date(), req.end.date(), requirements.fields,
                requirements.history_bars, requirements.price_basis,
                req.as_of or req.end.date(), requirements.symbols,
                requirements.universe, requirements.frequency,
            ), SearchSettlement, calculation=ScoreCalculation(),
        )
        value = request(parameters={"score": 2})
        preparation = kernel.prepare(value)
        self.assertEqual(SearchSettlement.instances, [])
        self.assertEqual(preparation.effective_config["history"], 1)
        result = kernel.run(value)
        self.assertEqual(result.final_equity, 102)
        self.assertEqual(result.dataset.provenance["config_hash"], preparation.config_hash)
        self.assertEqual(result.dataset.provenance["effective_config"], preparation.effective_config)


if __name__ == "__main__":
    unittest.main()
