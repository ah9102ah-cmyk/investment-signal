# -*- coding: utf-8 -*-
"""
历史回测: 2013-01 至今,验证 daily_signal.py 的评分+信号规则
============================================================
规则: 偏强 S>=+2 且 T>=0 且 M>=-1; 偏弱 S<=-4 或 (T<=-2 且 V<=-1); 其余中性
回测口径(场外): T日收盘观察 -> T+1日起模拟持仓生效; 计入单边 0.1% 换手成本
数据层: datahub.py(中证官网+新浪系, 当日缓存)
"""
import sys, os
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import datahub
import daily_signal as ds

pd.set_option("display.width", 160)

# ---------------------------------------------------------------- 指标
def percentile_series(s, window=1250):
    """滚动窗口内百分位(0-1), 前60个有效值之前为NaN"""
    if not isinstance(s, np.ndarray):
        vals = s.values
        idx = s.index
    else:
        vals = s
        idx = pd.RangeIndex(len(s))
    out = np.full(len(vals), np.nan)
    for i in range(len(vals)):
        lo = max(0, i - window + 1)
        if i - lo < 60 or pd.isna(vals[i]):
            continue
        out[i] = (vals[lo:i + 1] < vals[i]).mean()
    return pd.Series(out, index=idx)

def pct_score(p):
    if pd.isna(p):
        return 0
    p = p * 100
    if p >= 70: return 2
    if p >= 50: return 1
    if p >= 30: return 0
    if p >= 15: return -1
    return -2

def prep(close):
    df = pd.DataFrame({"close": close})
    df["ma20"] = df["close"].rolling(20).mean()
    df["ma60"] = df["close"].rolling(60).mean()
    df["ma250"] = df["close"].rolling(250).mean()
    df["rsi"] = ds.rsi(df["close"])
    df["chg5"] = df["close"] / df["close"].shift(5) - 1
    return df

def run_backtest(df, v_scores, name, strong_s=2, weak_s=-4, strong_t=0, weak_extra=True,
                 cost_rate=0.001):
    """df 需含预计算列 ma20/ma60/rsi/chg5。v_scores: 每日 V 分 Series。
    固定参数: 偏强S>=2; 偏弱S<=-4或(T破位且V<=-1)。"""
    n = len(df)
    close = df["close"].values
    ma20 = df["ma20"].values
    ma60 = df["ma60"].values
    rsi_v = df["rsi"].values
    chg5_v = df["chg5"].values
    v_arr = v_scores.values
    sigs = []
    for i in range(n):
        if i < 60 or pd.isna(v_arr[i]):
            sigs.append("中性")
            continue
        v = int(v_arr[i])
        t = 2 if (close[i] > ma20[i] and ma20[i] > ma60[i]) else \
            1 if (close[i] > ma20[i] and ma20[i] <= ma60[i]) else \
            -1 if (close[i] <= ma20[i] and ma20[i] > ma60[i]) else -2
        r = rsi_v[i]
        c5 = chg5_v[i]
        m = ds.score_momentum_values(r, None if pd.isna(c5) else c5)
        s = v + t + m
        if s >= strong_s and t >= strong_t and m >= -1:
            sigs.append("偏强")
        elif s <= weak_s or (weak_extra and t <= -2 and v <= -1):
            sigs.append("偏弱")
        else:
            sigs.append("中性")
    df = df.copy()
    df["sig"] = sigs
    df["pos"] = 0
    holding = False
    for i in range(n):
        sig = df["sig"].iloc[i]
        if sig == "偏强":
            holding = True
        elif sig == "偏弱":
            holding = False
        df.loc[df.index[i], "pos"] = 1 if holding else 0
    df["pos_eff"] = df["pos"].shift(1).fillna(0)
    df["ret"] = df["close"].pct_change()
    df["turnover"] = df["pos"].diff().abs().fillna(df["pos"])
    df["strat_ret"] = df["pos_eff"] * df["ret"] - df["turnover"].shift(1).fillna(0) * cost_rate
    df["strat_nav"] = (1 + df["strat_ret"].fillna(0)).cumprod()
    df["bh_nav"] = (1 + df["ret"].fillna(0)).cumprod()
    return df

def stats(df, name):
    d = df.dropna(subset=["ret"])
    if len(d) < 100:
        return None
    days = len(d)
    years = days / 244
    def ann(nav):
        return nav.iloc[-1] ** (1 / years) - 1
    def mdd(nav):
        return (nav / nav.cummax() - 1).min()
    strat_ann, bh_ann = ann(d["strat_nav"]), ann(d["bh_nav"])
    strat_mdd, bh_mdd = mdd(d["strat_nav"]), mdd(d["bh_nav"])
    sig_series = d["sig"]
    trades, wins = 0, 0
    entry_nav = None
    nav_s = d["strat_nav"]
    for i in range(len(sig_series)):
        s = sig_series.iloc[i]
        if s == "偏强" and entry_nav is None:
            entry_nav = nav_s.iloc[i]
            trades += 1
        elif s == "偏弱" and entry_nav is not None:
            if nav_s.iloc[i] / entry_nav > 1:
                wins += 1
            entry_nav = None
    win_rate = wins / trades if trades else float("nan")
    hold_ratio = d["pos"].mean()
    return dict(name=name, days=days,
                strat_ann=strat_ann, bh_ann=bh_ann, strat_mdd=strat_mdd, bh_mdd=bh_mdd,
                trades=trades, win_rate=win_rate, hold_ratio=hold_ratio)

