# -*- coding: utf-8 -*-
"""生成版本化的 V2 影子信号 data/v2_signals.json(2026-08 更正版, P1-2 推荐方案)。

原则:
- Python 是 V2 信号唯一计算源; 页面只读取 data/v2_signals.json 并展示, 不再自行计算买入/持有/卖出。
- 使用本周最近一个评估日(周频, 周五收盘; 若周五无数据取最近交易日)。
- 宽度来自 data/cons_snapshots/<date>.json(白酒/医疗/科创50/中证500); 无快照时标"宽度缺失/陈旧"。
- 黄金宏观 = 美债10Y方向 + 美元指数方向(美元无历史时降级标注, 不假装完整)。
- 盈利周期无可靠数据 -> 显式标记"盈利周期缺失", 不得按 0 分处理。
- 每个外部字段携带 source/data_date/fetched_at/staleness_days/status/fallback_source。
- 组合层(类内排名+风险提示)由 portfolio_v2 计算并写入同一 JSON。
- 输出双写: data/v2_signals.json + web/data/v2_signals.json(与 fund.json 双写同规则)。
"""
import json
import os
import sys
import datetime as dt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtest_v2 as bt
import category_v2 as v2
import datahub
import portfolio_v2 as pf
import v2_config as cfg

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_MAIN = os.path.join(ROOT, "data", "v2_signals.json")
OUT_WEB = os.path.join(ROOT, "web", "data", "v2_signals.json")
SNAP_DIR = os.path.join(ROOT, "data", "cons_snapshots")

# 哪些指数使用宽度快照(与 snapshot_cons.py 的 CONS_INDEXES 对应, 中证500 为更正版新增)
BREADTH_INDEXES = {
    "中证500": "000905",
    "中证白酒": "399997",
    "中证医疗": "399989",
    "科创50": "000688",
}

FIELD_SOURCES = {
    "price": {"source": "腾讯/中证日线", "fallback_source": None},
    "valuation": {"source": "中证官网滚动PE", "fallback_source": None},
    "pb": {"source": "乐咕沪深300PB", "fallback_source": None},
    "erp": {"source": "中证PE+新浪中债10Y", "fallback_source": None},
    "us10y": {"source": "新浪美债10Y", "fallback_source": "bond_zh_us_rate(英为)"},
    "dollar": {"source": "新浪DINIW实时", "fallback_source": None},
    "breadth": {"source": "成分股宽度快照", "fallback_source": None},
    "earnings": {"source": "东财F10盈利周期", "fallback_source": None},
}


def last_eval_day(df, today=None):
    """最近一个周五评估日(周频); 若今天就是周五, 用今天/最近交易日。"""
    today = today or dt.date.today()
    idx = df.index
    candidates = idx[idx <= pd_timestamp(today)]
    if len(candidates) == 0:
        return idx[-1]
    # 取周五(或周五前最近交易日); 若今天恰好周五取今天
    weekday = today.weekday()
    offset = (weekday - 4) % 7     # 距最近周五的天数(4=周五)
    target = today - dt.timedelta(days=offset)
    before = idx[idx <= pd_timestamp(target)]
    return before[-1] if len(before) else idx[-1]


def pd_timestamp(d):
    import pandas as pd
    return pd.Timestamp(d)


def latest_snapshot():
    """读取最新的成分宽度快照。返回 (snapshot_dict, date_str) 或 (None, None)。"""
    if not os.path.isdir(SNAP_DIR):
        return None, None
    files = sorted(f for f in os.listdir(SNAP_DIR) if f.endswith(".json"))
    if not files:
        return None, None
    latest = files[-1]
    try:
        snap = json.load(open(os.path.join(SNAP_DIR, latest), encoding="utf-8"))
        return snap, latest[:-5]
    except Exception as e:
        print(f"[v2_daily] 宽度快照读取失败: {e}")
        return None, latest[:-5]


