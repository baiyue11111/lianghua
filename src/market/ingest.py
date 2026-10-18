"""Exploratory XLSX-to-Parquet adapter with immutable source evidence."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from hashlib import sha256
import json
from pathlib import Path
from io import BytesIO
from tempfile import TemporaryDirectory
from typing import Any
from zipfile import ZipFile

import pyarrow as pa
import pyarrow.parquet as pq

from .xlsx_matrix import iter_sheet_rows


PRICE_FIELDS = ("open", "close", "high", "low", "volume", "amount")
PRICE_SHEETS = dict(zip(PRICE_FIELDS, ("开盘价", "收盘价", "最高价", "最低价", "成交量", "成交额"), strict=True))
FUNDAMENTAL_SHEETS = {
    "net_income_ttm": "归母净利润(TTM)",
    "net_assets": "净资产",
    "roe": "ROE",
}
VALUATION_SHEETS = {"pe": "市盈率(TTM)", "pb": "市净率"}


@dataclass(frozen=True)
class IngestionResult:
    output_dir: str
    source_hashes: dict[str, str]
    rows: dict[str, int]
    limitations: tuple[str, ...]


def sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1_048_576), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ingest_exploratory(
    *,
    price_workbook: Path,
    fundamental_workbook: Path,
    valuation_workbook: Path,
    output_dir: Path,
    max_symbols: int | None = None,
    only_price_parts: tuple[int, ...] | None = None,
) -> IngestionResult:
    """Convert the supplied static matrices to one documented exploratory dataset."""
    output_dir.mkdir(parents=True, exist_ok=True)
    if price_workbook.suffix.lower() == ".zip":
        calendar, _ = _write_price_archive(price_workbook, output_dir / "price_panels", max_symbols, only_price_parts)
        price_count = _unique_price_symbols(output_dir / "price_panels")
    else:
        calendar, price_count = _write_price_panels(price_workbook, output_dir / "price_panels.parquet", max_symbols)
    valuation_source: Any = valuation_workbook
    if valuation_workbook.suffix.lower() == ".zip":
        valuation_source = _archive_member(valuation_workbook, "raw_fundamentals_matrix_full.xlsx")
    fundamentals = _fundamental_rows(fundamental_workbook, valuation_source)
    pq.write_table(pa.table({"trade_date": calendar}), output_dir / "calendar.parquet", compression="zstd")
    _write_panel_table(output_dir / "fundamental_panels.parquet", fundamentals, tuple(FUNDAMENTAL_SHEETS) + tuple(VALUATION_SHEETS))
    source_hashes = {
        "price_workbook": sha256_file(price_workbook),
        "fundamental_workbook": sha256_file(fundamental_workbook),
        "valuation_workbook": sha256_file(valuation_workbook),
    }
    result = IngestionResult(
        output_dir=str(output_dir),
        source_hashes=source_hashes,
        rows={"calendar": len(calendar), "price_panels": price_count, "fundamental_panels": len(fundamentals)},
        limitations=(
            "SURVIVORSHIP_BIAS_CURRENT_UNIVERSE",
            "STATIC_INDUSTRY_MAPPING",
            "CONSERVATIVE_SYNTHETIC_FUNDAMENTAL_AVAILABILITY_DATES",
            "CASH_DIVIDENDS_UNAVAILABLE",
            "NO_SHARE_COUNT_IN_THIS_PRICE_MATRIX_NO_INFERRED_CORPORATE_ACTIONS",
        ),
    )
    (output_dir / "ingestion_manifest.json").write_text(
        json.dumps(asdict(result), ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8"
    )
    return result


def _write_price_panels(
    workbook: Path,
    output_path: Path,
    max_symbols: int | None = None,
    symbol_offset: int = 0,
) -> tuple[list[date], int]:
    streams = {field: iter_sheet_rows(workbook, sheet) for field, sheet in PRICE_SHEETS.items()}
    headers = {field: next(stream) for field, stream in streams.items()}
    first_header = headers["close"]
    if any(header != first_header for header in headers.values()):
        raise ValueError("price sheets must share an identical calendar")
    calendar = [date.fromisoformat(str(value)) for value in first_header[2:] if value is not None]
    writer: pq.ParquetWriter | None = None
    count = 0
    batch: list[dict[str, Any]] = []
    for row_index, combined in enumerate(zip(*(streams[field] for field in PRICE_FIELDS), strict=True)):
        if row_index < symbol_offset:
            continue
        if max_symbols is not None and count + len(batch) >= max_symbols:
            break
        symbols = {str(row[0]).zfill(6) for row in combined}
        names = {str(row[1]) for row in combined}
        if len(symbols) != 1 or len(names) != 1:
            raise ValueError("price sheet rows are not aligned by symbol and name")
        batch.append(
            {
                "symbol": symbols.pop(),
                "name": names.pop(),
                **{field: [_as_float(value) for value in row[2 : len(calendar) + 2]] for field, row in zip(PRICE_FIELDS, combined, strict=True)},
            }
        )
        if len(batch) == 250:
            writer = _write_price_batch(output_path, batch, writer)
            count += len(batch)
            batch = []
    if batch:
        writer = _write_price_batch(output_path, batch, writer)
        count += len(batch)
    if writer is None:
        raise ValueError("price workbook contains no price rows")
    writer.close()
    return calendar, count


def _write_price_archive(
    archive_path: Path,
    output_dir: Path,
    max_symbols: int | None = None,
    only_price_parts: tuple[int, ...] | None = None,
) -> tuple[list[date], int]:
    """Convert all price workbook parts without dropping symbols at part boundaries."""
    output_dir.mkdir(parents=True, exist_ok=True)
    members = sorted(
        name for name in ZipFile(archive_path).namelist()
        if name.startswith("raw_prices_matrix_full_part") and name.endswith(".xlsx")
    )
    if not members:
        raise ValueError("price archive has no raw_prices_matrix_full_part*.xlsx members")
    calendars: list[date] | None = None
    total = 0
    with ZipFile(archive_path) as archive, TemporaryDirectory(prefix="lianghua-price-") as temporary:
        for part_number, member in enumerate(members):
            if only_price_parts is not None and part_number not in only_price_parts:
                continue
            part_path = Path(temporary) / Path(member).name
            part_path.write_bytes(archive.read(member))
            if max_symbols is not None and total >= max_symbols:
                break
            part_calendar, part_count = _write_price_part_resumable(part_path, output_dir, part_number, max_symbols, total)
            if calendars is None:
                calendars = part_calendar
            elif calendars != part_calendar:
                raise ValueError("price archive parts do not share an identical calendar")
            total += part_count
    if calendars is None:
        raise ValueError("price archive contains no usable rows")
    return calendars, total


def _write_price_part_resumable(
    workbook: Path,
    output_dir: Path,
    part_number: int,
    max_symbols: int | None,
    global_offset: int,
) -> tuple[list[date], int]:
    """Stream one workbook once and checkpoint each closed Parquet chunk."""
    streams = {field: iter_sheet_rows(workbook, sheet) for field, sheet in PRICE_SHEETS.items()}
    headers = {field: next(stream) for field, stream in streams.items()}
    first_header = headers["close"]
    if any(header != first_header for header in headers.values()):
        raise ValueError("price sheets must share an identical calendar")
    calendar = [date.fromisoformat(str(value)) for value in first_header[2:] if value is not None]
    existing = sorted(
        path for path in output_dir.glob(f"part-{part_number:02d}-*.parquet")
        if ".tmp." not in path.name
    )
    offset = sum(pq.ParquetFile(path).metadata.num_rows for path in existing)
    chunk_number = len(existing)
    new_rows = 0
    batch: list[dict[str, Any]] = []
    for row_index, combined in enumerate(zip(*(streams[field] for field in PRICE_FIELDS), strict=True)):
        if row_index < offset:
            continue
        if max_symbols is not None and global_offset + new_rows >= max_symbols:
            break
        symbols = {str(row[0]).zfill(6) for row in combined}
        names = {str(row[1]) for row in combined}
        if len(symbols) != 1 or len(names) != 1:
            raise ValueError("price sheet rows are not aligned by symbol and name")
        batch.append({
            "symbol": symbols.pop(),
            "name": names.pop(),
            **{field: [_as_float(value) for value in row[2 : len(calendar) + 2]] for field, row in zip(PRICE_FIELDS, combined, strict=True)},
        })
        if len(batch) == 100:
            chunk_path = output_dir / f"part-{part_number:02d}-{chunk_number:03d}.parquet"
            temporary_path = chunk_path.with_suffix(".tmp.parquet")
            if temporary_path.exists():
                temporary_path.unlink()
            writer = _write_price_batch(temporary_path, batch, None)
            writer.close()
            temporary_path.replace(chunk_path)
            new_rows += len(batch)
            chunk_number += 1
            batch = []
    if batch:
        chunk_path = output_dir / f"part-{part_number:02d}-{chunk_number:03d}.parquet"
        temporary_path = chunk_path.with_suffix(".tmp.parquet")
        if temporary_path.exists():
            temporary_path.unlink()
        writer = _write_price_batch(temporary_path, batch, None)
        writer.close()
        temporary_path.replace(chunk_path)
        new_rows += len(batch)
    return calendar, offset + new_rows


def _archive_member(archive_path: Path, member_name: str) -> BytesIO:
    with ZipFile(archive_path) as archive:
        try:
            payload = archive.read(member_name)
        except KeyError as error:
            raise ValueError(f"archive member not found: {member_name}") from error
    return BytesIO(payload)


def _unique_price_symbols(path: Path) -> int:
    table = pq.read_table(path)
    return len(set(table.column("symbol").to_pylist()))


def _write_price_batch(
    path: Path, rows: list[dict[str, Any]], writer: pq.ParquetWriter | None
) -> pq.ParquetWriter:
    table = pa.table(
        {
            "symbol": [row["symbol"] for row in rows],
            "name": [row["name"] for row in rows],
            **{field: [row[field] for row in rows] for field in PRICE_FIELDS},
        }
    )
    if writer is None:
        writer = pq.ParquetWriter(path, table.schema, compression="zstd")
    writer.write_table(table)
    return writer


def _fundamental_rows(fundamental_workbook: Path, valuation_workbook: Path) -> list[dict[str, Any]]:
    streams = {
        field: iter_sheet_rows(fundamental_workbook, sheet) for field, sheet in FUNDAMENTAL_SHEETS.items()
    } | {field: iter_sheet_rows(valuation_workbook, sheet) for field, sheet in VALUATION_SHEETS.items()}
    headers = {field: next(stream) for field, stream in streams.items()}
    source_periods = {
        field: {str(period): index for index, period in enumerate(header) if index >= 2 and period is not None}
        for field, header in headers.items()
    }
    periods = [
        str(period)
        for period in headers["roe"][2:]
        if period is not None and all(str(period) in period_map for period_map in source_periods.values())
    ]
    if not periods:
        raise ValueError("fundamental and valuation sheets have no common periods")
    industries = _industries(fundamental_workbook)
    values_by_field: dict[str, dict[str, tuple[str, list[float | None]]]] = {}
    for field, stream in streams.items():
        rows_for_field: dict[str, tuple[str, list[float | None]]] = {}
        for row in stream:
            if len(row) < 2 or row[0] is None:
                continue
            symbol = str(row[0]).zfill(6)
            rows_for_field[symbol] = (
                str(row[1]),
                [
                    _as_float(row[source_periods[field][period]])
                    if len(row) > source_periods[field][period]
                    else None
                    for period in periods
                ],
            )
        values_by_field[field] = rows_for_field
    symbols = sorted(set().union(*(values.keys() for values in values_by_field.values())))
    return [
        {
            "symbol": symbol,
            "name": next(values[symbol][0] for values in values_by_field.values() if symbol in values),
            "period": periods,
            "industry": industries.get(symbol),
            **{
                field: values_by_field[field].get(symbol, ("", [None] * len(periods)))[1]
                for field in values_by_field
            },
        }
        for symbol in symbols
    ]


def _industries(workbook: Path) -> dict[str, str]:
    rows = iter_sheet_rows(workbook, "股票信息")
    header = next(rows)
    try:
        code_index = header.index("代码")
        industry_index = header.index("申万一级")
    except ValueError as error:
        raise ValueError("fundamental workbook lacks 代码 or 申万一级") from error
    return {
        str(row[code_index]).zfill(6): str(row[industry_index])
        for row in rows
        if len(row) > max(code_index, industry_index) and row[code_index] is not None and row[industry_index] is not None
    }


def _write_panel_table(path: Path, rows: list[dict[str, Any]], fields: tuple[str, ...]) -> None:
    columns: dict[str, Any] = {
        "symbol": [row["symbol"] for row in rows],
        "name": [row["name"] for row in rows],
    }
    if "period" in rows[0]:
        columns["period"] = [row["period"] for row in rows]
        columns["industry"] = [row["industry"] for row in rows]
    columns.update({field: [row[field] for row in rows] for field in fields})
    pq.write_table(pa.table(columns), path, compression="zstd")


def _as_float(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None
