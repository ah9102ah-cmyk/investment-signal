# -*- coding: utf-8 -*-
"""影子观察日志(阶段 C, 2026-08 更正版): 每周记录 common_v1 vs category_v2_shadow 对比。

P2-3 更正: 每周每个指数保存:
- strategy_version / candidate(主候选, 冻结) / signal_date / price_date / 当时价格
- 正式结果 / 影子结果 / 是否一致 / 差异原因
- 全部原始分项(valuation/macro/breadth/trend/momentum) + 结构轮/价格轮/综合分 + 可用权重占比
- 数据源及日期(data_sources) / 数据质量与降级字段
- 是否与正式结果一致 / 差异原因 / 信号是否反复(与上周影子结果对比)
- 后续 1/4/12 周收益 + 同期始终持有收益 + 同期 common_v1 收益(日期到达后补写, 不用未来数据生成当期信号)

P1-3 更正: 候选来自 cfg.CANDIDATE_MAP(每个指数显式主候选), 不再全部固定 balanced;
no_candidate 资产沿用 common_v1 并记录三套候选研究数据。
P1-5 更正: 宽度来自最新成分快照; 快照缺失/陈旧时降级标注, 不得静默用旧数据标记完整。
旧日志(2026-08-05, 阶段B原版)保留并标记为旧版本, 不计入新的观察期。
"""
import json, os, sys, datetime as dt
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtest_v2 as bt
import portfolio_v2 as pf
import v2_config as cfg
import v2_daily as vd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_FILE = os.path.join(ROOT, "data", "shadow_log.json")
SNAP_DIR = os.path.join(ROOT, "data", "cons_snapshots")

FWD_WEEKS = (1, 4, 12)


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


def load_log():
    if not os.path.exists(LOG_FILE):
        return {}
    try:
        return json.load(open(LOG_FILE, encoding="utf-8"))
    except Exception as e:
        print(f"[shadow_log] 旧日志读取失败, 重建: {e}")
        return {}


def ensure_meta(log, today):
    """初始化/校验 meta; 候选或核心规则变化时必须升级观察期(P1-3.8)。"""
    meta = log.get("meta")
    new_obs = False
    if not meta:
        meta = {"observation_id": f"obs-{today}", "strategy_version": cfg.STRATEGY_VERSION,
                "observation_start": today, "candidate_map": dict(cfg.CANDIDATE_MAP)}
        new_obs = True
    else:
        if meta.get("strategy_version") != cfg.STRATEGY_VERSION or \
           meta.get("candidate_map") != cfg.CANDIDATE_MAP:
            old_id = meta.get("observation_id", "?")
            meta = {"observation_id": f"obs-{today}", "strategy_version": cfg.STRATEGY_VERSION,
                    "observation_start": today, "candidate_map": dict(cfg.CANDIDATE_MAP),
                    "superseded_observation": old_id,
                    "superseded_note": "候选或核心规则变更, 按 P1-3.8 升级策略版本并重新开始观察期"}
            new_obs = True
    # 标记旧版本记录(阶段B原版: 无 strategy_version/candidate 字段)
    if "legacy_dates" not in meta:
        legacy = []
        for k, v in log.items():
            if k == "meta":
                continue
            entries = v.get("entries", {}) if isinstance(v, dict) else {}
            if entries and "candidate" not in next(iter(entries.values())):
                legacy.append(k)
        meta["legacy_dates"] = legacy
        meta["legacy_note"] = "旧日志(2026-08-05, 阶段B原版): 回测执行口径错误且跨端规则不一致, 不计入新的8-12周观察期"
    log["meta"] = meta
    return new_obs


def _fwd_returns(df, actions, signal_day, weeks=FWD_WEEKS):
    """从 signal_day 起 N 周后: 策略收益(按给定 actions 模拟) 与 始终持有收益。

    日期未到(数据不足)时返回 None; 不用未来数据生成当期信号(信号只由当日及之前数据决定)。"""
    if signal_day not in df.index:
        return None, None
    i0 = df.index.get_loc(signal_day)
    res = bt.simulate(df, actions, cost=0.001, delay=1, exec_mode="close")
    nav0 = float(res["strat_nav"].iloc[i0])
    bh0 = float(res["bh_nav"].iloc[i0])
    out_s, out_b = {}, {}
    for w in weeks:
        target = i0 + w * 5   # 约 N 周后的交易日(按5交易日/周取近似位置)
        if target >= len(df.index):
            continue
        out_s[f"fwd_{w}w"] = float(res["strat_nav"].iloc[target]) / nav0 - 1
        out_b[f"bh_{w}w"] = float(res["bh_nav"].iloc[target]) / bh0 - 1
    return out_s, out_b