def snapshot_asof(day):
    """选择日期不晚于信号日的最近宽度快照(v4 严格 as_of=signal_date 截止)。

    快照只能选择日期 <= 信号日的数据; 只有晚于信号日的快照时返回 (None, None),
    调用方按\"宽度缺失\"降级, 绝不允许把未来快照混入评分。"""
    day_str = str(day)[:10]
    if not os.path.isdir(SNAP_DIR):
        return None, None
    files = sorted(f for f in os.listdir(SNAP_DIR) if f.endswith(".json"))
    best = None
    for f in files:
        d = f[:-5]
        if d <= day_str:
            best = f
        else:
            break
    if best is None:
        return None, None
    try:
        with open(os.path.join(SNAP_DIR, best), encoding="utf-8") as f:
            snap = json.load(f)
        return snap, best[:-5]
    except Exception as e:
        print(f"[v2_daily] 宽度快照读取失败({best}): {e}")
        return None, best[:-5]


def dollar_data():
    """美元指数: 当前值 + 历史序列(带日期, v4 保留日期供按信号日截断)。

    返回 (dollar_now, dollar_hist): dollar_now=实时当前值(仅供展示/降级提示),
    dollar_hist=带 DatetimeIndex 的收盘价 Series(尽力而为; 失败即 None ->
    引擎标注\"美元指数缺失\", 不假装完整)。"""
    dollar_now = None
    dollar_hist = None
    try:
        dollar_now = datahub.dollar_index()
    except Exception as e:
        print(f"[v2_daily] 美元指数获取失败: {e}")
    # 尝试历史: akshare 美元指数历史接口(尽力; 失败即 None -> 引擎标注\"仅当前值\")
    try:
        import akshare as ak
        if hasattr(ak, "index_investing_global"):
            df = ak.index_investing_global(country="美国", index_name="美元指数",
                                           period="每日", start_date="20240101", end_date=dt.date.today().strftime("%Y%m%d"))
            if df is not None and len(df) > 30:
                df = df.rename(columns={"日期": "日期", "收盘": "收盘"})
                date_col = "日期" if "日期" in df.columns else df.columns[0]
                val_col = "收盘" if "收盘" in df.columns else df.columns[1]
                hist = pd.Series(pd.to_numeric(df[val_col], errors="coerce").to_numpy(),
                                 index=pd.to_datetime(df[date_col]), dtype=float)
                hist = hist[~hist.isna()].sort_index()
                if len(hist) > 30:
                    dollar_hist = hist
    except Exception as e:
        print(f"[v2_daily] 美元指数历史不可用(使用当前值): {e}")
    return dollar_now, dollar_hist


def dollar_asof(dollar_hist, day):
    """把美元历史截断到信号日(v4): 只保留日期 <= day 的值。

    返回 (values, last_date): values=截断后的收盘价 list, last_date=<=day 的最近日期
    (无数据时 (None, None))。未来日期一律排除, 绝不进入黄金宏观评分。"""
    if dollar_hist is None or len(dollar_hist) == 0:
        return None, None
    upto = dollar_hist.loc[:pd_timestamp(day)]
    if len(upto) == 0:
        return None, None
    return [float(x) for x in upto.tolist()], upto.index[-1].date().isoformat()


def build_data_meta(signal_date, price_date, valuation_date, us10y_date, breadth_date, dollar_date, fetched_at):
    meta = {}
    base = {
        "price": {"data_date": str(price_date) if price_date else None, "fetched_at": fetched_at},
        "valuation": {"data_date": str(valuation_date) if valuation_date else None, "fetched_at": fetched_at},
        "pb": {"data_date": str(valuation_date) if valuation_date else None, "fetched_at": fetched_at},
        "erp": {"data_date": str(valuation_date) if valuation_date else None, "fetched_at": fetched_at},
        "us10y": {"data_date": str(us10y_date) if us10y_date else None, "fetched_at": fetched_at},
        "dollar": {"data_date": str(dollar_date) if dollar_date else None, "fetched_at": fetched_at},
        "breadth": {"data_date": str(breadth_date) if breadth_date else None, "fetched_at": fetched_at},
        "earnings": {"data_date": None, "fetched_at": fetched_at},
    }
    for field, info in FIELD_SOURCES.items():
        meta[field] = {**info, **base[field]}
    return meta


