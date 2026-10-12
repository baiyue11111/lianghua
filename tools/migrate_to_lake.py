# -*- coding: utf-8 -*-
"""xlsx 宽表 -> Parquet 长表迁移（内存优化版）。

每卷逐 sheet melt -> 写中间 parquet -> 合并 -> 最终 concat。
支持断点续传：已有 _tmp/prices_partN.parquet 则跳过。
"""
import os, gc, time, traceback
import numpy as np
import pandas as pd

BASE = "/Users/wyatt/Program/lianghua/data"
ORIGIN = f"{BASE}/origin"
POOL = f"{ORIGIN}/完整全股池2014-2025"
DL = f"{ORIGIN}/退市股池2014-2025"
LAKE = f"{BASE}/lake"
TMP = f"{LAKE}/_tmp"
os.makedirs(TMP, exist_ok=True)

LOG = open(f"{LAKE}/migrate.log", "a", buffering=1)

def log(msg):
    LOG.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    print(msg, flush=True)

PRICE_SHEETS = {
    "开盘价": "open", "收盘价": "close", "最高价": "high", "最低价": "low",
    "成交量": "volume", "成交额": "amount",
    "流通市值": "float_mv", "流通股本": "float_shares", "总市值": "total_mv",
}

def zcode(c):
    s = str(int(c)) if isinstance(c, (int, float, np.integer, np.floating)) else str(c)
    return s.zfill(6)

def exchange_of(code):
    p = code[:3]
    if p in ("600", "601", "603", "605", "688"): return "SH"
    if p in ("000", "001", "002", "003", "300"): return "SZ"
    if p in ("920", "430", "83", "87", "88"): return "BJ"
    return "?"

# ---------- 1. stock_info ----------
def build_stock_info():
    out = f"{LAKE}/stock_info.parquet"
    if os.path.exists(out):
        log("stock_info: 已存在，跳过")
        return pd.read_parquet(out)

    frames = []
    for i in range(1, 8):
        info = pd.read_excel(f"{POOL}/raw_prices_matrix_full_part{i}.xlsx",
                             sheet_name="股票信息")
        info = info.rename(columns={
            "股票代码": "code", "股票名称": "name", "首交易日": "first_date",
            "末交易日": "last_date", "交易日数": "n_trading_days",
            "是否有日线": "has_daily"})
        info["code"] = info["code"].map(zcode)
        info["part"] = i
        frames.append(info)
    cur = pd.concat(frames, ignore_index=True)
    dup = cur["code"].duplicated().sum()
    if dup:
        log(f"stock_info: 警告 现存股代码重复 {dup} 个")

    dl = pd.read_csv(f"{DL}/delisted_list.csv", dtype=str)
    dli = pd.DataFrame({
        "code": dl["code"], "name": dl["name"],
        "first_date": dl["list_date"], "last_date": dl["delist_date"],
        "n_trading_days": np.nan, "has_daily": True, "part": 0})
    cur["status"] = "listed"
    dli["status"] = "delisted"
    alli = pd.concat([cur, dli], ignore_index=True)
    alli["exchange"] = alli["code"].map(exchange_of)
    alli = alli[["code", "name", "exchange", "status", "first_date",
                 "last_date", "n_trading_days", "has_daily"]]
    alli.to_parquet(out, index=False)
    log(f"stock_info: {len(alli)} 行 (现存 {len(cur)} + 退市 {len(dli)})")
    del frames, cur, dli; gc.collect()
    return alli

# ---------- 2. daily_prices ----------
def _melt_sheet(filepath, sheet, value_col):
    """读取一个 sheet，melt 成长表，返回 DataFrame(code,name,date,value)。"""
    df = pd.read_excel(filepath, sheet_name=sheet)
    df["股票代码"] = df["股票代码"].map(zcode)
    id_cols = ["股票代码", "股票名称"]
    long = df.melt(id_vars=id_cols, var_name="date", value_name=value_col)
    long = long.rename(columns={"股票代码": "code", "股票名称": "name"})
    long[value_col] = pd.to_numeric(long[value_col], errors="coerce")
    long[value_col] = long[value_col].astype(np.float32)
    del df; gc.collect()
    return long

