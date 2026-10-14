"""Read-only price coverage check through the standard DataBus query."""
from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True,
                        help="project root used to compose the standard DataBus")
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--symbols", default="",
                        help="comma-separated canonical symbols; empty means dataset scope")
    args = parser.parse_args(argv)
    if args.start > args.end:
        raise ValueError("start must not exceed end")

    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from protocal import DataQuery, build_data

    symbols = tuple(item for item in args.symbols.split(",") if item) or None
    fields = ("open", "high", "low", "close", "volume", "amount")
    data = build_data(args.root.resolve())
    result = data.query(DataQuery(
        args.dataset, args.start, args.end, fields, 1, "adjusted",
        symbols=symbols, universe="dataset"))
    rows = sum(len(frame.fields) for record in result.records
               for frame in (record.frame,))
    output = {
        "dataset": result.dataset,
        "start": args.start.isoformat(),
        "end": args.end.isoformat(),
        "calendar": [day.isoformat() for day in result.calendar[:-1]],
        "symbols": list(result.scope.get("symbols", ())),
        "rows": rows,
        "fields": list(result.fields),
        "price_basis": "adjusted",
        "status": "NOT_RELEASED",
    }
    print(json.dumps(output, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
