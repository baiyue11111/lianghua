"""命令行入口：参数 → 标准动作 → 共用交互核心 → 输出。"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import subprocess
import sys
from typing import Sequence

import importlib
import selectors
import time

protocol = importlib.import_module("frontend.ui-protocol")
CORE = Path(__file__).parent / "display" / "runtime" / "cli.mjs"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lianghua-backtest",
        description="可审计的 A 股研究与回测工具。",
        epilog="示例：lianghua-backtest run --config run.json",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    ingest = commands.add_parser(
        "ingest",
        help="把原始行情、基本面和估值文件整理为研究数据。",
        description="把原始行情、基本面和估值文件整理为研究数据。",
        epilog="示例：lianghua-backtest ingest --price prices.xlsx --fundamentals fundamentals.xlsx --valuations valuations.xlsx --output data/exploratory",
    )
    ingest.add_argument("--price", type=Path, required=True, help="raw_prices_matrix.xlsx or 完整全股池2014-2025.zip")
    ingest.add_argument("--fundamentals", type=Path, required=True)
    ingest.add_argument("--valuations", type=Path, required=True, help="valuation workbook or the complete data zip")
    ingest.add_argument("--output", type=Path, required=True)
    ingest.add_argument("--max-symbols", type=int)
    ingest.add_argument("--price-part", type=int, action="append", help="process only this 0-based zip price part; repeatable")
    run = commands.add_parser(
        "run",
        help="检查输入并运行一个已配置的历史回测。",
        description="检查输入并运行一个已配置的历史回测。",
        epilog=(
            "示例：lianghua-backtest run --config run.json --root .\n"
            "配置必须包含 request 和 backtests.adapter（格式为 module:Class）。"
        ),
    )
    run.add_argument("--config", type=Path, required=True, help="JSON with request and backtests adapter configuration")
    run.add_argument("--root", type=Path, default=Path.cwd(), help="project root for registered snapshot paths")
    report = commands.add_parser(
        "report",
        help="用报告构建器生成可查看的报告文件。",
        description="用报告构建器生成可查看的报告文件。",
        epilog="示例：lianghua-backtest report --artifact run.json --output report.html --builder build-report.mjs",
    )
    report.add_argument("--artifact", type=Path, required=True)
    report.add_argument("--output", type=Path, required=True)
    report.add_argument("--builder", type=Path, required=True)
    report.add_argument("--ready-timeout-ms", type=int, default=20_000)
    report.add_argument("--timeout-ms", type=int, default=30_000)

    return parser


def to_action(args: argparse.Namespace):
    values = {
        name: str(value) if isinstance(value, Path) else value
        for name, value in vars(args).items() if name != "command"
    }
    return protocol.UiAction("cli-1", "invoke", "cli", values, "cli." + args.command)


def exchange(action, backend: protocol.BackendPort | None = None):
    """Run the packaged core. A backend, when supplied, speaks only UiRequest/UiMessage."""
    if not CORE.is_file():
        raise OSError("交互核心发布产物缺失；请运行 npm run build --prefix frontend/display")
    command = ["node", str(CORE)] + (["--connected"] if backend else [])
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1)
    try:
        def write(frame):
            protocol.parse_wire("UiFrame", frame)
            process.stdin.write(json.dumps(frame, ensure_ascii=False, allow_nan=False) + "\n")
            process.stdin.flush()

        write({"kind": "action", "action": action.to_dict()})
        deadline = time.monotonic() + 30
        with selectors.DefaultSelector() as ready:
            ready.register(process.stdout, selectors.EVENT_READ)
            while True:
                if not ready.select(max(0, deadline - time.monotonic())):
                    raise TimeoutError("交互核心响应超时")
                line = process.stdout.readline()
                if not line:
                    raise RuntimeError(process.stderr.read().strip() or "交互核心提前结束")
                frame = protocol.parse_wire("UiFrame", json.loads(line))
                if frame.kind == "state":
                    write({"kind": "close"})
                    return frame.state
                if frame.kind != "request" or frame.request is None or backend is None:
                    raise ValueError("无效桥接消息")
                try:
                    messages = asyncio.run(backend.send(frame.request))
                except Exception as exc:
                    issue = protocol.UiProblem("urn:lianghua:ui:problem", "backend_failed", str(exc))
                    messages = [protocol.UiMessage("failure", "problem", "urn:lianghua:ui:problem",
                                "urn:lianghua:ui:problem:v1", issue.to_dict(),
                                correlation_id=frame.request.request_id)]
                write({"kind": "response", "request": frame.request.to_dict(),
                       "messages": [message.to_dict() for message in messages]})
    finally:
        if process.stdin:
            try:
                process.stdin.close()
            except BrokenPipeError:
                pass  # The child already closed its input; still reap it.
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        process.stdout.close()
        process.stderr.close()


def main(argv: Sequence[str] | None = None, *, backend: protocol.BackendPort | None = None) -> int:
    parser = build_parser()
    values = list(sys.argv[1:] if argv is None else argv)
    if not values:
        parser.print_help()
        return 0
    args = parser.parse_args(values)
    try:
        state = exchange(to_action(args), backend)
        slot = state.targets["cli"]
        if slot.problems:
            print("；".join(issue.title for issue in slot.problems), file=sys.stderr)
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
