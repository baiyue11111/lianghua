from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
DISPLAY = ROOT / "frontend" / "display"
WEB = ROOT / "frontend" / "web"


class FrontendLayoutTests(unittest.TestCase):
    def test_web_owns_the_react_application(self):
        self.assertTrue((WEB / "package.json").is_file())
        self.assertTrue((WEB / "src" / "main.tsx").is_file())
        self.assertTrue((WEB / "vite.config.ts").is_file())

    def test_display_owns_framework_independent_interaction_modules(self):
        expected = (
            DISPLAY / "src" / "research" / "port.ts",
            DISPLAY / "src" / "research" / "curves.ts",
            DISPLAY / "src" / "research" / "format.ts",
            DISPLAY / "src" / "research" / "locale.ts",
            DISPLAY / "src" / "research" / "research-model.ts",
            DISPLAY / "src" / "research" / "strategy-model.ts",
            DISPLAY / "src" / "lib" / "closed-loop.ts",
        )
        for path in expected:
            with self.subTest(path=path):
                self.assertTrue(path.is_file())

    def test_entries_have_no_business_channel(self):
        source=(ROOT/"frontend"/"cli.py").read_text()
        for name in ("from application", "from data", "run_backtest", "ingest_exploratory"):
            self.assertNotIn(name,source)
        for path in (WEB/"src").rglob("*.tsx"):
            if "components" in path.parts or "hooks" in path.parts:
                continue
            source=path.read_text()
            self.assertNotRegex(source,r"\bfetch\s*\(")
            self.assertNotIn("/api/",source)
            self.assertNotRegex(source,r"\bsetInterval\s*\(")

    def test_display_contains_no_web_implementation(self):
        forbidden_suffixes = {".tsx", ".css", ".jsx"}
        forbidden_names = {
            "vite.config.ts",
            "tsconfig.app.json",
            "tsconfig.node.json",
            "eslint.config.js",
            "index.html",
        }
        ignored = {"node_modules", "dist", "runtime", ".git"}
        for path in DISPLAY.rglob("*"):
            if not path.is_file():
                continue
            if ignored.intersection(path.relative_to(DISPLAY).parts):
                continue
            with self.subTest(path=path):
                self.assertNotIn(path.name, forbidden_names)
                self.assertNotIn(path.suffix, forbidden_suffixes)
                if path.suffix in {".ts", ".js", ".mjs"}:
                    source = path.read_text(encoding="utf-8")
                    self.assertIsNone(re.search(r"from ['\"]react|require\(['\"]react", source))
                    self.assertNotIn("/api/", source)


if __name__ == "__main__":
    unittest.main()
