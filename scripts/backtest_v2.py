# -*- coding: utf-8 -*-
"""category_v2_shadow 回测: 每类指数 5 方案对比(任务书 §13)。

对比: common_v1 / 始终持有 / 简单200日趋势 / 估值单因子 / category_v2_shadow(三套候选)
统一假设: 周频判断(每周最后交易日), 信号次日执行, 单边成本 0.1%(可调),
          ETF 成立前数据标为指数模拟研究期, 不使用未来数据。
恒生科技: 阶段 B 不参与回测(数据不足通道)。
宽度: 无历史成分快照, 历史回测不含宽度(与线上 shadow 当前截面观察分开标注)。
"""
import os, sys, math
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import datahub
import signal_rules as rules
import category_v2 as v2
import v2_config as cfg

TRADING_DAYS = 244
COST = 0.001


# ---------------------------------------------------------------- 数据
def percentile_series(s, window=rules.VALUE_WINDOW):
    """滚动窗口百分位(0-1), 前60有效值前为 NaN(与 backtest.py 同口径)。"""
    vals = pd.to_numeric(pd.Series(s), errors="coerce").to_numpy(dtype=float)
    out = np.full(len(vals), np.nan)
    for i in range(len(vals)):
        lo = max(0, i - window + 1)
        if pd.isna(vals[i]):
            continue
        sample = vals[lo:i + 1]
        sample = sample[~pd.isna(sample)]
        if len(sample) < rules.VALUE_MIN_PERIODS:
            continue
        out[i] = (sample < vals[i]).mean()
    return pd.Series(out, index=getattr(s, "index", pd.RangeIndex(len(s))))


def build_assets():
    """返回 {资产名: frame}。frame 列: close, pe, pb(300), erp(300), us10y(黄金), rsi, ma20, ma60"""
    assets = {}

    def load_close_index(sym, cache_name):
        df = datahub.index_history(sym, cache_name).set_index("日期")
        df = df[["收盘", "滚动市盈率"]].rename(columns={"收盘": "close", "滚动市盈率": "pe"})
        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        df["pe"] = pd.to_numeric(df["pe"], errors="coerce")
        return df

    # 沪深300
    df300 = load_close_index("000300", "hist_300")
    pb_df = datahub.pb_lg()
    if pb_df is not None:
        pb = pb_df.set_index("日期")["市净率"]
        df300["pb"] = pb.reindex(df300.index).astype(float)
    cn10y = datahub.bond_cn().set_index("日期")["收益率"].astype(float)
    df300["erp"] = (100 / df300["pe"] - cn10y.reindex(df300.index)).astype(float)
    assets["沪深300"] = df300.dropna(subset=["close"])

    # 中证500 / 科创50 / 白酒 / 医疗: 通用 PE
    for name, sym, cache in [("中证500", "000905", "hist_index_000905"),
                             ("科创50", "000688", "hist_index_000688"),
                             ("中证白酒", "399997", "hist_index_399997"),
                             ("中证医疗", "399989", "hist_index_399989")]:
        df = load_close_index(sym, cache)
        assets[name] = df.dropna(subset=["close"])

    # 红利低波: ETF 前复权价格 + 指数 PE(2019前为指数模拟研究期)
    hist_hl, hl_src = datahub.hist_etf_research("512890")
    df_hl = hist_hl.set_index("日期")[["收盘"]].rename(columns={"收盘": "close"})
    value_hl = datahub.hist_hl().set_index("日期")["滚动市盈率"].astype(float)
    df_hl["pe"] = value_hl.reindex(df_hl.index)
    assets["红利低波"] = df_hl.dropna(subset=["close"])

    # 黄金: ETF 前复权价格 + 美债10Y
    hist_au, au_src = datahub.hist_etf_research("518880")
    df_au = hist_au.set_index("日期")[["收盘"]].rename(columns={"收盘": "close"})
    us10y = datahub.bond_us().set_index("日期")["收益率"].astype(float)
    df_au["us10y"] = us10y.reindex(df_au.index)
    assets["黄金"] = df_au.dropna(subset=["close"])

    # 共同技术指标
    for name, df in assets.items():
        df["ma20"] = df["close"].rolling(20).mean()
        df["ma60"] = df["close"].rolling(60).mean()
        df["rsi"] = pd.Series(rsi_series(df["close"]).values, index=df.index)
        df["chg5"] = df["close"] / df["close"].shift(5) - 1
        df["ret"] = df["close"].pct_change()
    return assets


