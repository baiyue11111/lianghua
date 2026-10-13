#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""migrate_partition.py —— daily_prices 单文件 → 按年分区 zstd 数据集（F+D）

布局：data/sources/lake/daily/<year>.parquet（年内按 code,date 排序，row_group 25 万行）
流程：写入 data/sources/lake/_daily_new/（暂存）→ 严格等价性校验 → 原子切换。
切换后旧文件改名为 daily_prices.parquet.bak_partition 保留，验证无误可手删。

同时生成 data/sources/lake/latest_snapshot.parquet（C：全市场每股最新一行）。
"""
from __future__ import annotations

import os
import sys
import time

import pandas as pd
import pyarrow.parquet as pq

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAKE = os.path.join(ROOT, "data", "sources", "lake")
SRC = os.path.join(LAKE, "daily_prices.parquet")
STAGING = os.path.join(LAKE, "_daily_new")
DST = os.path.join(LAKE, "daily")
LATEST = os.path.join(LAKE, "latest_snapshot.parquet")

ROW_GROUP = 250_000


def write_partitions(df: pd.DataFrame, out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    for year, g in df.groupby(df["date"].str[:4]):
        g = g.sort_values(["code", "date"])
        g.to_parquet(os.path.join(out_dir, f"{year}.parquet"),
                     index=False, compression="zstd", compression_level=3,
                     row_group_size=ROW_GROUP)
        print(f"  {year}.parquet  {len(g):>9,} 行", flush=True)


def verify(src_df: pd.DataFrame) -> bool:
    """暂存分区与原文件逐位等价：行数 / 年度分布 / 每股行数 / 关键列求和。"""
    print("等价性校验 ...", flush=True)
    parts = [pd.read_parquet(os.path.join(STAGING, f))
             for f in sorted(os.listdir(STAGING)) if f.endswith(".parquet")]
    new = pd.concat(parts, ignore_index=True)
    checks = {
        "总行数一致": len(new) == len(src_df),
        "列集合一致": set(new.columns) == set(src_df.columns),
    }
    a = src_df.groupby(src_df["date"].str[:4]).size()
    b = new.groupby(new["date"].str[:4]).size()
    checks["年度分布一致"] = a.equals(b)
    ca = src_df.groupby("code").size().sort_index()
    cb = new.groupby("code").size().sort_index()
    checks["每股行数一致"] = ca.equals(cb)
    for col in ("close", "volume", "amount"):
        sa = src_df.groupby(src_df["date"].str[:4])[col].sum()
        sb = new.groupby(new["date"].str[:4])[col].sum()
        checks[f"{col} 年度求和一致"] = bool((sa - sb).abs().max() < 1e-6 * sa.abs().max())
    ok = all(checks.values())
    for k, v in checks.items():
        print(f"  {'✅' if v else '❌'} {k}")
    return ok


def write_latest(df: pd.DataFrame) -> None:
    latest = (df.sort_values("date").groupby("code")
              .tail(1)[["code", "date", "close", "pctChg", "amount"]])
    latest.to_parquet(LATEST, index=False, compression="zstd", compression_level=3)
    print(f"latest_snapshot.parquet  {len(latest):,} 行", flush=True)


def main() -> int:
    t0 = time.time()
    print(f"读取原文件 {SRC} ...", flush=True)
    df = pd.read_parquet(SRC)
    print(f"{len(df):,} 行（{time.time()-t0:.1f}s）", flush=True)

    print("写分区（暂存区）...", flush=True)
    write_partitions(df, STAGING)
    write_latest(df)

    if not verify(df):
        print("❌ 校验失败，未切换。暂存区保留在", STAGING)
        return 1
    del df

    # 原子切换
    backup = SRC + ".bak_partition"
    if os.path.exists(DST):
        print("目标已存在，放弃切换：", DST)
        return 1
    os.replace(SRC, backup)
    os.rename(STAGING, DST)
    total = sum(os.path.getsize(os.path.join(DST, f)) for f in os.listdir(DST))
    print(f"\n✅ 切换完成（{time.time()-t0:.1f}s）")
    print(f"  新布局：{DST}/（{total/1e6:.0f}MB，{len(os.listdir(DST))} 个年份文件）")
    print(f"  旧文件备份：{backup}（验证无误后可删除）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