def build_signal_for(name, df, day, candidate, dollar_now, dollar_hist, fetched_at):
    """为单个资产构建完整 V2 信号(评估日 day, 候选 candidate)。

    供 v2_daily.main() 与 shadow_log.py 共用, 保证口径一致。
    v4 严格 as_of=signal_date 截止:
    - 宽度快照只选日期 <= day 的最近一份(snapshot_asof);
    - 美元历史保留日期并截断到 day(dollar_asof), 未来日期不入评分;
    - pb/erp(沪深300)携带各自真实数据日期进 data_dates/陈旧度协议。"""
    kwargs = dict(candidate=candidate, signal_date=str(day.date()))
    i = df.index.get_loc(day)
    close = df["close"].iloc[:i + 1]
    kwargs["close"] = close.tolist()
    kwargs["spot"] = float(close.iloc[-1])
    rsi = df["rsi"].iloc[i]
    kwargs["rsi_value"] = float(rsi) if not pd_isna(rsi) else None
    price_date = day.date()

    if name == "黄金":
        us = df["us10y"].iloc[:i + 1].dropna()
        kwargs["us10y_history"] = us.tolist()
        us10y_date = us.index[-1].date() if len(us) else None
        dollar_values, dollar_date = dollar_asof(dollar_hist, day)
        kwargs["dollar_history"] = dollar_values
        kwargs["dollar_now"] = float(dollar_values[-1]) if dollar_values else None
        data_dates = {"price": str(price_date), "us10y": str(us10y_date) if us10y_date else None,
                      "dollar": dollar_date}
    else:
        # 完整 PE 历史优先(红利低波等 ETF 帧: 估值分位用指数完整历史, 不只ETF窗口)
        pe_full = getattr(df, "attrs", {}).get("pe_full")
        if pe_full is not None:
            pe = pe_full.loc[:day].dropna()
        else:
            pe = df["pe"].iloc[:i + 1].dropna()
        kwargs["pe_history"] = pe.tolist()
        kwargs["pe_now"] = float(pe.iloc[-1]) if len(pe) else None
        valuation_date = pe.index[-1].date() if len(pe) else None
        data_dates = {"price": str(price_date),
                      "valuation": str(valuation_date) if valuation_date else None}
        if name == "沪深300":
            pb = df["pb"].iloc[:i + 1].dropna()
            erp = df["erp"].iloc[:i + 1].dropna()
            kwargs["pb_history"] = pb.tolist()
            kwargs["pb_now"] = float(pb.iloc[-1]) if len(pb) else None
            kwargs["erp_history"] = erp.tolist()
            kwargs["erp_now"] = float(erp.iloc[-1]) if len(erp) else None
            if len(pb):
                data_dates["pb"] = str(pb.index[-1].date())
            if len(erp):
                data_dates["erp"] = str(erp.index[-1].date())

    # 宽度(只选日期不晚于信号日的快照, v4)
    breadth_ratio = None
    if name in BREADTH_INDEXES:
        snap, snap_date = snapshot_asof(day.date())
        if snap is not None:
            info = snap.get("indexes", {}).get(name)
            if info and isinstance(info, dict) and info.get("above_ma250_ratio") is not None:
                breadth_ratio = info["above_ma250_ratio"]
        if breadth_ratio is not None:
            kwargs["breadth_ratio"] = breadth_ratio
            data_dates["breadth"] = snap_date or str(day.date())
        elif snap is not None:
            data_dates["breadth"] = snap_date or str(day.date())

    # 盈利周期: 无可靠数据 -> 不传(引擎显式标"盈利周期缺失", 不按0)
    kwargs["data_dates"] = data_dates
    kwargs["data_meta"] = build_data_meta(
        day.date(), price_date, data_dates.get("valuation"), data_dates.get("us10y"),
        data_dates.get("breadth"), data_dates.get("dollar"), fetched_at)
    return v2.compute_signal(name, **kwargs)


