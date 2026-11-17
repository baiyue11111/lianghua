#!/usr/bin/env python3
"""Static G1/G2 guardrail check for lianghua strategy files and configs.

Two engines, both conservative by design (report suspicious lines, never claim proof):

  1. regex fast scan — catches direct `account.equity` / `account.cash` / NAV-track
     names regardless of Python syntax validity. Kept for backward compatibility.
  2. AST taint scan — parses the file, propagates a NAV/perf taint from source
     (`account.equity`, `account.cash`, NAV-track attributes; and through aliases
     of the `account` object) into locals, and reports when it reaches a
     weight/exposure sink. This is what regex cannot do: aliasing and
     line-independent structure. Findings are ADDITIVE ONLY: a clean AST result
     never downgrades a regex hit (see exp_track_a2.py for why — taint hidden in
     a list/dict member is missed by the AST but caught by regex).

Usage:
    python check_strategy.py src/plugins/strategies/exp_qf_h8f1.py
    python check_strategy.py src/plugins/strategies/*.py --config exp_qf_h8f1.json
    python check_strategy.py src/plugins/strategies/ --strict
    python check_strategy.py file.py --no-ast        # legacy regex-only

Exit code 0 = no high-severity violations, 1 = G1 violation (or any warning
with --strict), 2 = bad input.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

# (rule id, regex, severity, message)
SOURCE_RULES = [
    ("G1-NAV-EQUITY", re.compile(r"account\s*\.\s*equity"), "high",
     "reads realized account equity — must not feed weights / slots / exposure"),
    ("G1-NAV-CASH", re.compile(r"account\s*\.\s*cash"), "high",
     "reads account cash — must not feed weights / slots / exposure"),
    ("G1-NAV-TRACK", re.compile(r"nav_values|nav_dates|_dilution\b|equity_W_ago|track_slope"), "high",
     "maintains a NAV track / dilution mechanism — see case-r29.md"),
    ("G1-DILUTE-PARAM", re.compile(r"\bdilute_positions\b"), "medium",
     "dilution slot parameter present; verify it equals max_positions (disabled)"),
    ("G1-PERF-FEEDBACK", re.compile(r"drawdown|realized_pnl|profit_ratio|equity_ratio"), "medium",
     "possible performance feedback into alpha decisions"),
]

VALIDATION_HINTS = ("isfinite(", "raise ", "equity <= 0", "equity > 0")
DEFAULT_INT = re.compile(r"^\s*(\w+)\s*:\s*int\s*=\s*field\(\s*default\s*=\s*(\d+)", re.M)

# AST taint vocabulary.
NAV_SOURCE_ATTRS = frozenset({"equity", "cash"})
NAV_TRACK_NAMES = frozenset({"nav_values", "nav_dates", "equity_W_ago", "track_slope", "track_band"})
VALIDATORS = frozenset({"isfinite", "isnan", "isinf", "abs"})
WEIGHT_SINK_FUNCS = frozenset({"TargetSet", "equal_weights", "weights"})
WEIGHT_SINK_PARAMS = frozenset({"weights", "weight", "exposure", "positions", "n_positions", "slots"})


# ---------------------------------------------------------------- regex engine
def is_validation_only(line: str) -> bool:
    return any(hint in line for hint in VALIDATION_HINTS)


def check_source_regex(path: Path) -> list[tuple[str, str, int, str, str]]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return [("high", "IO-ERROR", 0, "", str(exc))]

    lines = text.splitlines()
    track_rule = next(rule for rid, rule, _, _ in SOURCE_RULES if rid == "G1-NAV-TRACK")
    has_track = any(track_rule.search(line) for line in lines)
    findings: list[tuple[str, str, int, str, str]] = []
    for rid, pattern, severity, message in SOURCE_RULES:
        for num, line in enumerate(lines, 1):
            if not pattern.search(line):
                continue
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith('"""'):
                continue
            if rid == "G1-NAV-EQUITY" and is_validation_only(line) and not has_track:
                continue  # pure finiteness guard, no NAV track in the file
            findings.append((severity, rid, num, stripped[:100], message))
    return findings


def check_params(path: Path) -> list[tuple[str, str, int, str, str]]:
    text = path.read_text(encoding="utf-8")
    defaults = {name: int(value) for name, value in DEFAULT_INT.findall(text)}
    dilute, maximum = defaults.get("dilute_positions"), defaults.get("max_positions")
    if dilute is None or maximum is None:
        return []
    if dilute > maximum:
        return [("high", "G1-DILUTION-ACTIVE", 0, f"dilute_positions={dilute} > max_positions={maximum}",
                 "NAV-driven slot amplification is enabled by default; see case-r29.md")]
    return [("info", "G1-DILUTION-OFF", 0, f"dilute_positions={dilute} == max_positions={maximum}",
             "slot amplification disabled in defaults")]


