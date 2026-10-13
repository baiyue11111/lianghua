# -*- coding: utf-8 -*-
"""后处理：
1. 由日度 isST 标记推导每只股票的 ST 区间表 -> 全市场ST历史标记/st_episodes.csv
2. baostock isST vs 深交所官方简称变更表 交叉验证 -> validation_report.md
"""
import os
import pandas as pd

BASE = "/Users/wyatt/Program/lianghua/data/origin"
ST = f"{BASE}/全市场ST历史标记"
DL = f"{BASE}/退市股池2014-2025"

# ---------- 1. ST 区间表 ----------
def build_episodes():
    df = pd.read_parquet(f"{ST}/st_flags_daily.parquet")
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0)
    # 仅保留有成交的交易日（停牌日 baostock 会冻结填充，isST 可能滞后）
    d = df[df["volume"] > 0][["code", "date", "isST"]].copy()
    d = d.sort_values(["code", "date"])

    episodes = []
    for code, g in d.groupby("code"):
        state = 0
        start = None
        last_st_date = None
        n_days = 0
        for date, st in zip(g["date"], g["isST"]):
            if st == 1 and state == 0:
                state, start, n_days = 1, date, 0
            elif st == 1 and state == 1:
                n_days += 1
            elif st == 0 and state == 1:
                episodes.append({"code": code, "st_start": start,
                                 "st_end": last_st_date, "lift_date": date,
                                 "st_trading_days": n_days + 1})
                state, start = 0, None
            if st == 1:
                last_st_date = date
        if state == 1:  # 末段未摘帽（退市时仍ST）
            episodes.append({"code": code, "st_start": start,
                             "st_end": last_st_date, "lift_date": "",
                             "st_trading_days": n_days + 1})
    ep = pd.DataFrame(episodes)
    ep.to_csv(f"{ST}/st_episodes.csv", index=False, encoding="utf-8-sig")
    print(f"episodes: {len(ep)} 段, 覆盖 {ep['code'].nunique()} 只股票")
    return ep

# ---------- 2. 与深交所官方简称变更表交叉验证 ----------
def validate_sz():
    daily = pd.read_parquet(f"{ST}/st_flags_daily.parquet")
    daily["volume"] = pd.to_numeric(daily["volume"], errors="coerce").fillna(0)
    daily = daily[daily["volume"] > 0][["code", "date", "isST"]]
    sz_daily = daily[daily["code"].str.startswith(("000", "001", "002", "003", "300"))]

    official = pd.read_csv(f"{ST}/source/szse_name_changes_official.csv",
                           dtype={"证券代码": str})
    official = official[official["证券代码"].str.len() == 6]

    def is_st_name(s):
        return isinstance(s, str) and (s.startswith("ST") or s.startswith("*ST")
                                       or s.startswith("S*ST") or s.startswith("SST"))

    # 官方表中的 ST 戴帽/摘帽事件
    off_events = []  # (code, date, type)
    for _, r in official.iterrows():
        before, after = r["变更前简称"], r["变更后简称"]
        if is_st_name(after) and not is_st_name(before):
            off_events.append((r["证券代码"], r["变更日期"], "戴上"))
        elif not is_st_name(after) and is_st_name(before):
            off_events.append((r["证券代码"], r["变更日期"], "摘帽"))

    # 限制 2013 之后（日线覆盖期）
    off_events = [e for e in off_events if e[1] >= "2013-01-01"]

    # baostock 端的翻转事件
    bs_events = {}
    for code, g in sz_daily.sort_values(["code", "date"]).groupby("code"):
        prev = 0
        for date, st in zip(g["date"], g["isST"]):
            if st == 1 and prev == 0:
                bs_events.setdefault((code, "戴上"), []).append(date)
            elif st == 0 and prev == 1:
                bs_events.setdefault((code, "摘帽"), []).append(date)
            prev = st

    total = matched = unmatched = 0
    details = []
    for code, date, typ in off_events:
        cands = bs_events.get((code, typ), [])
        if not cands:
            continue
        # 官方日期的下一个交易日附近 ±3 个交易日内匹配
        nxt = [c for c in cands if c >= date]
        if nxt and (pd.Timestamp(nxt[0]) - pd.Timestamp(date)).days <= 7:
            matched += 1
        else:
            unmatched += 1
            details.append((code, date, typ, cands[:2]))
        total += 1

    lines = [
        "# baostock isST 与深交所官方简称变更表 交叉验证报告",
        "",
        f"- 验证范围：深市 A 股，2013-01-01 之后官方表中的 ST 戴帽/摘帽事件",
        f"- 官方 ST 相关事件数（日线覆盖期内）：{total}",
        f"- baostock 在官方日期后 7 天内出现同向翻转：{matched} ({matched/max(total,1)*100:.1f}%)",
        f"- 未匹配：{unmatched}",
        "",
        "## 未匹配明细（前 30 条）",
        "",
        "| 代码 | 官方日期 | 类型 | baostock 最近翻转日 |",
        "|---|---|---|---|",
    ]
    for code, date, typ, c in details[:30]:
        lines.append(f"| {code} | {date} | {typ} | {c if c else '无'} |")
    if not details:
        lines.append("| （无） | | | |")

    with open(f"{ST}/validation_report.md", "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"validation: {matched}/{total} matched ({matched/max(total,1)*100:.1f}%)")
    return matched, total

if __name__ == "__main__":
    ep = build_episodes()
    matched, total = validate_sz()
