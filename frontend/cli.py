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
    parser.add_argument("--backtest", type=Path, help="直接读取一个 backtest JSON 配置")
    parser.add_argument("--collect", metavar="MARKET:COMMAND", help="收集：沪深:[2003,2012]、latest、range、auto、check、doctor、status；auto 前台运行，Ctrl+C 停止")
    parser.add_argument("--refresh", action="store_true", help="重新获取并替换指定区间的已有记录")
    for name in ("data", "codes", "fields", "start", "end", "interval"):
        parser.add_argument("--" + name, help={
            "data": "注册的数据类型，多个用逗号分隔；all 表示全部",
            "codes": "证券代码，多个用逗号分隔；省略时使用市场证券目录",
            "fields": "字段，多个用逗号分隔", "start": "起始日期（含）",
            "end": "结束日期（含）", "interval": "auto 间隔，例如 30s、5m、1h、1d，默认 1d",
        }[name])

    return parser


def to_action(args: argparse.Namespace):
    if args.collect is not None:
        values = {name: value for name in ("data", "codes", "fields", "start", "end", "interval")
                  if (value := getattr(args, name, None)) is not None}
        if args.refresh:
            values['refresh'] = True
        values['selector'] = args.collect
        operation = 'cli.collect'
    elif args.backtest is not None:
        values = {"config": str(args.backtest)}
        operation = "cli.backtest"
    else:
        raise ValueError('no command selected: pass --backtest or --collect')
    return protocol.UiAction("cli-1", "invoke", "cli", values, operation)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    values = list(sys.argv[1:] if argv is None else argv)
    if not values:
        parser.print_help()
        return 0
    args = parser.parse_args(values)
    try:
        if args.collect is not None and args.backtest is not None:
            parser.error('--collect cannot be combined with --backtest')
        if args.collect is None and any(getattr(args, name) is not None for name in ('data', 'codes', 'fields', 'start', 'end', 'interval')):
            parser.error('collection arguments require --collect')
        state = protocol.exchange(to_action(args), on_state=print_state) if args.collect is not None else protocol.exchange(to_action(args))
        slot = state.targets["cli"]
        if slot.problems:
            print("；".join(
                f"{issue.code}: {issue.detail or issue.title}" for issue in slot.problems
            ), file=sys.stderr)
            return 2
        output = [message.data for message in slot.messages]
        print(json.dumps(output[0] if len(output) == 1 else output, ensure_ascii=False, allow_nan=False))
        return 2 if any(isinstance(value, dict) and value.get('status') == 'incomplete' for value in output) else 0
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, TypeError, RuntimeError, TimeoutError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


def print_state(state):
    slot = state.targets['cli']
    for message in slot.messages:
        print(json.dumps(message.data, ensure_ascii=False, allow_nan=False), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
