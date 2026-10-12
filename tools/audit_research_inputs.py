"""Read-only research-input audit; no performance claims from incomplete data."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import duckdb


def audit(lake: Path) -> dict:
    files = sorted((lake / "daily").glob("*.parquet"))
    if not files:
        raise ValueError("No daily partitions found")
    connection = duckdb.connect()
    connection.read_parquet([str(p) for p in files]).create_view("daily")
    aggregate = connection.sql("""
        SELECT min(date), max(date), count(*), count(DISTINCT code),
               count(close_hfq), count(isST),
               count(*) FILTER (WHERE close<=0 OR open<=0 OR high<low)
        FROM daily
    """).fetchone()
    duplicates = connection.sql("""
        SELECT count(*) FROM (
            SELECT code,date FROM daily GROUP BY code,date HAVING count(*)>1
        )
    """).fetchone()[0]
    invalid = connection.sql("""
        SELECT code,date,open,high,low,close FROM daily
        WHERE close<=0 OR open<=0 OR high<low ORDER BY code,date
    """).fetchall()
    coverage = connection.sql("""
        SELECT substr(date,1,4) AS calendar_year,count(*) AS row_count,count(DISTINCT code) symbols,
               count(close_hfq) adjusted_close_rows,count(isST) st_rows
        FROM daily GROUP BY 1 ORDER BY 1
    """).fetchall()
    # Turnover is RMB and volume is shares in this lake. A 1% tolerance is
    # diagnostic rounding tolerance; it never repairs a source price.
    price_basis = connection.sql("""
        SELECT code,count(*) AS rows,min(date),max(date),
               median(amount/volume/close)
        FROM daily WHERE volume>0 AND amount>0 AND low>0 AND high>0 AND close>0
          AND (amount/volume < low*0.99 OR amount/volume > high*1.01)
        GROUP BY code ORDER BY rows DESC,code
    """).fetchall()
    trade_state = connection.sql("""
        SELECT
          count(*) FILTER (WHERE tradestatus=0 AND (volume>0 OR amount>0)),
          count(*) FILTER (WHERE tradestatus=1 AND
            (coalesce(volume,0)<=0 OR coalesce(amount,0)<=0)),
          count(*) FILTER (WHERE tradestatus IS NULL AND
            coalesce(volume,0)=0 AND coalesce(amount,0)=0)
        FROM daily
    """).fetchone()
    freshness = connection.sql("""
        WITH ends AS (SELECT code,max(date) last_date FROM daily GROUP BY code)
        SELECT max(last_date),count(*) FILTER (WHERE last_date=(SELECT max(last_date) FROM ends)),
          count(*),quantile_disc(last_date,0.5) FROM ends
    """).fetchone()
    hashes = {}
    for path in files:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1048576), b""):
                digest.update(block)
        hashes[path.name] = digest.hexdigest()
    connection.close()
    return {
        "summary": dict(zip(("first_date", "last_date", "rows", "symbols",
                            "adjusted_close_rows", "st_rows", "invalid_price_rows"), aggregate)),
        "duplicate_keys": duplicates,
        "price_basis_issues": [dict(zip(("code", "rows", "first_date", "last_date", "median_vwap_close_ratio"), r)) for r in price_basis],
        "price_basis_tolerance": 0.01,
        "trade_state_issues": dict(zip(("suspended_with_turnover", "trading_without_turnover", "unknown_zero_turnover"), trade_state)),
        "freshness": dict(zip(("latest_date", "latest_stock_count", "tracked_stock_count", "median_stock_date"), freshness)),
        "invalid_prices": invalid,
        "annual_coverage": [dict(zip(("year", "rows", "symbols", "adjusted_close_rows", "st_rows"), r)) for r in coverage],
        "partition_sha256": hashes,
        "adjustment_cache_symbols": len(list((lake / "_adj_cache").glob("*.parquet"))),
        "ranking_status": "NOT_VALIDATED",
        "requirements_before_ranking": [
            "Validate adjustment coverage and factor provenance for every tested stock/date.",
            "Distinguish cash dividends from split-share changes in the execution ledger.",
            "Remove future-known last-bar liquidation assumptions.",
            "Run frozen candidate definitions under the same cost and execution model.",
            "Report all trials and full-period net CAGR/Sharpe before selecting qualified results."
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--lake", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.lake)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result["summary"], ensure_ascii=False))