def main():
    fetched_at = dt.datetime.now().isoformat(timespec="seconds")
    assets = bt.build_assets()
    snap, snap_date = latest_snapshot()
    dollar_now, dollar_hist = dollar_data()

    today = dt.date.today()
    signals = {}
    vol_map = {}
    global_eval_day = None

    for name, df in assets.items():
        day = last_eval_day(df, today)
        if global_eval_day is None or day > global_eval_day:
            global_eval_day = day
        candidate = cfg.CANDIDATE_MAP.get(name, "no_candidate")
        sig = build_signal_for(name, df, day, candidate, dollar_now, dollar_hist, fetched_at)
        if candidate == "no_candidate":
            # 无候选通过验收门槛 -> 影子观察沿用正式策略(common_v1)(P1-3)。
            # 同时记录三套候选动作作为研究数据, 但不得事后挑表现最好的一套当主候选。
            # v4: 研究候选与正式影子信号使用完全相同的时点数据/陈旧度/类别规则
            #     (同一 build_signal_for, 含 as_of 快照与美元截断)。
            formal = bt.common_v1_actions(df, name).get(day, "数据不足")
            sig["action"] = formal
            sig["candidate"] = "no_candidate"
            sig["action_reason"] = "无候选通过验收门槛，影子观察沿用正式策略(common_v1)"
            sig["candidates_research"] = {
                c: build_signal_for(name, df, day, c, dollar_now, dollar_hist, fetched_at)
                for c in ("value", "balanced", "trend")}
        signals[name] = sig
        # 年化波动(供类内排名用): 用最近60日日收益年化
        close = df["close"].iloc[:df.index.get_loc(day) + 1]
        rets = close.pct_change().dropna().tail(60)
        vol_map[name] = float(rets.std() * (244 ** 0.5)) if len(rets) > 20 else 0.3

    # 恒生科技: 数据不足通道, 不伪造估值(8个指数全部覆盖)
    if "恒生科技" not in signals:
        hs = v2.compute_signal("恒生科技", close=None, spot=None,
                               candidate=cfg.CANDIDATE_MAP.get("恒生科技", "no_candidate"),
                               signal_date=str(global_eval_day.date() if global_eval_day else today))
        hs["candidate"] = cfg.CANDIDATE_MAP.get("恒生科技", "no_candidate")
        signals["恒生科技"] = hs
        vol_map["恒生科技"] = 0.3

    # 组合层: 类内排名 + 风险集中提示(只输出观察提示, 不输出仓位)
    warnings = pf.apply_portfolio(signals, vol_map)

    out = {
        "generated_at": fetched_at,
        "strategy_version": cfg.STRATEGY_VERSION,
        "signal_date": max((s["signal_date"] or "" for s in signals.values()), default=str(today)),
        "candidate_map": dict(cfg.CANDIDATE_MAP),
        "assets": {name: sig for name, sig in signals.items()},
        "warnings": warnings,
        "note": "V2 影子研究数据(影子观察, 不改变正式三态); 页面只读取展示, 不自行计算。"
                "v4: 所有信号严格 as_of=signal_date 截止(快照/美元/字段日期), 未来数据标记 future 不入评分。",
    }
    for path in (OUT_MAIN, OUT_WEB):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)

    # 打印摘要
    print(f"已生成 {OUT_MAIN}")
    print(f"信号评估日: {out['signal_date']}  宽度快照(as_of): {snap_date or '无'}")
    print(f"{'资产':<8}{'候选':<14}{'动作':<8}{'质量':<8}{'可用权重':>8}  说明")
    for name, sig in signals.items():
        print(f"{name:<8}{sig['candidate']:<14}{sig['action']:<8}{sig['data_quality']:<8}"
              f"{sig['available_weight_ratio']:>8.2f}  {','.join(sig['degraded_fields'][:4])}")
    if warnings:
        print("组合提示:")
        for w in warnings:
            print("  ⚠", w)


def pd_isna(x):
    import pandas as pd
    return bool(pd.isna(x))


if __name__ == "__main__":
    main()