def main():
    assets = bt.build_assets()
    today = dt.date.today().isoformat()
    snap, snap_date = vd.latest_snapshot()
    dollar_now, dollar_history = vd.dollar_data()
    fetched_at = dt.datetime.now().isoformat(timespec="seconds")

    log = load_log()
    new_obs = ensure_meta(log, today)
    entries = {}
    health = {}
    signals = {}
    prev_actions = {}

    # 上一周影子结果(用于"信号反复"判断)
    for k in sorted(log.keys()):
        if k == "meta":
            continue
        for name, e in (log.get(k, {}).get("entries", {}) or {}).items():
            if e.get("candidate") and "shadow" in e:
                prev_actions.setdefault(name, []).append(e["shadow"])

    for name, df in assets.items():
        health[name] = {"days": len(df), "first": str(df.index.min().date()),
                        "last": str(df.index.max().date()),
                        "pe_valid": int(df["pe"].notna().sum()) if "pe" in df else None,
                        "us10y_valid": int(df["us10y"].notna().sum()) if "us10y" in df else None}
        day = vd.last_eval_day(df, dt.date.today())
        candidate = cfg.CANDIDATE_MAP.get(name, "no_candidate")
        formal = bt.common_v1_actions(df, name).get(day, "数据不足")
        sig = vd.build_signal_for(name, df, day, candidate, snap, snap_date,
                                  dollar_now, dollar_history, fetched_at)
        if candidate == "no_candidate":
            sig["action"] = formal
            sig["action_reason"] = "无候选通过验收门槛，影子观察沿用正式策略(common_v1)"
            sig["candidates_research"] = {
                c: bt.v2_signal_at(df, name, c, day)["action"] for c in ("value", "balanced", "trend")}
        shadow = sig["action"]
        signals[name] = sig
        flapping = bool(prev_actions.get(name) and prev_actions[name][-1] not in ("数据不足",) and prev_actions[name][-1] != shadow)

        entry = {
            "strategy_version": cfg.STRATEGY_VERSION,
            "candidate": candidate,
            "signal_date": str(day.date()),
            "price_date": sig.get("price_date"),
            "price": round(float(sig.get("spot", df["close"].iloc[df.index.get_loc(day)])), 4),
            "formal": formal,
            "shadow": shadow,
            "match": formal == shadow,
            "reason": diff_reason(name, formal, shadow, sig),
            "flapping": flapping,
            "valuation_score": sig.get("valuation_score"),
            "macro_score": sig.get("macro_score"),
            "breadth_score": sig.get("breadth_score"),
            "earnings_score": sig.get("earnings_score"),
            "trend_score": sig.get("trend_score"),
            "momentum_score": sig.get("momentum_score"),
            "structural_score": sig.get("structural_score"),
            "technical_score": sig.get("technical_score"),
            "composite_score": sig.get("composite_score"),
            "available_weight_ratio": sig.get("available_weight_ratio"),
            "data_quality": sig.get("data_quality"),
            "degraded_fields": sig.get("degraded_fields"),
            "data_sources": sig.get("data_sources"),
            "class_rank": sig.get("class_rank"),
            "risk_cluster": sig.get("risk_cluster"),
        }
        # 补写后续收益(日期到达后才算, 用当时已记录的信号序列)
        acts_shadow = {}
        for k in sorted(log.keys()):
            if k == "meta":
                continue
            e = log[k]["entries"].get(name)
            if e and e.get("candidate"):
                try:
                    acts_shadow[pd.Timestamp(e["signal_date"])] = e["shadow"]
                except Exception:
                    pass
        acts_shadow[day] = shadow
        fwd_s, fwd_b = _fwd_returns(df, acts_shadow, day)
        if fwd_s:
            entry.update(fwd_s)
        if fwd_b:
            entry.update(fwd_b)
        # 同期 common_v1 策略收益
        fwd_c, _ = _fwd_returns(df, bt.common_v1_actions(df, name), day)
        if fwd_c:
            entry.update({f"common_v1_{k}": v for k, v in fwd_c.items()})
        entries[name] = entry

    # 组合层: 类内排名 + 风险提示
    warnings = pf.apply_portfolio(signals, {name: 0.3 for name in signals})
    for name, sig in signals.items():
        entries[name]["class_rank"] = sig.get("class_rank")

    try:
        fund = json.load(open(os.path.join(ROOT, "data", "fund.json"), encoding="utf-8"))
        health["fund_us10y"] = fund.get("us10y")
    except Exception as e:
        health["fund_us10y"] = f"读取失败: {e}"

    # 防止覆盖同名旧版本记录: 若今天键原本是旧格式(无 candidate), 先归档到 legacy/<date>
    old_today = log.get(today)
    log[today] = {"date": today, "entries": entries, "warnings": warnings, "health": health}
    if old_today and old_today.get("entries"):
        first = next(iter(old_today["entries"].values()))
        if "candidate" not in first:
            log[f"legacy/{today}"] = old_today
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=1)

    # 周报
    meta = log["meta"]
    print("=" * 82)
    print(f"影子观察周报  {today}  观察期 {meta.get('observation_id')} (共 {len([k for k in log if k != 'meta'])} 周记录)")
    if new_obs:
        print(f"⚠ 新观察期开始: 原观察期 {meta.get('superseded_observation', '—')} 作废(候选/规则变更)")
    if meta.get("legacy_dates"):
        print(f"⚠ 旧版本记录已标记(不计入新观察期): {', '.join(meta['legacy_dates'])}")
    print("=" * 82)
    print(f"{'资产':<8}{'候选':<14}{'正式':<6}{'影子':<6}{'反复':<4}{'一致':<4}{'质量':<6} 说明")
    for name, e in entries.items():
        mark = "✓" if e["match"] else "✗"
        flap = "是" if e["flapping"] else ""
        print(f"{name:<8}{e['candidate']:<14}{e['formal']:<6}{e['shadow']:<6}{flap:<4}{mark:<4}{e['data_quality']:<6}{e['reason']}")
    print("-" * 82)
    if warnings:
        for w in warnings:
            print("⚠", w)
    else:
        print("组合层：暂无风险集中提示")
    print("=" * 82)
    print("数据健康(最近交易日):", {k: v["last"] if isinstance(v, dict) else v for k, v in health.items() if k != "fund_us10y"})
    print(f"已追加到 {LOG_FILE}")


if __name__ == "__main__":
    main()
