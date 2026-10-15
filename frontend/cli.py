"""命令行入口：参数 → 标准动作 → 共用交互核心 → 输出。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

import importlib

protocol = importlib.import_module("frontend.ui-protocol")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lianghua",
        description="可审计的 A 股研究与回测工具。",
        epilog="示例：lianghua --backtest config.json",
    )
    commands = parser.add_subparsers(dest="command")
    ingest = commands.add_parser(
        "ingest",
        help="把原始行情、基本面和估值文件整理为研究数据。",
        description="把原始行情、基本面和估值文件整理为研究数据。",
        epilog="示例：lianghua ingest --price prices.xlsx --fundamentals fundamentals.xlsx --valuations valuations.xlsx --output data/exploratory",
    )
    ingest.add_argument("--price", type=Path, required=True, help="raw_prices_matrix.xlsx or 完整全股池2014-2025.zip")
    ingest.add_argument("--fundamentals", type=Path, required=True)
    ingest.add_argument("--valuations", type=Path, required=True, help="valuation workbook or the complete data zip")
    ingest.add_argument("--output", type=Path, required=True)
    ingest.add_argument("--max-symbols", type=int)
    ingest.add_argument("--price-part", type=int, action="append", help="process only this 0-based zip price part; repeatable")
    report = commands.add_parser(
        "report",
        help="用报告构建器生成可查看的报告文件。",
        description="用报告构建器生成可查看的报告文件。",
        epilog="示例：lianghua report --artifact run.json --output report.html --builder build-report.mjs",
    )
    report.add_argument("--artifact", type=Path, required=True)
    report.add_argument("--output", type=Path, required=True)
    report.add_argument("--builder", type=Path, required=True)
    report.add_argument("--ready-timeout-ms", type=int, default=20_000)
    report.add_argument("--timeout-ms", type=int, default=30_000)
    parser.add_argument("--backtest", type=Path, help="直接读取一个 backtest JSON 配置")

    return parser


def to_action(args: argparse.Namespace):
    if args.backtest is not None:
        values = {"config": str(args.backtest)}
        operation = "cli.backtest"
    else:
        values = {
            name: str(value) if isinstance(value, Path) else value
            for name, value in vars(args).items()
            if name not in {"command", "backtest"} and value is not None
        }
        operation = "cli." + args.command
    return protocol.UiAction("cli-1", "invoke", "cli", values, operation)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    values = list(sys.argv[1:] if argv is None else argv)
    if not values:
        parser.print_help()
        return 0
    args = parser.parse_args(values)
    try:
        if args.backtest is not None and args.command is not None:
            parser.error("--backtest cannot be combined with ingest or report")
        state = protocol.exchange(to_action(args))
        slot = state.targets["cli"]
        if slot.problems:
            print("；".join(
                f"{issue.code}: {issue.detail or issue.title}" for issue in slot.problems
            ), file=sys.stderr)
            return 2
        output = [message.data for message in slot.messages]
        print(json.dumps(output[0] if len(output) == 1 else output, ensure_ascii=False, allow_nan=False))
        return 0
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, TypeError, RuntimeError, TimeoutError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
