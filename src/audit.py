"""Immutable audit bundles and the canonical artifact consumed by the HTML reader."""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from hashlib import sha256
import json
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.csv as pacsv
import pyarrow.parquet as pq

from evaluator import evaluate_equity
from research import BacktestResult


def deterministic_run_id(source_hashes: dict[str, str], config_hash: str, code_version: str) -> str:
    payload = json.dumps(
        {"code_version": code_version, "config_hash": config_hash, "source_hashes": source_hashes},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(payload.encode("utf-8")).hexdigest()[:24]


def code_version(source_root: Path) -> str:
    digest = sha256()
    for path in sorted(source_root.rglob("*.py")):
        digest.update(path.relative_to(source_root).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def write_audit_bundle(
    *,
    result: BacktestResult,
    source_hashes: dict[str, str],
    config_hash: str,
    research_tier: str,
    output_root: Path,
    limitations: list[str],
) -> Path:
    version = code_version(Path(__file__).parent)
    run_id = deterministic_run_id(source_hashes, config_hash, version)
    run_dir = output_root / run_id
    # Reserve the exact run identity before writing anything. Existing evidence
    # is never overwritten, including incomplete prior runs needing inspection.
    run_dir.mkdir(parents=True, exist_ok=False)
    audit_dir = run_dir / "audit"
    audit_dir.mkdir()
    equity_rows = [{"trade_date": day.isoformat(), "equity": equity} for day, equity in result.equity]
    _write_records(audit_dir / "equity", equity_rows)
    _write_records(audit_dir / "orders", [_order_row(order) for order in result.orders])
    _write_records(audit_dir / "fills", [_fill_row(fill) for fill in result.fills])
    _write_records(audit_dir / "targets", [_target_row(target) for target in result.targets])
    _write_records(audit_dir / "daily_holdings", result.holdings)
    _write_records(audit_dir / "signals", result.signals)
    metrics = evaluate_equity(result.equity)
    # This writer receives equity, not validated data/ledger/walk-forward
    # evidence. Descriptive metrics cannot certify candidate eligibility.
    status = "UNVALIDATED_BACKTEST"
    (run_dir / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "research_tier": research_tier,
                "strategy_id": result.strategy_id,
                "source_hashes": source_hashes,
                "config_hash": config_hash,
                "code_version": version,
                "status": status,
                "limitations": limitations,
            },
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        ),
        encoding="utf-8",
    )
    artifact = _artifact(run_id, result.strategy_id, metrics, equity_rows, [_fill_row(fill) for fill in result.fills], limitations, status)
    (run_dir / "artifact.json").write_text(json.dumps(artifact, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    return run_dir


def _write_records(stem: Path, rows: list[dict[str, Any]] | tuple[dict[str, Any], ...]) -> None:
    if not rows:
        rows = [{"_empty": True}]
    table = pa.Table.from_pylist(rows)
    pq.write_table(table, stem.with_suffix(".parquet"), compression="zstd")
    pacsv.write_csv(table, stem.with_suffix(".csv"))


def _order_row(order: Any) -> dict[str, Any]:
    return {**asdict(order), "side": order.side.value, "signal_date": order.signal_date.isoformat(), "submit_date": order.submit_date.isoformat()}


def _fill_row(fill: Any) -> dict[str, Any]:
    return {**asdict(fill), "side": fill.side.value, "trade_date": fill.trade_date.isoformat()}


def _target_row(target: Any) -> dict[str, Any]:
    return {
        "strategy_id": target.strategy_id,
        "as_of": target.as_of.isoformat(),
        "gross_exposure": target.gross_exposure,
        "target_weights": json.dumps(target.target_weights, ensure_ascii=False, sort_keys=True),
        "reasons": json.dumps(target.reasons, ensure_ascii=False, sort_keys=True),
    }


def _artifact(
    run_id: str,
    strategy_id: str,
    metrics: dict[str, Any],
    equity_rows: list[dict[str, Any]],
    fills: list[dict[str, Any]],
    limitations: list[str],
    status: str,
) -> dict[str, Any]:
    generated_at = datetime.now(UTC).isoformat()
    source = {
        "id": "audit_bundle",
        "label": "Immutable local audit bundle",
        "path": "audit/equity.parquet",
        "query": {"engine": "duckdb", "sql": "SELECT * FROM 'audit/equity.parquet'", "description": "Generated by the deterministic backtest ledger", "tables_used": ["audit/equity.parquet", "audit/fills.parquet"]},
    }
    return {
        "surface": "report",
        "manifest": {
            "version": 1,
            "surface": "report",
            "title": f"A股探索回测审计：{strategy_id}",
            "description": f"运行 {run_id}，状态 {status}",
            "generatedAt": generated_at,
            "sources": [source],
            "cards": [
                {"id": "final_equity", "dataset": "metrics", "sourceId": "audit_bundle", "metrics": [{"label": "期末权益", "field": "final_equity", "format": "currency"}]},
                {"id": "sharpe", "dataset": "metrics", "sourceId": "audit_bundle", "metrics": [{"label": "年化夏普", "field": "sharpe", "format": "number"}]},
                {"id": "drawdown", "dataset": "metrics", "sourceId": "audit_bundle", "metrics": [{"label": "最大回撤", "field": "max_drawdown", "format": "percent"}]},
            ],
            "charts": [
                {"id": "equity_curve", "title": "权益曲线", "type": "line", "dataset": "equity", "sourceId": "audit_bundle", "encodings": {"x": {"field": "trade_date", "type": "temporal"}, "y": {"field": "equity", "type": "quantitative", "format": "currency"}}, "layout": "full"}
            ],
            "tables": [
                {"id": "fills", "title": "逐笔成交", "dataset": "fills", "sourceId": "audit_bundle", "defaultSort": {"field": "trade_date", "direction": "desc"}, "columns": [{"field": "trade_date", "label": "日期", "type": "date"}, {"field": "symbol", "label": "代码", "type": "text"}, {"field": "side", "label": "方向", "type": "text"}, {"field": "shares", "label": "数量", "format": "number"}, {"field": "price", "label": "成交价", "format": "number"}, {"field": "commission", "label": "佣金", "format": "currency"}, {"field": "reason", "label": "原因", "type": "text"}]}
            ],
            "blocks": [
                {"id": "title", "type": "markdown", "body": f"# A股探索回测审计：{strategy_id}\n\n状态：`{status}`。"},
                {"id": "kpis", "type": "metric-strip", "cardIds": ["final_equity", "sharpe", "drawdown"]},
                {"id": "limits", "type": "markdown", "body": "## 数据与研究限制\n\n" + "\n".join(f"- {item}" for item in limitations)},
                {"id": "equity", "type": "chart", "chartId": "equity_curve"},
                {"id": "fills", "type": "table", "tableId": "fills"},
            ],
        },
        "snapshot": {"version": 1, "generatedAt": generated_at, "status": "partial", "datasets": {"metrics": [metrics], "equity": _downsample(equity_rows, 2_000), "fills": fills[:2_000]}},
        "sources": [source],
    }


def _downsample(rows: list[dict[str, Any]], maximum: int) -> list[dict[str, Any]]:
    if len(rows) <= maximum:
        return rows
    step = (len(rows) + maximum - 1) // maximum
    sampled = rows[::step]
    return sampled[:-1] + [rows[-1]] if sampled[-1] != rows[-1] else sampled