def momentum_rotation(px, mom_days=40, top_k=2, cash_below=0.0, cost_rate=0.001):
    """每周最后一个交易日观察; 仅纳入正动量资产,最多 top_k 个,等权模拟。"""
    rets = px.pct_change()
    pos = pd.DataFrame(0.0, index=px.index, columns=px.columns)
    mom = px / px.shift(mom_days) - 1
    cur = np.zeros(len(px.columns))
    for i in range(len(px)):
        is_week_end = i == len(px) - 1 or px.index[i].isocalendar()[:2] != px.index[i + 1].isocalendar()[:2]
        if i >= mom_days and is_week_end:
            m = mom.iloc[i]
            cur[:] = 0.0
            chosen = m[m > cash_below].nlargest(top_k).index
            if len(chosen):
                for c in chosen:
                    cur[px.columns.get_loc(c)] = 1.0 / len(chosen)
        pos.iloc[i] = cur
    turnover = pos.diff().abs().sum(axis=1).fillna(pos.abs().sum(axis=1))
    net_ret = (pos.shift(1) * rets).sum(axis=1) - turnover.shift(1).fillna(0) * cost_rate
    nav = (1 + net_ret).cumprod()
    return nav, pos

# ---------------------------------------------------------------- 数据准备
def build_assets():
    """返回 {资产名: (df含预计算指标, V分Series)}"""
    print("加载数据 ...")
    hist_hl = datahub.hist_hl().set_index("日期")
    hist_300 = datahub.hist_300().set_index("日期")
    hist_au = datahub.hist_au().set_index("日期")
    hist_au = hist_au[hist_au.index >= "2013-01-01"]
    cn10y_df = datahub.bond_cn()
    us10y_df = datahub.bond_us()
    pb_df = datahub.pb_lg()

    assets = {}

    # 红利低波: V=PE百分位(取反)
    df_hl = prep(hist_hl["收盘"])
    v_hl = percentile_series(hist_hl["滚动市盈率"]).map(pct_score) * -1
    assets["红利低波"] = (df_hl, v_hl)

    # 沪深300: V = PE + PB + 股债性价比
    df_300 = prep(hist_300["收盘"])
    idx = df_300.index
    pe_pct = pd.Series(percentile_series(hist_300["滚动市盈率"].reindex(idx).values).values, index=idx)
    v_pe = pe_pct.map(pct_score) * -1
    if pb_df is not None:
        pb_series = pb_df.set_index("日期")["市净率"].reindex(idx)
        pb_pct = pd.Series(percentile_series(pb_series.values).values, index=idx)
        v_pb = pb_pct.map(pct_score) * -1
    else:
        v_pb = pd.Series(0, index=idx)
        print("警告: PB 数据缺失, 沪深300 V分降级")
    ep = 100 / hist_300["滚动市盈率"].reindex(idx)
    erp = (ep - cn10y_df.set_index("日期")["收益率"].reindex(idx)).dropna()
    erp_pct_s = pd.Series(np.nan, index=idx)
    if len(erp) > 60:
        erp_pct_s = pd.Series(percentile_series(erp.values).values, index=erp.index)
    v_erp = erp_pct_s.map(pct_score)
    v_300 = (v_pe + v_pb + v_erp).round(0)
    assets["沪深300"] = (df_300, v_300)

    # 黄金: V = 乖离250日 + 美债方向
    df_au = prep(hist_au["收盘"])
    bias = (df_au["close"] / df_au["ma250"] - 1) * 100
    v_au_bias = bias.map(lambda b: 2 if b < -15 else 1 if b < -5 else 0 if b < 5 else -1 if b < 15 else -2)
    us_dir_s = pd.Series(0.0, index=df_au.index)
    if len(us10y_df) > 30:
        us = us10y_df.set_index("日期")["收益率"].reindex(df_au.index)
        us_chg = us - us.shift(20)
        us_dir_s = us_chg.map(lambda x: 1 if x < -0.05 else -1 if x > 0.05 else 0).fillna(0)
    v_au = (v_au_bias + us_dir_s.reindex(df_au.index, fill_value=0)).clip(-2, 2)
    assets["黄金"] = (df_au, v_au)
    return assets

