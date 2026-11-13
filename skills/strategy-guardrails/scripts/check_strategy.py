#!/usr/bin/env python3
"""Static G1/G2 guardrail check for lianghua strategy files and configs.

Usage:
    python check_strategy.py src/plugins/strategies/exp_qf_h8f1.py
    python check_strategy.py src/plugins/strategies/*.py --config exp_qf_h8f1.json
    python check_strategy.py src/plugins/strategies/ --strict

Exit code 0 = no high-severity violations, 1 = G1 violation (or any warning
with --strict), 2 = bad input. The check is deliberately conservative: it
reports suspicious lines instead of trying to prove intent.
"""
from __future__ import annotations

import argparse
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


def is_validation_only(line: str) -> bool:
    return any(hint in line for hint in VALIDATION_HINTS)


def check_source(path: Path) -> list[tuple[str, str, int, str, str]]:
    """Return (severity, rule, line_no, source_line, message) tuples."""
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
    """Detect dilution slot amplification in a Params dataclass."""
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

    all_findings: list[tuple[Path, tuple[str, str, int, str, str]]] = []
    for path in paths:
        if path.suffix == ".py":
            found = check_source(path) + check_params(path)
        elif path.suffix == ".json":
            found = check_config(path)
        else:
            continue
        all_findings.extend((path, f) for f in found)

    order = {"high": 0, "medium": 1, "info": 2}
    all_findings.sort(key=lambda pair: (order[pair[1][0]], str(pair[0]), pair[1][2]))
    for path, (severity, rid, num, line, message) in all_findings:
        loc = f"{path}:{num}" if num else str(path)
        extra = f" | {line}" if line else ""
        print(f"[{severity.upper():6}] {rid:22} {loc}{extra}\n             {message}")

    high = sum(1 for _, f in all_findings if f[0] == "high")
    medium = sum(1 for _, f in all_findings if f[0] == "medium")
    print(f"\nchecked {len(paths)} file(s): {high} high, {medium} medium finding(s)")
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