def build_daily_prices():
    final = f"{LAKE}/daily_prices.parquet"
    if os.path.exists(final):
        log("daily_prices: 已存在，跳过")
        return pd.read_parquet(final)

    per_part = []
    for i in range(1, 7):  # part7 = 北交所无日线
        part_out = f"{TMP}/prices_part{i}.parquet"
        if os.path.exists(part_out):
            log(f"prices part{i}: 已存在，跳过")
            per_part.append(part_out)
            continue

        t0 = time.time()
        fp = f"{POOL}/raw_prices_matrix_full_part{i}.xlsx"
        merged = None
        for sheet, col in PRICE_SHEETS.items():
            long_df = _melt_sheet(fp, sheet, col)
            if merged is None:
                merged = long_df
            else:
                merged = merged.merge(long_df, on=["code", "name", "date"], how="outer")
            del long_df; gc.collect()

        merged["pool"] = "current"
        for c in ["close_hfq", "turn", "pctChg", "tradestatus", "isST"]:
            merged[c] = np.nan
        merged.to_parquet(part_out, index=False)
        log(f"prices part{i}: {len(merged)} 行, {time.time()-t0:.0f}s")
        per_part.append(part_out)
        del merged; gc.collect()

    log("concat all parts...")
    cur = pd.concat([pd.read_parquet(p) for p in per_part], ignore_index=True)

    # 退市股
    dl_file = f"{DL}/delisted_prices_daily.parquet"
    if os.path.exists(dl_file):
        dl = pd.read_parquet(dl_file)
        dlp = pd.DataFrame({
            "code": dl["code"], "name": "", "date": dl["date"],
            "open": dl["open"], "high": dl["high"], "low": dl["low"],
            "close": dl["close"], "volume": dl["volume"], "amount": dl["amount"],
            "float_mv": np.nan, "float_shares": np.nan, "total_mv": np.nan,
            "pool": "delisted",
            "close_hfq": dl["close_hfq"], "turn": dl["turn"],
            "pctChg": dl["pctChg"], "tradestatus": dl["tradestatus"],
            "isST": dl["isST"],
        })
        names = pd.read_parquet(f"{LAKE}/stock_info.parquet")[["code", "name"]]
        dlp = dlp.merge(names, on="code", how="left", suffixes=("", "_y"))
        dlp["name"] = dlp.pop("name_y")
        allp = pd.concat([cur, dlp], ignore_index=True)
    else:
        log("警告：退市股价格文件不存在，仅迁移现存股")
        allp = cur

    allp["date"] = allp["date"].astype(str)
    allp = allp.sort_values(["code", "date"]).reset_index(drop=True)
    allp.to_parquet(final, index=False)
    log(f"daily_prices: {len(allp)} 行, {allp['code'].nunique()} 只, "
        f"{allp['date'].min()} ~ {allp['date'].max()}")

    # 清理中间文件
    for p in per_part:
        os.remove(p)
    gc.collect()
    return allp

# ---------- 3. valuation_factors ----------
def build_valuation():
    out = f"{LAKE}/valuation_factors.parquet"
    if os.path.exists(out):
        log("valuation: 已存在，跳过")
        return
    f = f"{POOL}/raw_fundamentals_matrix_full.xlsx"
    mapping = {"市盈率(TTM)": "pe_ttm", "市净率": "pb",
               "净资产收益率(ROE)": "roe", "股息率(TTM)": "div_yield"}
    merged = None
    for sheet, col in mapping.items():
        df = pd.read_excel(f, sheet_name=sheet)
        df["股票代码"] = df["股票代码"].map(zcode)
        long = df.melt(id_vars=["股票代码", "股票名称"], var_name="quarter", value_name=col)
        long = long.rename(columns={"股票代码": "code", "股票名称": "name"})
        long[col] = pd.to_numeric(long[col], errors="coerce")
        if merged is None:
            merged = long
        else:
            merged = merged.merge(long, on=["code", "name", "quarter"], how="outer")
        del df, long; gc.collect()
    merged.to_parquet(out, index=False)
    log(f"valuation_factors: {len(merged)} 行")
    del merged; gc.collect()

# ---------- 4. fundamentals ----------
def build_fundamentals():
    out = f"{LAKE}/fundamentals.parquet"
    if os.path.exists(out):
        log("fundamentals: 已存在，跳过")
        return
    f = f"{ORIGIN}/全股票基本面.xlsx"
    xl = pd.ExcelFile(f)
    sheets = [s for s in xl.sheet_names if s != "股票信息"]
    merged = None
    for sheet in sheets:
        df = pd.read_excel(f, sheet_name=sheet)
        kc = "代码" if "代码" in df.columns else "股票代码"
        kn = "名称" if "名称" in df.columns else "股票名称"
        df[kc] = df[kc].map(zcode)
        col = sheet.strip()
        long = df.melt(id_vars=[kc, kn], var_name="quarter", value_name=col)
        long = long.rename(columns={kc: "code", kn: "name"})
        long[col] = pd.to_numeric(long[col], errors="coerce")
        if merged is None:
            merged = long
        else:
            merged = merged.merge(long, on=["code", "name", "quarter"], how="outer")
        del df, long; gc.collect()
    merged.to_parquet(out, index=False)
    log(f"fundamentals: {len(merged)} 行 x {len(sheets)+3} 列")
    del merged; gc.collect()

if __name__ == "__main__":
    log("==== migrate start (v2) ====")
    try:
        build_stock_info()
        build_daily_prices()
        build_valuation()
        build_fundamentals()
    except Exception:
        log("迁移异常:\n" + traceback.format_exc())
    log("==== migrate end ====")
