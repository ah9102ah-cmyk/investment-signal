# -*- coding: utf-8 -*-
"""影子观察日志(阶段 C): 每周记录 common_v1 vs category_v2_shadow 对比。

用途: 为 8~12 周影子观察积累记录(任务书 §16)。
记录内容(每资产): 正式三态 / 影子三态 / 是否一致 / 差异原因 / 数据质量 / 类内排名;
组合层: 风险提示; 数据层: 各资产数据长度(健康)。
输出: 追加到 data/shadow_log.json(按日期), 并打印本周报告。
只观察, 不切换策略, 不输出仓位。
"""
import json, os, sys, datetime as dt
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtest_v2 as bt
import portfolio_v2 as pf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_FILE = os.path.join(ROOT, "data", "shadow_log.json")


def diff_reason(asset, formal, shadow, shadow_sig):
    """正式 vs 影子差异的一句话原因(大白话)。"""
    if formal == shadow:
        return ""
    if formal == "买入" and shadow == "持有":
        return "正式版按偏强总分买入；影子版要求结构轮与价格轮同时确认才买"
    if formal == "买入" and shadow == "卖出":
        return "正式版偏强但影子版双轮同步转弱提前退出"
    if formal == "卖出" and shadow == "持有":
        return "正式版按弱项总分卖出；影子版在中长期趋势仍向上时不会仅因估值偏贵卖出"
    if formal == "卖出" and shadow == "买入":
        return "正式版弱项触发卖出，但影子版双轮确认转强（需要继续观察是否误判）"
    if formal == "持有" and shadow == "买入":
        return "正式版未达偏强门槛；影子版双轮确认达到买入条件"
    if formal == "持有" and shadow == "卖出":
        return "正式版未达弱项门槛；影子版双轮同步转弱提前退出"
    if shadow == "数据不足":
        return "影子版缺关键数据，按数据不足处理（保守）"
    return "两套规则口径不同"


def main():
    assets = bt.build_assets()
    today = dt.date.today().isoformat()
    entries = {}
    health = {}
    signals = {}

    for name, df in assets.items():
        health[name] = {"days": len(df), "first": str(df.index.min().date()),
                        "last": str(df.index.max().date()),
                        "pe_valid": int(df["pe"].notna().sum()) if "pe" in df else None,
                        "us10y_valid": int(df["us10y"].notna().sum()) if "us10y" in df else None}
        if name == "恒生科技":
            entries[name] = {"formal": "数据不足", "shadow": "数据不足", "match": True,
                             "reason": "无可靠历史估值且行情长度不足", "quality": "估值缺失",
                             "degraded": ["估值缺失", "行情历史不足"], "class_rank": None}
            continue
        idx = df.index
        weeks = pd.Series(idx.to_period("W-FRI"), index=idx)
        eval_days = idx[weeks.ne(weeks.shift(-1))]
        day = eval_days[-1]
        formal = bt.common_v1_actions(df, name).get(day, "数据不足")
        shadow_sig = bt.v2_signal_at(df, name, "balanced", day)
        shadow = shadow_sig["action"]
        signals[name] = shadow_sig
        entries[name] = {
            "formal": formal,
            "shadow": shadow,
            "match": formal == shadow,
            "reason": diff_reason(name, formal, shadow, shadow_sig),
            "quality": shadow_sig["data_quality"],
            "degraded": shadow_sig["degraded_fields"],
            "structural": shadow_sig["structural_score"],
            "technical": shadow_sig["technical_score"],
            "momentum": shadow_sig["momentum_score"],
            "composite": shadow_sig["composite_score"],
            "class_rank": shadow_sig.get("class_rank"),
            "signal_date": str(day.date()),
        }

    # 组合层: 类内排名 + 风险提示
    warnings = pf.apply_portfolio(signals)
    for name, sig in signals.items():
        entries[name]["class_rank"] = sig.get("class_rank")

    # 数据源健康(轻量): 通过缓存文件时间与长度判断
    try:
        fund = json.load(open(os.path.join(ROOT, "data", "fund.json"), encoding="utf-8"))
        health["fund_us10y"] = fund.get("us10y")
    except Exception as e:
        health["fund_us10y"] = f"读取失败: {e}"

    # 累积日志
    log = {}
    if os.path.exists(LOG_FILE):
        try:
            log = json.load(open(LOG_FILE, encoding="utf-8"))
        except Exception:
            log = {}
    log[today] = {"date": today, "entries": entries, "warnings": warnings, "health": health}
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=1)

    # 本周报告
    print("=" * 78)
    print(f"影子观察周报  {today}  (共 {len(log)} 周记录)")
    print("=" * 78)
    print(f"{'资产':<8}{'正式':<6}{'影子':<6}{'一致':<4}{'类内':<4}{'质量':<6} 说明")
    for name, e in entries.items():
        mark = "✓" if e["match"] else "✗"
        rank = "—" if e["class_rank"] is None else str(e["class_rank"])
        print(f"{name:<8}{e['formal']:<6}{e['shadow']:<6}{mark:<4}{rank:<4}{e['quality']:<6}{e['reason']}")
    print("-" * 78)
    if warnings:
        for w in warnings:
            print("⚠", w)
    else:
        print("组合层：暂无风险集中提示")
    print("=" * 78)
    print("数据健康(最近交易日):", {k: v["last"] if isinstance(v, dict) else v for k, v in health.items() if k != "fund_us10y"})
    print(f"已追加到 {LOG_FILE}")


if __name__ == "__main__":
    main()