# ---------------------------------------------------------------- 输出
def main():
    assets = build_assets()
    print("回测中 ...")
    results = {name: run_backtest(df, v, name) for name, (df, v) in assets.items()}

    rows = [stats(d, name) for name, d in results.items()]
    print()
    print("=" * 78)
    print("单资产回测  (2013 起 ~ 最新, 场外口径: 信号当日净值成交, 次日持仓生效)")
    print("=" * 78)
    print(f"{'资产':<8}{'天数':>6}{'策略年化':>10}{'持有年化':>10}{'策略回撤':>10}{'持有回撤':>10}{'交易数':>6}{'胜率':>8}{'持仓占比':>8}")
    print("-" * 78)
    for r in rows:
        wr = r['win_rate'] * 100 if not pd.isna(r['win_rate']) else float('nan')
        print(f"{r['name']:<8}{r['days']:>6}{r['strat_ann']*100:>9.1f}%{r['bh_ann']*100:>9.1f}%"
              f"{r['strat_mdd']*100:>9.1f}%{r['bh_mdd']*100:>9.1f}%{r['trades']:>6}{wr:>7.0f}%{r['hold_ratio']*100:>7.0f}%")
    print("-" * 78)

    common = None
    for name, d in results.items():
        nav = d[["strat_nav"]].rename(columns={"strat_nav": name})
        common = nav if common is None else common.join(nav, how="inner")
    combo_strat = common.mean(axis=1)
    common_bh = None
    for name, d in results.items():
        nav = d[["bh_nav"]].rename(columns={"bh_nav": name})
        common_bh = nav if common_bh is None else common_bh.join(nav, how="inner")
    combo_bh = common_bh.mean(axis=1)

    def combo_stats(nav, label):
        nav = nav.dropna()
        years = len(nav) / 244
        ann = nav.iloc[-1] ** (1 / years) - 1
        mdd = (nav / nav.cummax() - 1).min()
        print(f"{label:<10} 年化 {ann*100:>6.1f}%   最大回撤 {mdd*100:>6.1f}%")

    print()
    print("组合(三资产等权, 观察规则 vs 全程持有基准):")
    combo_stats(combo_strat, "策略组合")
    combo_stats(combo_bh, "持有组合")

    print()
    print("策略组合逐年收益 vs 持有组合:")
    yr = pd.DataFrame({"策略": combo_strat, "持有": combo_bh}).dropna()
    yr["year"] = yr.index.year
    yr_agg = yr.groupby("year").apply(
        lambda g: (g["策略"].iloc[-1] / g["策略"].iloc[0] - 1, g["持有"].iloc[-1] / g["持有"].iloc[0] - 1),
        include_groups=False)
    print(f"{'年份':<6}{'策略':>10}{'持有':>10}")
    for y, (s, h) in yr_agg.items():
        print(f"{y:<6}{s*100:>9.1f}%{h*100:>9.1f}%")

    print()
    print("判断天数分布(偏强/偏弱/中性):")
    for name, d in results.items():
        sig = d["sig"]
        print(f"  {name}: 偏强 {int((sig=='偏强').sum()):>4} 天, 偏弱 {int((sig=='偏弱').sum()):>4} 天, "
              f"中性 {int((sig=='中性').sum()):>4} 天")

    # ---------- 动量轮动(待持续验证的对照方案) ----------
    print()
    print("=" * 78)
    print("动量轮动(40日动量 / 最多2项 / 周末观察) — 待持续验证")
    print("=" * 78)
    px = pd.concat([d["close"].rename(n) for n, d in results.items()], axis=1, join="inner")
    mom_nav, mom_pos = momentum_rotation(px, 40, 2)
    rets = px.pct_change()
    bh_nav = (1 + rets.mean(axis=1)).cumprod()

    def nav_stats(nav, label):
        nav = nav.dropna()
        years = len(nav) / 244
        ann = nav.iloc[-1] ** (1 / years) - 1
        mdd = (nav / nav.cummax() - 1).min()
        print(f"{label:<10} 年化 {ann*100:6.1f}%   最大回撤 {mdd*100:6.1f}%")

    nav_stats(mom_nav, "动量轮动")
    nav_stats(combo_strat, "信号组合")
    nav_stats(combo_bh, "等权持有")

    print()
    print("逐年对比(持有 / 信号组合 / 动量轮动):")
    dfy = pd.DataFrame({"持有": combo_bh, "信号": combo_strat, "动量": mom_nav}).dropna()
    dfy["year"] = dfy.index.year
    yagg = dfy.groupby("year").apply(
        lambda g: (g["持有"].iloc[-1] / g["持有"].iloc[0] - 1,
                   g["信号"].iloc[-1] / g["信号"].iloc[0] - 1,
                   g["动量"].iloc[-1] / g["动量"].iloc[0] - 1),
        include_groups=False)
    print(f"{'年份':<6}{'持有':>10}{'信号':>10}{'动量轮动':>10}")
    for y, (h, s, m) in yagg.items():
        print(f"{y:<6}{h*100:>9.1f}%{s*100:>9.1f}%{m*100:>9.1f}%")

    print()
    print("动量轮动平均持仓:", {c: f"{mom_pos[c].mean()*100:.0f}%" for c in mom_pos.columns})

if __name__ == "__main__":
    main()