def rsi_series(close):
    """Wilder RSI 序列(与 daily_signal.rsi 同口径的向量版)。"""
    import daily_signal as ds
    values = pd.Series(close, dtype=float)
    return ds.rsi(values)


# ---------------------------------------------------------------- 策略动作
def common_v1_actions(df, asset_name):
    """common_v1 三态(页面正式口径): final_action(V/T/M + 160/200/240共识)。"""
    actions = {}
    idx = df.index
    for i, day in enumerate(idx):
        if i < 60:
            continue
        v = None
        if asset_name == "黄金":
            if i >= 249:
                ma250 = df["close"].iloc[i - 249:i + 1].mean()
                bias = (df["close"].iloc[i] / ma250 - 1) * 100
                v = rules.gold_bias_score(bias)
        elif asset_name == "沪深300":
            pe_h = df["pe"].iloc[:i + 1].dropna()
            pb_h = df["pb"].iloc[:i + 1].dropna() if "pb" in df else pd.Series(dtype=float)
            erp_h = df["erp"].iloc[:i + 1].dropna() if "erp" in df else pd.Series(dtype=float)
            v = rules.hs300_value_score(
                pe_h, float(pe_h.iloc[-1]) if len(pe_h) else float("nan"),
                pb_h, float(pb_h.iloc[-1]) if len(pb_h) else float("nan"),
                erp_h, float(erp_h.iloc[-1]) if len(erp_h) else float("nan"),
            )[0]
        else:
            pe_h = df["pe"].iloc[:i + 1].dropna()
            v = rules.valuation_percentile_score(pe_h, float(pe_h.iloc[-1]) if len(pe_h) else float("nan"))
        close = df["close"]
        t = 2 if (close.iloc[i] > df["ma20"].iloc[i] and df["ma20"].iloc[i] > df["ma60"].iloc[i]) else \
            1 if (close.iloc[i] > df["ma20"].iloc[i] and df["ma20"].iloc[i] <= df["ma60"].iloc[i]) else \
            -1 if (close.iloc[i] <= df["ma20"].iloc[i] and df["ma20"].iloc[i] > df["ma60"].iloc[i]) else -2
        m = ds_score_momentum(df["rsi"].iloc[i], df["chg5"].iloc[i] if pd.notna(df["chg5"].iloc[i]) else None)
        long_avg, long_votes = None, None
        if i >= max(rules.LONG_TREND_HORIZONS):
            moves = [close.iloc[i] / close.iloc[i - h] - 1 for h in rules.LONG_TREND_HORIZONS]
            long_avg = sum(moves) / len(moves)
            long_votes = sum(1 for x in moves if x > 0)
        if None in (v, t, m, long_avg, long_votes):
            actions[day] = "数据不足"
            continue
        action, _ = rules.final_action(v, t, m, long_avg, long_votes)
        actions[day] = action
    return actions


def ds_score_momentum(rsi_value, chg5):
    """与 daily_signal.score_momentum_values 相同, 供 common_v1 复现。"""
    import daily_signal as ds
    return ds.score_momentum_values(rsi_value, chg5)