# ------------------------------------------------------------------ AST engine
def _target_names(target: ast.AST) -> set[str]:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, ast.Attribute):
        base = target.value.id if isinstance(target.value, ast.Name) else ""
        return {f"{base}.{target.attr}"}
    if isinstance(target, (ast.Tuple, ast.List)):
        out: set[str] = set()
        for elt in target.elts:
            out |= _target_names(elt)
        return out
    return set()


class TaintCtx:
    """Per-function taint state: numeric taint + aliases of the account object."""

    def __init__(self, fn: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.tainted: set[str] = set()
        self.accounts: set[str] = set()
        for arg in list(fn.args.args) + list(fn.args.kwonlyargs):
            ann = getattr(arg, "annotation", None)
            is_account = arg.arg == "account" or (
                ann is not None and "AccountSnapshot" in ast.dump(ann))
            if is_account:
                self.accounts.add(arg.arg)

    def is_source(self, node: ast.AST) -> bool:
        if isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name) and node.value.id in self.accounts:
                return node.attr in NAV_SOURCE_ATTRS
            if node.attr in NAV_TRACK_NAMES:
                return True
        return False

    def is_tainted(self, node: ast.AST | None) -> bool:
        if node is None:
            return False
        if self.is_source(node):
            return True
        if isinstance(node, ast.Constant):
            return False
        if isinstance(node, ast.Name):
            return node.id in self.tainted
        if isinstance(node, (ast.Attribute, ast.Subscript)):
            return self.is_tainted(node.value)
        if isinstance(node, ast.BinOp):
            return self.is_tainted(node.left) or self.is_tainted(node.right)
        if isinstance(node, ast.UnaryOp):
            return self.is_tainted(node.operand)
        if isinstance(node, ast.Compare):
            return self.is_tainted(node.left) or any(self.is_tainted(c) for c in node.comparators)
        if isinstance(node, ast.BoolOp):
            return any(self.is_tainted(v) for v in node.values)
        if isinstance(node, ast.IfExp):
            return any(self.is_tainted(v) for v in (node.test, node.body, node.orelse))
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            return any(self.is_tainted(e) for e in node.elts)
        if isinstance(node, ast.Dict):
            return any(self.is_tainted(v) for v in node.values)
        if isinstance(node, ast.Call):
            func = node.func
            fname = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if fname in VALIDATORS:
                return False  # a validator yields a bool, not a signal
            if self.is_tainted(func):
                return True
            return any(self.is_tainted(a) for a in node.args) or any(
                self.is_tainted(k.value) for k in node.keywords)
        if isinstance(node, ast.Lambda):
            return self.is_tainted(node.body)
        if isinstance(node, ast.Await):
            return self.is_tainted(node.value)
        return False

    def absorb(self, targets: list[ast.AST], value: ast.AST | None) -> None:
        if value is None:
            return
        names: set[str] = set()
        for t in targets:
            names |= _target_names(t)
        if isinstance(value, ast.Name) and value.id in self.accounts:
            self.accounts |= names  # `acct = account` keeps the source identity
        if self.is_tainted(value):
            self.tainted |= names


def _is_sink_call(node: ast.Call) -> bool:
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    if name in WEIGHT_SINK_FUNCS:
        return True
    return any(k.arg in WEIGHT_SINK_PARAMS for k in node.keywords)


FINDING = tuple[str, str, int, str, str]


