#!/usr/bin/env python3
"""Build the deterministic research universe from the local data lake.

This is an input manifest builder, not strategy logic.  It never accesses the
network: the master stock table defines the full universe and daily partitions
only establish which master codes have local daily data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Iterable

import duckdb
import pandas as pd


SUPPORTED_EXCHANGES = frozenset({"SH", "SZ"})
MASTER_COLUMNS = frozenset({"code", "name", "exchange"})
DAILY_COLUMNS = frozenset({"code", "date"})


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_columns(frame: pd.DataFrame, required: Iterable[str], path: Path) -> None:
    missing = sorted(set(required) - set(frame.columns))
    if missing:
        raise ValueError(f"{path} 缺少列: {', '.join(missing)}")


def _normalise_code(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def _duplicates(frame: pd.DataFrame, keys: list[str]) -> list[dict[str, object]]:
    counts = frame.groupby(keys, dropna=False, sort=True).size()
    result = []
    for key, count in counts.items():
        if count <= 1:
            continue
        values = key if isinstance(key, tuple) else (key,)
        result.append({**dict(zip(keys, (str(v) for v in values))), "count": int(count)})
    return result


def _daily_relation(paths: list[Path]) -> str:
    quoted = ", ".join("'" + str(path).replace("'", "''") + "'" for path in paths)
    return f"read_parquet([{quoted}])"


def build_universe(lake: Path, output: Path) -> dict[str, object]:
    master_path = lake / "stock_info.parquet"
    daily_dir = lake / "daily"
    if not master_path.is_file():
        raise FileNotFoundError(master_path)
    if not daily_dir.is_dir():
        raise FileNotFoundError(daily_dir)

    master = pd.read_parquet(master_path)
    _require_columns(master, MASTER_COLUMNS, master_path)
    master = master[["code", "name", "exchange"]].copy()
    master["code"] = master["code"].map(_normalise_code)
    master["name"] = master["name"].fillna("").astype(str)
    master["exchange"] = master["exchange"].fillna("").astype(str).str.strip().str.upper()
    if (master["code"] == "").any():
        raise ValueError("stock_info.parquet 包含空 code")

    daily_files = sorted(daily_dir.glob("*.parquet"))
    daily_file_manifest = []
    for path in daily_files:
        _require_columns(pd.read_parquet(path, columns=list(DAILY_COLUMNS)), DAILY_COLUMNS, path)
        daily_file_manifest.append({"path": str(path.relative_to(lake)), "sha256": sha256_file(path)})

    daily_codes: set[str] = set()
    daily_duplicates: list[dict[str, object]] = []
    if daily_files:
        relation = _daily_relation(daily_files)
        with duckdb.connect() as connection:
            daily_codes = {
                _normalise_code(row[0])
                for row in connection.execute(f"SELECT DISTINCT code FROM {relation} WHERE code IS NOT NULL").fetchall()
            }
            daily_duplicates = [
                {"code": str(code), "date": str(day), "count": int(count)}
                for code, day, count in connection.execute(
                    f"SELECT code, date, COUNT(*) AS count FROM {relation} "
                    "GROUP BY code, date HAVING COUNT(*) > 1 ORDER BY code, date"
                ).fetchall()
            ]

    master_duplicates = _duplicates(master, ["code"])
    valid = not master_duplicates
    master_by_code = master.drop_duplicates("code", keep="first").set_index("code")
    master_codes = sorted(master_by_code.index)
    missing_codes = sorted(set(master_codes) - daily_codes)
    missing_daily = [
        {"code": code, "name": str(master_by_code.at[code, "name"]), "exchange": str(master_by_code.at[code, "exchange"])}
        for code in missing_codes
    ]

    exchange_coverage: dict[str, dict[str, int]] = {}
    for exchange in sorted(master_by_code["exchange"].unique()):
        exchange_codes = {code for code in master_codes if master_by_code.at[code, "exchange"] == exchange}
        exchange_coverage[exchange] = {
            "master_count": len(exchange_codes),
            "daily_count": len(exchange_codes & daily_codes),
            "missing_daily_count": len(exchange_codes - daily_codes),
        }

    supported_codes = [code for code in master_codes if master_by_code.at[code, "exchange"] in SUPPORTED_EXCHANGES] if valid else []
    unsupported_codes = [code for code in master_codes if master_by_code.at[code, "exchange"] not in SUPPORTED_EXCHANGES] if valid else []
    output.mkdir(parents=True, exist_ok=True)
    for code_file, codes in (("supported_codes.txt", supported_codes), ("unsupported_codes.txt", unsupported_codes)):
        path = output / code_file
        if valid:
            path.write_text("\n".join(codes) + "\n", encoding="utf-8")
        elif path.exists():
            path.unlink()

    manifest = {
        "schema_version": 1,
        "valid": valid,
        "master": {"path": str(master_path.relative_to(lake)), "sha256": sha256_file(master_path), "count": len(master), "unique_count": len(master_codes)},
        "daily": {"files": daily_file_manifest, "unique_code_count": len(daily_codes)},
        "master_codes": master_codes,
        "duplicate_master_keys": master_duplicates,
        "duplicate_daily_keys": daily_duplicates,
        "missing_daily": missing_daily,
        "exchange_coverage": exchange_coverage,
        "supported": {"exchanges": sorted(SUPPORTED_EXCHANGES), "codes": supported_codes, "count": len(supported_codes)},
        "unsupported": {"codes": unsupported_codes, "count": len(unsupported_codes)},
    }
    (output / "universe.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lake", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = build_universe(args.lake, args.output)
    print(json.dumps({
        "master_count": manifest["master"]["count"],
        "daily_unique_code_count": manifest["daily"]["unique_code_count"],
        "supported_count": manifest["supported"]["count"],
        "unsupported_count": manifest["unsupported"]["count"],
        "missing_daily_count": len(manifest["missing_daily"]),
    }, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