def v2_signal_at(df, asset_name, candidate, day):
    """评估日 day 的完整 v2 信号 dict(供回测与影子日志共用)。"""
    i = df.index.get_loc(day)
    close = df["close"].iloc[:i + 1]
    kwargs = dict(
        close=close.tolist(), spot=float(close.iloc[-1]),
        rsi_value=float(df["rsi"].iloc[i]) if pd.notna(df["rsi"].iloc[i]) else None,
        candidate=candidate, signal_date=str(day.date()),
    )
    if asset_name == "黄金":
        us = df["us10y"].iloc[:i + 1].dropna()
        kwargs.update(us10y_history=us.tolist())
    else:
        pe = df["pe"].iloc[:i + 1].dropna()
        kwargs.update(pe_history=pe.tolist(),
                      pe_now=float(pe.iloc[-1]) if len(pe) else None)
        if asset_name == "沪深300":
            pb = df["pb"].iloc[:i + 1].dropna()
            erp = df["erp"].iloc[:i + 1].dropna()
            kwargs.update(pb_history=pb.tolist(),
                          pb_now=float(pb.iloc[-1]) if len(pb) else None,
                          erp_history=erp.tolist(),
                          erp_now=float(erp.iloc[-1]) if len(erp) else None)
    return v2.compute_signal(asset_name, **kwargs)


def v2_shadow_actions(df, asset_name, candidate):
    """category_v2_shadow 动作序列(周频评估日计算, 其余日沿用)。"""
    actions = {}
    idx = df.index
    weeks = pd.Series(idx.to_period("W-FRI"), index=idx)
    week_end = weeks.ne(weeks.shift(-1))
    eval_days = idx[week_end]
    for day in eval_days:
        sig = v2_signal_at(df, asset_name, candidate, day)
        actions[day] = sig["action"]
    return actions


def trend200_actions(df):
    actions = {}
    close = df["close"]
    for i, day in enumerate(df.index):
        if i < 200 or pd.isna(close.iloc[i]) or pd.isna(close.iloc[i - 200]):
            actions[day] = "数据不足"
        elif close.iloc[i] > close.iloc[i - 200]:
            actions[day] = "买入"
        else:
            actions[day] = "卖出"
    return actions


def valuation_only_actions(df):
    """估值单因子: PE 5年分位 < 30% 持有(便宜), >= 70% 卖出(贵), 其余维持。
    黄金无 PE, 用 250 日均线乖离作为价格位置单因子(便宜=乖离<-5% 持有)。"""
    actions = {}
    if "pe" not in df.columns:
        # 黄金: 250 日线位置单因子
        close = df["close"]
        for i, day in enumerate(df.index):
            if i < 249:
                actions[day] = "数据不足"
                continue
            ma250 = close.iloc[i - 249:i + 1].mean()
            bias = (close.iloc[i] / ma250 - 1) * 100
            if bias < -5:
                actions[day] = "买入"
            elif bias > 5:
                actions[day] = "卖出"
            else:
                actions[day] = "持有"
        return actions
    pe_pct = percentile_series(df["pe"])
    for i, day in enumerate(df.index):
        p = pe_pct.iloc[i]
        if pd.isna(p):
            actions[day] = "数据不足"
        elif p < 0.30:
            actions[day] = "买入"
        elif p >= 0.70:
            actions[day] = "卖出"
        else:
            actions[day] = "持有"
    return actions


# ---------------------------------------------------------------- 模拟
def simulate(df, actions, cost=COST, delay=1):
    """周频动作 -> 次日执行持仓模拟。返回带 pos/ret/nav 的 DataFrame。"""
    idx = df.index
    pos = pd.Series(0.0, index=idx)
    holding = False
    for i, day in enumerate(idx):
        a = actions.get(day)
        if a == "买入":
            holding = True
        elif a == "卖出":
            holding = False
        pos.iloc[i] = 1.0 if holding else 0.0
    effective = pos.shift(delay).fillna(0)
    turnover = pos.diff().abs().fillna(pos.abs()).shift(delay).fillna(0)
    ret = effective * df["ret"].fillna(0) - turnover * cost
    out = df[["ret"]].copy()
    out["pos"] = effective
    out["turnover"] = turnover
    out["strat_ret"] = ret
    out["strat_nav"] = (1 + ret.fillna(0)).cumprod()
    out["bh_nav"] = (1 + df["ret"].fillna(0)).cumprod()
    return out