def _walk_function(fn: ast.FunctionDef | ast.AsyncFunctionDef, lines: list[str],
                   findings: list[FINDING]) -> None:
    ctx = TaintCtx(fn)
    is_generate = fn.name == "generate"

    def record(rid: str, node: ast.AST, message: str, severity: str = "high") -> None:
        num = getattr(node, "lineno", 0)
        src = lines[num - 1].strip()[:100] if 0 < num <= len(lines) else ""
        findings.append((severity, rid, num, src, message))

    def scan_sinks(stmt: ast.stmt) -> None:
        for sub in ast.walk(stmt):
            if isinstance(sub, ast.Call) and _is_sink_call(sub):
                if any(ctx.is_tainted(a) for a in sub.args) or any(
                        ctx.is_tainted(k.value) for k in sub.keywords):
                    record("G1-TAINT-SINK", sub, "NAV/perf-derived value reaches a weight/exposure sink")

    def visit(stmts: list[ast.stmt]) -> None:
        for st in stmts:
            if isinstance(st, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = list(st.targets) if isinstance(st, ast.Assign) else [st.target]
                ctx.absorb(targets, st.value)
                scan_sinks(st)
                continue
            if isinstance(st, ast.Return) and st.value is not None:
                if is_generate and ctx.is_tainted(st.value):
                    record("G1-TAINT-RETURN", st,
                           "generate() returns a NAV/perf-derived value — must not drive weights")
                scan_sinks(st)
                continue
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue  # scanned in its own walk
            for attr in ("body", "orelse", "finalbody"):
                block = getattr(st, attr, None)
                if isinstance(block, list) and block and isinstance(block[0], ast.stmt):
                    visit(block)
            for handler in getattr(st, "handlers", []) or []:
                visit(handler.body)

    visit(fn.body)


def _parse(path: Path) -> tuple[ast.Module | None, list[str], FINDING | None]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return None, [], ("high", "IO-ERROR", 0, "", str(exc))
    try:
        return ast.parse(text, filename=str(path)), text.splitlines(), None
    except SyntaxError as exc:
        # Do not go silent on unparseable files — same policy as lookahead-lint.
        return None, [], ("medium", "AST-PARSE-ERROR", exc.lineno or 0,
                          (exc.text or "").strip()[:100], f"could not AST-parse: {exc.msg}")


def ast_findings(path: Path) -> list[FINDING]:
    tree, lines, err = _parse(path)
    if err:
        return [err] if err[1] != "IO-ERROR" else []  # IO already reported by regex
    assert tree is not None
    out: list[FINDING] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _walk_function(node, lines, out)
    order = {"high": 0, "medium": 1, "info": 2}
    best: dict[tuple[str, int], FINDING] = {}
    for f in out:
        key = (f[1], f[2])
        if key not in best or order[f[0]] < order[best[key][0]]:
            best[key] = f
    return sorted(best.values(), key=lambda f: (f[2], order[f[0]]))


# NOTE: no "AST proves clean" downgrade exists on purpose. The taint engine does
# not follow container members (list.append, dict item) or cross-function state,
# so absence of a G1-TAINT finding is NOT evidence of safety. Real case:
# exp_track_a2.py routes account.equity -> nav -> self.state["returns"] ->
# exposure -> weights, which the taint engine misses but regex catches. An
# absence of evidence must never weaken a regex hit — heuristics only ADD.


# ------------------------------------------------------------------------ CLI
def check_config(path: Path) -> list[tuple[str, str, int, str, str]]:
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [("high", "IO-ERROR", 0, "", str(exc))]
    params = cfg.get("params") or {}
    findings: list[tuple[str, str, int, str, str]] = []
    dilute, maximum = params.get("dilute_positions"), params.get("max_positions")
    if dilute is not None and maximum is not None and dilute > maximum:
        findings.append(("high", "G1-DILUTION-ACTIVE", 0,
                         f"params.dilute_positions={dilute} > max_positions={maximum}",
                         "disable it in config before accepting any scale comparison"))
    cash, principal = cfg.get("initial_cash"), params.get("principal")
    if cash is not None and principal is not None and str(cash) != str(int(principal)):
        findings.append(("medium", "G2-CASH-MISMATCH", 0,
                         f"initial_cash={cash}, params.principal={principal}",
                         "keep both fields identical when deriving capital tiers"))
    return findings


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("targets", nargs="*", help="strategy .py files / directories, or config .json files")
    ap.add_argument("--config", action="append", default=[], help="additional config JSON to check")
    ap.add_argument("--strict", action="store_true", help="fail on medium-severity warnings too")
    ap.add_argument("--no-ast", action="store_true", help="legacy regex-only scan (for A/B comparison)")
    args = ap.parse_args()

    paths: list[Path] = []
    for raw in list(args.targets) + list(args.config):
        p = Path(raw)
        if p.is_dir():
            paths.extend(sorted(p.glob("*.py")))
        elif p.exists():
            paths.append(p)
        else:
            print(f"[MISSING] {p}")
            return 2
    if not paths:
        print("no files to check")
        return 2

    all_findings: list[tuple[Path, FINDING]] = []
    for path in paths:
        if path.suffix == ".py":
            found = check_source_regex(path)
            if not args.no_ast:
                found += ast_findings(path)  # additive only; never downgrades regex
            found += check_params(path)
        elif path.suffix == ".json":
            found = check_config(path)
        else:
            continue
        seen: set[tuple[str, int]] = set()
        for f in found:
            key = (f[1], f[2])
            if key in seen:
                continue
            seen.add(key)
            all_findings.append((path, f))

    order = {"high": 0, "medium": 1, "info": 2}
    all_findings.sort(key=lambda pair: (order[pair[1][0]], str(pair[0]), pair[1][2]))
    for path, (severity, rid, num, line, message) in all_findings:
        loc = f"{path}:{num}" if num else str(path)
        extra = f" | {line}" if line else ""
        print(f"[{severity.upper():6}] {rid:22} {loc}{extra}\n             {message}")

    high = sum(1 for _, f in all_findings if f[0] == "high")
    medium = sum(1 for _, f in all_findings if f[0] == "medium")
    engine = "regex-only" if args.no_ast else "regex+AST-taint"
    print(f"\nchecked {len(paths)} file(s) [{engine}]: {high} high, {medium} medium finding(s)")
    if high:
        print("VERDICT: FAIL — G1/G2 violation; fix before ranking or reporting this strategy")
        return 1
    if args.strict and medium:
        print("VERDICT: FAIL (--strict) — medium warnings present")
        return 1
    print("VERDICT: PASS — no NAV-feedback violation detected (still run scale-invariance check)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
