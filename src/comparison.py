"""Cross-strategy comparison artifact built only from immutable run bundles."""

from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq


def write_comparison(run_dirs: list[Path], output_dir: Path) -> Path:
    """Produce a bounded, source-linked comparison without recomputing a strategy."""
    output_dir.mkdir(parents=True, exist_ok=True)
    audit_dir = output_dir / "audit"
    audit_dir.mkdir(exist_ok=True)
    metrics: list[dict[str, Any]] = []
    curve: list[dict[str, Any]] = []
    for run_dir in run_dirs:
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        values = json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))
        strategy = manifest["strategy_id"]
        metrics.append({"strategy": strategy, "status": manifest["status"], **values})
        equity = pq.read_table(run_dir / "audit" / "equity.parquet").to_pylist()
        step = max(1, (len(equity) + 599) // 600)
        curve.extend({"strategy": strategy, **row} for row in equity[::step])
    pq.write_table(pa.Table.from_pylist(curve), audit_dir / "equity_comparison.parquet", compression="zstd")
    pq.write_table(pa.Table.from_pylist(metrics), audit_dir / "strategy_metrics.parquet", compression="zstd")
    now = datetime.now(UTC).isoformat()
    source = {
        "id": "comparison_audit",
        "label": "Immutable strategy comparison audit",
        "path": "audit/equity_comparison.parquet",
        "query": {"engine": "duckdb", "sql": "SELECT * FROM 'audit/equity_comparison.parquet'", "description": "Union of immutable strategy equity ledgers", "tables_used": ["audit/equity_comparison.parquet", "audit/strategy_metrics.parquet"]},
    }
    artifact = {
        "surface": "report",
        "manifest": {
            "version": 1, "surface": "report", "title": "A股探索策略对比审计", "generatedAt": now,
            "sources": [source],
            "charts": [{"id": "comparison", "title": "策略权益曲线对比", "type": "line", "dataset": "equity", "sourceId": "comparison_audit", "encodings": {"x": {"field": "trade_date", "type": "temporal"}, "y": {"field": "equity", "type": "quantitative", "format": "currency"}, "color": {"field": "strategy", "type": "nominal"}}, "layout": "full"}],
            "tables": [{"id": "metrics", "title": "策略 KPI 与资格状态", "dataset": "metrics", "sourceId": "comparison_audit", "defaultSort": {"field": "sharpe", "direction": "desc"}, "columns": [{"field": "strategy", "label": "策略", "type": "text"}, {"field": "sharpe", "label": "夏普", "format": "number"}, {"field": "max_drawdown", "label": "最大回撤", "format": "percent"}, {"field": "total_return", "label": "总收益", "format": "percent"}, {"field": "status", "label": "状态", "type": "text"}]}],
            "blocks": [{"id": "title", "type": "markdown", "body": "# A股探索策略对比审计\n\n当前验证为探索级且为子样本，结论固定为 `NO_QUALIFIED_STRATEGY`。"}, {"id": "curve", "type": "chart", "chartId": "comparison"}, {"id": "table", "type": "table", "tableId": "metrics"}],
        },
        "snapshot": {"version": 1, "generatedAt": now, "status": "partial", "datasets": {"equity": curve, "metrics": metrics}},
        "sources": [source],
    }
    (output_dir / "artifact.json").write_text(json.dumps(artifact, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    return output_dir