def metrics(result, start=None, end=None):
    sample = result.loc[start:end].dropna(subset=["ret"]) if start is not None else result.dropna(subset=["ret"])
    if len(sample) < 100:
        return None
    ret = sample["strat_ret"].fillna(0)
    nav = sample["strat_nav"]
    years = len(ret) / TRADING_DAYS
    ann = nav.iloc[-1] ** (1 / years) - 1 if years > 0 else 0
    mdd = (nav / nav.cummax() - 1).min()
    vol = ret.std() * math.sqrt(TRADING_DAYS)
    sharpe = ret.mean() / ret.std() * math.sqrt(TRADING_DAYS) if ret.std() > 0 else float("nan")
    bh = sample["bh_nav"]
    bh_ann = bh.iloc[-1] ** (1 / years) - 1 if years > 0 else 0
    bh_mdd = (bh / bh.cummax() - 1).min()
    return dict(days=len(sample), ann=ann, mdd=mdd, vol=vol, sharpe=sharpe,
                bh_ann=bh_ann, bh_mdd=bh_mdd,
                invested=sample["pos"].mean(), turnover=sample["turnover"].sum())


def fmt(v, kind="pct"):
    if v is None or (isinstance(v, float) and (pd.isna(v) or math.isnan(v))):
        return "—"
    if kind == "num":
        return f"{v:.2f}"
    return f"{v * 100:.1f}%"


# ---------------------------------------------------------------- 主流程
def main():
    assets = build_assets()
    print("=" * 100)
    print("category_v2_shadow 回测 (周频评估 / 次日执行 / 单边成本 %.1f%%)" % (COST * 100))
    print("=" * 100)

    all_rows = []
    for name, df in assets.items():
        category, _ = cfg.CATEGORY_MAP[name]
        print(f"\n### {name} [{category}]  {df.index.min().date()} ~ {df.index.max().date()} ({len(df)}日)")
        if name == "恒生科技":
            print("  数据不足通道, 不参与回测收益对比")
            continue
        schemes = {}
        schemes["common_v1"] = simulate(df, common_v1_actions(df, name))
        schemes["始终持有"] = simulate(df, {d: "买入" for d in df.index})
        schemes["200日趋势"] = simulate(df, trend200_actions(df))
        schemes["估值单因子"] = simulate(df, valuation_only_actions(df))
        for cand in ("value", "balanced", "trend"):
            schemes[f"v2_{cand}"] = simulate(df, v2_shadow_actions(df, name, cand))

        print(f"{'方案':<16}{'年化':>9}{'回撤':>9}{'波动':>9}{'夏普':>8}{'持有年化':>10}{'持有回撤':>10}{'投入':>8}{'换手':>8}")
        for sname, result in schemes.items():
            stat = metrics(result)
            if stat is None:
                print(f"{sname:<16}  样本不足")
                continue
            print(f"{sname:<16}{fmt(stat['ann']):>9}{fmt(stat['mdd']):>9}{fmt(stat['vol']):>9}"
                  f"{fmt(stat['sharpe'], 'num'):>8}{fmt(stat['bh_ann']):>10}{fmt(stat['bh_mdd']):>10}"
                  f"{fmt(stat['invested']):>8}{fmt(stat['turnover'], 'num'):>8}")
            all_rows.append(dict(asset=name, scheme=sname, **stat))

    print("\n" + "=" * 100)
    print("成本敏感度(单边成本 0.1% / 0.3% / 0.5%) — 取各指数 v2_balanced 汇总")
    print("=" * 100)
    for cost in (0.001, 0.003, 0.005):
        total = {}
        for name, df in assets.items():
            if name == "恒生科技":
                continue
            result = simulate(df, v2_shadow_actions(df, name, "balanced"), cost=cost)
            stat = metrics(result)
            if stat:
                total[name] = stat
        anns = [s["ann"] for s in total.values()]
        mdds = [s["mdd"] for s in total.values()]
        print(f"成本 {cost*100:.1f}%: 各指数平均年化 {sum(anns)/len(anns)*100:.1f}%  平均回撤 {sum(mdds)/len(mdds)*100:.1f}%")


if __name__ == "__main__":
    main()
