"""Keep collection and persistence from spreading back into other modules."""
import ast
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]


class CollectionBoundaryTests(unittest.TestCase):
    def test_external_collection_libraries_stay_in_collect(self):
        paths = list(ROOT.glob('*.py'))
        for folder in ('src', 'tools', 'experiments', 'frontend', 'xinwenlianbo'):
            paths.extend((ROOT / folder).rglob('*.py'))
        violations = []
        libraries = {'requests', 'httpx', 'baostock', 'akshare', 'urllib.request'}
        for path in paths:
            if path.is_relative_to(ROOT / 'src/collect'):
                continue
            for node in ast.walk(ast.parse(path.read_text())):
                names = []
                if isinstance(node, ast.Import):
                    names = [item.name for item in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module]
                if any(name in libraries for name in names):
                    violations.append(f'{path.relative_to(ROOT)}:{node.lineno}')
        self.assertEqual(violations, [])

    def test_collect_does_not_open_private_persistent_files(self):
        forbidden = {'open', 'write_text', 'write_bytes', 'mkdir', 'makedirs', 'replace', 'remove', 'unlink', 'connect'}
        violations = []
        for path in (ROOT / 'src/collect').glob('*.py'):
            for node in ast.walk(ast.parse(path.read_text())):
                if not isinstance(node, ast.Call):
                    continue
                name = node.func.id if isinstance(node.func, ast.Name) else None
                if isinstance(node.func, ast.Attribute):
                    name = node.func.attr
                    # String normalization is not os.replace.
                    if name in {'replace', 'remove'} and not (isinstance(node.func.value, ast.Name) and node.func.value.id == 'os'):
                        continue
                    if name == 'connect' and isinstance(node.func.value, ast.Name) and node.func.value.id == 'duckdb' and not node.args and not node.keywords:
                        continue
                if name in forbidden:
                    violations.append(f'{path.name}:{node.lineno}:{name}')
        self.assertEqual(violations, [])

    def test_core_import_does_not_load_frontend_storage_or_providers(self):
        code = "import sys; from collect import core; assert not any(name in sys.modules for name in ('frontend.ui-protocol','storage','baostock','requests','collect.quotes'))"
        result = subprocess.run([sys.executable, '-c', code], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
