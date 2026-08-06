# -*- coding: utf-8 -*-
"""影子观察日志(阶段 C, 2026-08 更正版 + v4 数据完整性修正): 每周记录 common_v1 vs category_v2_shadow 对比。

v4(2026-08-06)关键变更:
- 严格 as_of=signal_date 截止: 宽度快照只选日期 <= 信号日的最近一份; 美元历史保留日期并截断到
  信号日; 数据日期晚于信号日标记 future/invalid 不得进入评分(引擎层 enforcement)。
- 收益回填重写: 每次运行遍历全部历史观察记录, 到达 1/4/12 周(定义为 5/20/60 个交易日,
  自信号日后第 w*5 个交易日, 信号日当天不计)后补写对应旧记录; 只追加 fwd_*/bh_*/common_v1_* 字段,
  绝不重新计算或覆盖当时保存的历史信号(formal/shadow/分项/陈旧度)。
- 完整研究候选落盘: 每个 no_candidate 指数的 value/balanced/trend 三套候选信号(完整 dict)写入
  candidates_research; 候选策略与正式影子信号使用完全相同的时点数据/陈旧度/类别规则
  (同一 vd.build_signal_for)。8-12 周后可据此比较候选策略。
- 观察期归档: 候选/核心规则变更(STRATEGY_VERSION 升级)或显式重建基线时, 旧观察期全部记录
  移动到 archive/<observation_id>/<date>, 不混入新观察期; 旧记录保留原文不删除。

每周每个指数保存:
- strategy_version / candidate(主候选, 冻结) / signal_date / price_date / 当时价格
- 正式结果 / 影子结果 / 是否一致 / 差异原因
- 全部原始分项(valuation/macro/breadth/trend/momentum) + 结构轮/价格轮/综合分 + 可用权重占比
- 数据源及日期(data_sources) / 数据质量与降级字段
- 研究候选(三套完整信号, 同数据同规则)
- 后续 1/4/12 周收益 + 同期始终持有收益 + 同期 common_v1 收益(日期到达后补写, 不用未来数据生成当期信号)
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
TRADING_DAYS_PER_WEEK = 5   # 1/4/12 周 = 5/20/60 个交易日(信号日当天不计)

# V3 批次(2026-08-05)污染说明: 该批次由旧代码生成, 宽度快照/美元当前值晚于信号日混入评分
V3_CONTAMINATION_NOTE = ("V3批次(obs-2026-08-05)数据截止污染: 宽度快照(2026-08-05)与美元当前值"
                         "晚于信号日(2026-07-31)混入评分, 已整体归档作废; 重建观察基线后, "
                         "首个周五自动任务验证通过才正式起算8-12周观察期")


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
        with open(LOG_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[shadow_log] 旧日志读取失败, 重建: {e}")
        return {}


def save_log(log):
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=1)


def ensure_meta(log, today):
    """初始化/校验 meta; 候选或核心规则变化时必须升级观察期(P1-3.8 + v4 重建基线)。"""
    meta = log.get("meta")
    new_obs = False
    if not meta:
        meta = {"observation_id": f"obs-{today}", "strategy_version": cfg.STRATEGY_VERSION,
                "observation_start": today, "candidate_map": dict(cfg.CANDIDATE_MAP)}
        new_obs = True
    else:
        old_version = meta.get("strategy_version")
        if old_version != cfg.STRATEGY_VERSION or \
           meta.get("candidate_map") != cfg.CANDIDATE_MAP:
            old_id = meta.get("observation_id", "?")
            if old_version == "category_v2_shadow_v3":
                note = V3_CONTAMINATION_NOTE
            else:
                note = "候选或核心规则变更, 按 P1-3.8 升级策略版本并重新开始观察期"
            meta = {"observation_id": f"obs-{today}", "strategy_version": cfg.STRATEGY_VERSION,
                    "observation_start": today, "candidate_map": dict(cfg.CANDIDATE_MAP),
                    "superseded_observation": old_id,
                    "superseded_note": note}
            if "legacy_dates" in log.get("meta", {}):
                meta["legacy_dates"] = log["meta"]["legacy_dates"]
                meta["legacy_note"] = log["meta"].get("legacy_note", "")
            new_obs = True
    # 标记旧版本记录(阶段B原版: 无 strategy_version/candidate 字段)
    if "legacy_dates" not in meta:
        legacy = []
        for k, v in log.items():
            if k == "meta" or k.startswith(("legacy/", "archive/", "invalid/")):
                continue
            entries = v.get("entries", {}) if isinstance(v, dict) else {}
            if entries and "candidate" not in next(iter(entries.values())):
                legacy.append(k)
        meta["legacy_dates"] = legacy
        meta["legacy_note"] = "旧日志(2026-08-05, 阶段B原版): 回测执行口径错误且跨端规则不一致, 不计入新的8-12周观察期"
    log["meta"] = meta
    return new_obs


def active_record_keys(log, meta):
    """当前观察期的日期键(meta/legacy/archive 与早于 observation_start 的记录除外)。"""
    start = meta.get("observation_start", "")
    for k in sorted(log.keys()):
        if k == "meta" or k.startswith(("legacy/", "archive/", "invalid/")):
            continue
        if k < start:
            continue
        yield k


def archive_superseded(log):
    """把不属于当前观察期的日期记录移动到 archive/<observation_id>/<date>。

    触发: STRATEGY_VERSION 升级或显式重建基线后, observation_start 后移;
    旧观察期记录(日期 < observation_start)一律归档, 不混入新观察期。
    旧记录保留原文(只改存储位置), 不删除。返回移动条数。"""
    meta = log.get("meta", {})
    start = meta.get("observation_start", "")
    if not start:
        return 0
    moved = 0
    for k in [k for k in list(log.keys()) if k != "meta"]:
        if k.startswith(("legacy/", "archive/", "invalid/")):
            continue
        if k >= start:
            continue
        obs_id = meta.get("superseded_observation") or f"obs-archive-{k}"
        dest = f"archive/{obs_id}/{k}"
        log[dest] = log.pop(k)
        moved += 1
    return moved


# ---------------------------------------------------------------- 收益回填
def _fwd_returns(df, actions, signal_day, weeks=FWD_WEEKS):
    """从 signal_day 起 N 周后: 策略收益(按给定 actions 模拟) 与 始终持有收益。

    v4 定义: N 周 = N*5 个交易日(信号日之后第 N*5 个交易日, 信号日当天不计)。
    日期未到(数据不足)时返回 None; 不用未来数据生成当期信号(信号只由当日及之前数据决定)。"""
    if signal_day not in df.index:
        return None, None
    i0 = df.index.get_loc(signal_day)
    res = bt.simulate(df, actions, cost=0.001, delay=1, exec_mode="close")
    nav0 = float(res["strat_nav"].iloc[i0])
    bh0 = float(res["bh_nav"].iloc[i0])
    out_s, out_b = {}, {}
    for w in weeks:
        target = i0 + w * TRADING_DAYS_PER_WEEK
        if target >= len(df.index):
            continue
        out_s[f"fwd_{w}w"] = float(res["strat_nav"].iloc[target]) / nav0 - 1
        out_b[f"bh_{w}w"] = float(res["bh_nav"].iloc[target]) / bh0 - 1
    return out_s, out_b


def shadow_actions_from_log(log, meta, name):
    """从当前观察期记录构建影子动作序列(当时保存的 shadow, 绝不重算)。"""
    acts = {}
    for k in active_record_keys(log, meta):
        e = (log.get(k, {}).get("entries", {}) or {}).get(name)
        if not e or not e.get("candidate"):
            continue
        sd = e.get("signal_date")
        if not sd or e.get("shadow") is None:
            continue
        try:
            acts[pd.Timestamp(sd)] = e["shadow"]
        except Exception:
            pass
    return acts


def backfill_entry(entry, name, df, acts_shadow):
    """给单条记录补写到期收益(只追加 fwd_*/bh_*/common_v1_* 字段)。

    绝不修改历史信号字段(formal/shadow/分项/陈旧度等); 已存在的字段不覆盖。"""
    if df is None:
        return
    sd = entry.get("signal_date")
    if not sd or not entry.get("candidate"):
        return
    try:
        day = pd.Timestamp(sd)
    except Exception:
        return
    if day not in df.index:
        return
    need_s = any(f"fwd_{w}w" not in entry for w in FWD_WEEKS)
    need_b = any(f"bh_{w}w" not in entry for w in FWD_WEEKS)
    need_c = any(f"common_v1_{w}w" not in entry for w in FWD_WEEKS)
    if not (need_s or need_b or need_c):
        return
    fwd_s, fwd_b = _fwd_returns(df, acts_shadow, day)
    if need_s and fwd_s:
        for k, v in fwd_s.items():
            entry.setdefault(k, v)
    if need_b and fwd_b:
        for k, v in fwd_b.items():
            entry.setdefault(k, v)
    if need_c:
        fwd_c, _ = _fwd_returns(df, bt.common_v1_actions(df, name), day)
        if fwd_c:
            for w in FWD_WEEKS:
                k = f"fwd_{w}w"
                if k in fwd_c:
                    entry.setdefault(f"common_v1_{w}w", fwd_c[k])


def backfill_log(log, assets):
    """每次运行遍历全部历史观察记录, 到期(1/4/12 周)后回填对应旧记录。

    返回 (回填条数, 新增收益字段数)。"""
    meta = log.get("meta", {})
    filled = 0
    added = 0
    for k in active_record_keys(log, meta):
        record = log.get(k, {})
        entries = record.get("entries", {}) or {}
        for name, entry in entries.items():
            before = len(entry)
            acts = shadow_actions_from_log(log, meta, name)
            backfill_entry(entry, name, assets.get(name), acts)
            if len(entry) > before:
                filled += 1
                added += len(entry) - before
    return filled, added


# ---------------------------------------------------------------- 主流程
def main():
    assets = bt.build_assets()
    today = dt.date.today().isoformat()
    dollar_now, dollar_hist = vd.dollar_data()
    fetched_at = dt.datetime.now().isoformat(timespec="seconds")

    log = load_log()
    new_obs = ensure_meta(log, today)
    moved = archive_superseded(log)
    meta = log["meta"]
    entries = {}
    health = {}
    signals = {}

    # 上一周影子结果(仅当前观察期, 用于"信号反复"判断; 归档记录不参与)
    prev_actions = {}
    for k in active_record_keys(log, meta):
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
        sig = vd.build_signal_for(name, df, day, candidate, dollar_now, dollar_hist, fetched_at)
        # 完整研究候选(与正式影子信号同一时点数据/陈旧度/类别规则; 落盘后 8-12 周可比较)
        candidates_research = {
            c: vd.build_signal_for(name, df, day, c, dollar_now, dollar_hist, fetched_at)
            for c in ("value", "balanced", "trend")}
        if candidate == "no_candidate":
            sig["action"] = formal
            sig["action_reason"] = "无候选通过验收门槛，影子观察沿用正式策略(common_v1)"
        sig["candidates_research"] = candidates_research
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
            "candidates_research": candidates_research,
        }
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

    # 收益回填: 遍历全部历史观察记录, 到期补写(只追加, 不覆盖历史信号)
    filled, added = backfill_log(log, assets)
    save_log(log)

    # 周报
    print("=" * 82)
    print(f"影子观察周报  {today}  观察期 {meta.get('observation_id')} "
          f"(共 {len(list(active_record_keys(log, meta)))} 周记录)")
    if new_obs:
        print(f"⚠ 新观察期开始: 原观察期 {meta.get('superseded_observation', '—')} 作废({meta.get('superseded_note', '')})")
    if moved:
        print(f"⚠ 已归档 {moved} 条旧观察期记录 -> archive/{meta.get('superseded_observation', '')}/")
    if meta.get("legacy_dates"):
        print(f"⚠ 旧版本记录已标记(不计入新观察期): {', '.join(meta['legacy_dates'])}")
    if filled:
        print(f"✅ 收益回填: {filled} 条历史记录补写 {added} 个到期收益字段(1/4/12周=5/20/60交易日)")
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


def rebuild_baseline():
    """一次性: 归档当前观察期全部记录并重建基线(不写新周记录)。

    供数据完整性修复后重建观察基线用: 标记旧批次(如 obs-2026-08-05 V3)作废归档,
    建立新 observation_id; 首个周五自动任务(云端 shadow_weekly)从零积累新观察期。"""
    log = load_log()
    today = dt.date.today().isoformat()
    new_obs = ensure_meta(log, today)
    moved = archive_superseded(log)
    save_log(log)
    meta = log["meta"]
    print("=" * 82)
    print(f"观察基线重建  {today}")
    if new_obs:
        print(f"⚠ 新观察期: {meta.get('observation_id')}")
        print(f"  原观察期 {meta.get('superseded_observation', '—')} 作废: {meta.get('superseded_note', '')}")
    if moved:
        print(f"⚠ 已归档 {moved} 条旧观察期记录 -> archive/{meta.get('superseded_observation', '')}/")
    print(f"  当前活动记录: {len(list(active_record_keys(log, meta)))} 条")
    print("=" * 82)
    print("下次周五云端自动任务(shadow_weekly)将从新观察期零积累开始, 验证通过后才正式起算8-12周观察期")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "rebuild_baseline":
        rebuild_baseline()
    else:
        main()
