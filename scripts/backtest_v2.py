# -*- coding: utf-8 -*-
"""category_v2_shadow 回测: 每类指数 5 方案对比(任务书 §13 + 2026-08 更正版)。

对比: common_v1 / 始终持有 / 简单200日趋势 / 估值单因子 / category_v2_shadow(三套候选)
统一假设: 周频判断, 信号延迟 1/2/3/5 交易日, 两种成交口径(2026-08 更正):
  - exec_mode="open":  成交日开盘成交, 必须使用真实开盘价(当日收益 = 收盘/开盘-1)
  - exec_mode="close": 成交日收盘成交, 只能从成交后的下一日开始计算收益
  - 信号日不产生策略收益; 成交成本只在真实换仓日扣除
  - 单边成本 0.1%/0.3%/0.5%; 周一至周五 5 种周内观察偏移
恒生科技: 阶段 B 不参与回测(数据不足通道)。
宽度: 无历史成分快照, 历史回测不含宽度(与线上 shadow 当前截面观察分开标注)。
输出指标: 年化/最大回撤/年化波动/下行波动/夏普/参与比例/年化换手/信号次数/最长连续跑输期,
          分年度表现, 牛/熊/震荡阶段表现, 成本敏感度, 延迟敏感度, 参数±20%扰动稳定性。
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
WEEK_FREQS = ["W-MON", "W-TUE", "W-WED", "W-THU", "W-FRI"]   # 周内观察偏移 0..4


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
    """返回 {资产名: frame}。frame 列: open, close, pe, pb(300), erp(300), us10y(黄金), rsi, ma20, ma60"""
    assets = {}

    def load_close_index(sym, cache_name):
        df = datahub.index_history(sym, cache_name).set_index("日期")
        df = df[["开盘", "收盘", "滚动市盈率"]].rename(
            columns={"开盘": "open", "收盘": "close", "滚动市盈率": "pe"})
        df["open"] = pd.to_numeric(df["open"], errors="coerce")
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

    # 红利低波: ETF 前复权价格(含开盘) + 指数 PE(2019前为指数模拟研究期)
    hist_hl, hl_src = datahub.hist_etf_research("512890")
    df_hl = hist_hl.set_index("日期")[["开盘", "收盘"]].rename(
        columns={"开盘": "open", "收盘": "close"})
    value_hl = datahub.hist_hl().set_index("日期")["滚动市盈率"].astype(float)
    df_hl["pe"] = value_hl.reindex(df_hl.index)
    # 完整 PE 历史(2013+)随帧携带: 估值分位必须用完整历史, 不能只取ETF窗口内的PE(P1-4)
    df_hl.attrs["pe_full"] = value_hl
    assets["红利低波"] = df_hl.dropna(subset=["close"])

    # 黄金: ETF 前复权价格(含开盘) + 美债10Y
    hist_au, au_src = datahub.hist_etf_research("518880")
    df_au = hist_au.set_index("日期")[["开盘", "收盘"]].rename(
        columns={"开盘": "open", "收盘": "close"})
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
        df["ret_oc"] = df["close"] / df["open"] - 1   # 开盘->收盘收益(开盘成交口径)
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
            pe_h = _pe_upto(df, day)
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


def _pe_upto(df, day):
    """评估日当时可见的完整 PE 历史(优先 attrs['pe_full'] 完整序列, 如红利低波)。"""
    full = getattr(df, "attrs", {}).get("pe_full")
    if full is not None:
        return full.loc[:day].dropna()
    return df["pe"].iloc[:df.index.get_loc(day) + 1].dropna()


def v2_signal_at(df, asset_name, candidate, day):
    """评估日 day 的完整 v2 信号 dict(供回测与影子日志共用)。

    未来数据扰动安全性: 只用 iloc[:i+1] 截至评估日的数据, 评估日之后的数据不影响信号。"""
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
        pe = _pe_upto(df, day)
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


def v2_shadow_actions(df, asset_name, candidate, week_offset=0):
    """category_v2_shadow 动作序列(周频评估日计算, 其余日沿用)。

    week_offset: 0=周一收盘观察, 4=周五收盘观察(每5日观察偏移)。"""
    actions = {}
    idx = df.index
    weeks = pd.Series(idx.to_period(WEEK_FREQS[week_offset % 5]), index=idx)
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


# ---------------------------------------------------------------- 模拟(更正成交口径)
def simulate(df, actions, cost=COST, delay=1, exec_mode="close"):
    """周频动作 -> 指定执行口径的持仓模拟(2026-08 更正)。

    时间线(P1-1):
      - 信号日 t 产生动作, 不产生策略收益。
      - 执行日 = t 之后第 delay 个交易日。
      - exec_mode="open":  执行日开盘成交(用真实开盘价), 当日收益 = close/open-1;
                           若卖出清仓, 当日收益 = 0。
      - exec_mode="close": 执行日收盘成交, 当日按旧持仓计收益, 新持仓从下一日开始。
      - 成交成本只在真实换仓日扣除(单边成本 × 换仓幅度)。
    返回带 pos/strat_ret/strat_nav/bh_nav/turnover/cost_day 的 DataFrame。"""
    idx = df.index
    n = len(idx)
    ret_cc = df["ret"].fillna(0).to_numpy()
    ret_oc = df["ret_oc"].fillna(0).to_numpy() if "ret_oc" in df else ret_cc

    plan = []
    for i, day in enumerate(idx):
        a = actions.get(day)
        if a in ("买入", "卖出"):
            target = 1.0 if a == "买入" else 0.0
            j = i + delay
            if j < n:
                plan.append((j, target))
    plan.sort(key=lambda item: item[0])
    ptr = 0

    pos = np.zeros(n)
    cost_day = np.zeros(n)
    strat_ret = np.zeros(n)
    holding = 0.0
    for i in range(n):
        old_holding = holding
        while ptr < len(plan) and plan[ptr][0] == i:
            _, target = plan[ptr]
            if target != holding:
                cost_day[i] += cost * abs(target - holding)
            holding = target
            ptr += 1
        changed = (holding != old_holding)
        if exec_mode == "open":
            eff = holding
            if eff > 0:
                strat_ret[i] = ret_oc[i] if changed else ret_cc[i]
        else:  # close
            eff = old_holding
            if eff > 0:
                strat_ret[i] = ret_cc[i]
        pos[i] = holding
        strat_ret[i] -= cost_day[i]

    out = pd.DataFrame(index=idx)
    out["pos"] = pos
    out["turnover"] = pd.Series(np.abs(np.diff(pos, prepend=0.0)), index=idx)
    out["cost_day"] = cost_day
    out["strat_ret"] = strat_ret
    out["strat_nav"] = (1 + strat_ret).cumprod()
    out["bh_nav"] = (1 + ret_cc).cumprod()
    out["signals"] = pd.Series([1 if actions.get(d) in ("买入", "卖出") else 0 for d in idx], index=idx)
    return out


def metrics(result, start=None, end=None):
    """扩展指标集(任务书 §13)。按段计算时对段内净值归一化(从段起点=1 计算)。"""
    sample = result.loc[start:end].dropna(subset=["strat_ret"]) if start is not None else result.dropna(subset=["strat_ret"])
    if len(sample) < 100:
        return None
    ret = sample["strat_ret"].fillna(0).to_numpy()
    nav_raw = sample["strat_nav"].to_numpy()
    bh_raw = sample["bh_nav"].to_numpy()
    nav = nav_raw / nav_raw[0]       # 段内归一化(段起点 = 1)
    bh_nav = bh_raw / bh_raw[0]
    years = len(ret) / TRADING_DAYS
    ann = nav[-1] ** (1 / years) - 1 if years > 0 else 0
    mdd = (nav / np.maximum.accumulate(nav) - 1).min()
    vol = ret.std() * math.sqrt(TRADING_DAYS)
    down = ret[ret < 0]
    down_vol = down.std() * math.sqrt(TRADING_DAYS) if len(down) > 1 else 0.0
    sharpe = ret.mean() / ret.std() * math.sqrt(TRADING_DAYS) if ret.std() > 0 else float("nan")
    bh_ann = bh_nav[-1] ** (1 / years) - 1 if years > 0 else 0
    bh_mdd = (bh_nav / np.maximum.accumulate(bh_nav) - 1).min()
    # 每日 BH 收益(用于"最长连续跑输期"判定)
    prev = np.concatenate([[1.0], bh_nav[:-1]])
    bh_ret = bh_nav / prev - 1
    # 最长连续跑输期: 连续 strat_ret < bh_ret 的天数
    worse = ret < bh_ret
    longest = cur = 0
    for w in worse:
        cur = cur + 1 if w else 0
        longest = max(longest, cur)
    trades = int((np.abs(np.diff(result["pos"].to_numpy())) > 1e-9).sum())
    total_turnover = float(sample["turnover"].sum())
    return dict(days=len(sample), ann=ann, mdd=mdd, vol=vol, down_vol=down_vol,
                sharpe=sharpe, bh_ann=bh_ann, bh_mdd=bh_mdd,
                invested=float(sample["pos"].mean()),
                turnover_total=total_turnover,
                turnover_annual=total_turnover / years if years > 0 else 0.0,
                signals=int(sample["signals"].sum()), trades=trades,
                worst_streak=longest)


def fmt(v, kind="pct"):
    if v is None or (isinstance(v, float) and (pd.isna(v) or math.isnan(v))):
        return "—"
    if kind == "num":
        return f"{v:.2f}"
    return f"{v * 100:.1f}%"


def market_phase(df):
    """按 200日均线及其斜率把每个交易日分成 bull/bear/range。"""
    close = df["close"]
    ma200 = close.rolling(200).mean()
    slope = ma200.diff(20)
    phase = pd.Series("range", index=df.index)
    phase[(close > ma200) & (slope > 0)] = "bull"
    phase[(close < ma200) & (slope < 0)] = "bear"
    return phase


def phase_metrics(result, phase, p, min_days=60):
    """阶段回测统计(v4 修正): 只累计该阶段选中的日收益。

    旧实现把 result.loc[seg.index] 直接喂给 metrics(), 用绝对 strat_nav 归一化,
    但阶段通常由多段不连续区间组成, 段间非阶段日的收益也被算进 nav 比值。
    v4 改为: 只取该阶段日期的逐日收益, 阶段内净值 = 这些日收益的连乘
    (与"只累计该阶段选中的日收益"口径一致), 再从 1 开始算年化/回撤/波动。
    始终持有对照同样只累计该阶段日收益。
    """
    idx = phase[phase == p].index
    seg = result.loc[idx]
    if len(seg) < min_days:
        return None
    ret = seg["strat_ret"].fillna(0).to_numpy()
    nav = (1 + ret).cumprod()
    years = len(ret) / TRADING_DAYS
    ann = nav[-1] ** (1 / years) - 1 if years > 0 else 0
    mdd = (nav / np.maximum.accumulate(nav) - 1).min()
    vol = ret.std() * math.sqrt(TRADING_DAYS)
    down = ret[ret < 0]
    down_vol = down.std() * math.sqrt(TRADING_DAYS) if len(down) > 1 else 0.0
    sharpe = ret.mean() / ret.std() * math.sqrt(TRADING_DAYS) if ret.std() > 0 else float("nan")
    # 始终持有对照: 全序列日收益 -> 只取该阶段选中的日
    bh_ret_daily = result["bh_nav"].pct_change().fillna(0)
    bh_sel = (1 + bh_ret_daily.loc[idx]).cumprod()
    bh_ann = bh_sel.iloc[-1] ** (1 / years) - 1 if years > 0 else 0
    bh_mdd = (bh_sel / np.maximum.accumulate(bh_sel) - 1).min()
    return dict(days=len(seg), ann=ann, mdd=mdd, vol=vol, down_vol=down_vol,
                sharpe=sharpe, bh_ann=bh_ann, bh_mdd=bh_mdd,
                invested=float(seg["pos"].mean()) if "pos" in seg else 0.0)


# ---------------------------------------------------------------- 主流程
def run_full_report(exec_mode="close", cost=COST, delay=1, week_offset=0):
    """单组参数下完整报告: 各资产 5 方案对比 + 分年度 + 牛熊震荡。"""
    assets = build_assets()
    print("=" * 110)
    print(f"category_v2_shadow 回测 (周频评估 offset={week_offset} / 延迟{delay}日 / "
          f"{'开盘' if exec_mode == 'open' else '收盘'}成交 / 单边成本 {cost * 100:.1f}%)")
    print("=" * 110)

    rows = []
    for name, df in assets.items():
        category, _ = cfg.CATEGORY_MAP[name]
        print(f"\n### {name} [{category}]  {df.index.min().date()} ~ {df.index.max().date()} ({len(df)}日)")
        if name == "恒生科技":
            print("  数据不足通道, 不参与回测收益对比")
            continue
        schemes = {}
        schemes["common_v1"] = simulate(df, common_v1_actions(df, name), cost=cost, delay=delay, exec_mode=exec_mode)
        schemes["始终持有"] = simulate(df, {d: "买入" for d in df.index}, cost=cost, delay=delay, exec_mode=exec_mode)
        schemes["200日趋势"] = simulate(df, trend200_actions(df), cost=cost, delay=delay, exec_mode=exec_mode)
        schemes["估值单因子"] = simulate(df, valuation_only_actions(df), cost=cost, delay=delay, exec_mode=exec_mode)
        for cand in ("value", "balanced", "trend"):
            schemes[f"v2_{cand}"] = simulate(df, v2_shadow_actions(df, name, cand, week_offset),
                                             cost=cost, delay=delay, exec_mode=exec_mode)

        hdr = f"{'方案':<16}{'年化':>8}{'回撤':>8}{'波动':>8}{'下行':>8}{'夏普':>7}{'投入':>7}{'年换手':>8}{'信号':>6}{'跑输期':>8}{'持有年化':>9}"
        print(hdr)
        for sname, result in schemes.items():
            stat = metrics(result)
            if stat is None:
                print(f"{sname:<16}  样本不足")
                continue
            print(f"{sname:<16}{fmt(stat['ann']):>8}{fmt(stat['mdd']):>8}{fmt(stat['vol']):>8}"
                  f"{fmt(stat['down_vol']):>8}{fmt(stat['sharpe'], 'num'):>7}{fmt(stat['invested']):>7}"
                  f"{fmt(stat['turnover_annual'], 'num'):>8}{stat['signals']:>6}{stat['worst_streak']:>8}"
                  f"{fmt(stat['bh_ann']):>9}")
            rows.append(dict(asset=name, scheme=sname, **stat))

        # 分年度(各方案)
        print("  分年度年化:")
        years = sorted(set(d.year for d in df.index))
        print("  " + f"{'年份':<8}" + "".join(f"{sname[:10]:>12}" for sname in schemes))
        for y in years:
            line = f"  {y:<8}"
            for sname, result in schemes.items():
                stat = metrics(result, start=f"{y}-01-01", end=f"{y}-12-31")
                line += f"{fmt(stat['ann'] if stat else None):>12}"
            print(line)

        # 牛/熊/震荡阶段(v4: 只累计该阶段选中的日收益, 段内净值从1起算)
        phase = market_phase(df)
        print("  市场阶段(200日线):", {k: int((phase == k).sum()) for k in ("bull", "bear", "range")})
        for p in ("bull", "bear", "range"):
            if int((phase == p).sum()) < 60:
                continue
            parts = []
            for sname, result in schemes.items():
                stat = phase_metrics(result, phase, p)
                parts.append(f"{sname[:10]} {fmt(stat['ann'] if stat else None)}/{fmt(stat['mdd'] if stat else None)}")
            print(f"  {p}: " + " | ".join(parts))
    return rows


def run_sensitivity():
    """成本/延迟/周偏移/参数扰动敏感度 + 新旧口径对比。"""
    assets = build_assets()
    print("\n" + "=" * 110)
    print("成本敏感度(单边成本 0.1% / 0.3% / 0.5%, 收盘成交, 延迟1) — 各指数 v2_balanced 汇总")
    print("=" * 110)
    for cost in (0.001, 0.003, 0.005):
        anns, mdds = [], []
        for name, df in assets.items():
            if name == "恒生科技":
                continue
            stat = metrics(simulate(df, v2_shadow_actions(df, name, "balanced"), cost=cost))
            if stat:
                anns.append(stat["ann"]); mdds.append(stat["mdd"])
        print(f"成本 {cost * 100:.1f}%: 平均年化 {np.mean(anns) * 100:.2f}%  平均回撤 {np.mean(mdds) * 100:.1f}%")

    print("\n" + "=" * 110)
    print("延迟敏感度(延迟 1/2/3/5 交易日, 收盘成交, 成本0.1%) — 各指数 v2_balanced 年化")
    print("=" * 110)
    print(f"{'资产':<10}" + "".join(f"d{delay}:{'年化':>9}{'回撤':>9}" for delay in (1, 2, 3, 5)))
    for name, df in assets.items():
        if name == "恒生科技":
            continue
        line = f"{name:<10}"
        for delay in (1, 2, 3, 5):
            stat = metrics(simulate(df, v2_shadow_actions(df, name, "balanced"), delay=delay))
            line += f"{fmt(stat['ann'] if stat else None):>9}{fmt(stat['mdd'] if stat else None):>9}"
        print(line)

    print("\n" + "=" * 110)
    print("周内观察偏移(周一~周五 5 种, 收盘成交, 延迟1, 成本0.1%) — v2_balanced 年化")
    print("=" * 110)
    print(f"{'资产':<10}" + "".join(f"{freq.split('-')[1]:>10}" for freq in WEEK_FREQS))
    for name, df in assets.items():
        if name == "恒生科技":
            continue
        line = f"{name:<10}"
        for off in range(5):
            stat = metrics(simulate(df, v2_shadow_actions(df, name, "balanced", week_offset=off)))
            line += f"{fmt(stat['ann'] if stat else None):>10}"
        print(line)

    print("\n" + "=" * 110)
    print("参数扰动稳定性(权重 ±20% 重新归一化, 收盘成交, 延迟1, 成本0.1%) — 各指数 v2_balanced 年化区间")
    print("=" * 110)
    for name, df in assets.items():
        if name == "恒生科技":
            continue
        base = cfg.CANDIDATES[name]["balanced"]
        anns = []
        for key in base:
            for mult in (0.8, 1.2):
                perturbed = {k: (v * mult if k == key else v) for k, v in base.items()}
                total = sum(perturbed.values())
                perturbed = {k: v / total for k, v in perturbed.items()}
                saved = dict(cfg.CANDIDATES)
                cfg.CANDIDATES[name] = dict(cfg.CANDIDATES[name])
                cfg.CANDIDATES[name]["balanced"] = perturbed
                try:
                    stat = metrics(simulate(df, v2_shadow_actions(df, name, "balanced")))
                    if stat:
                        anns.append(stat["ann"])
                finally:
                    cfg.CANDIDATES[name] = saved[name]
        if anns:
            print(f"{name:<10} 年化区间 {min(anns) * 100:.2f}% ~ {max(anns) * 100:.2f}% (基准 {metrics(simulate(df, v2_shadow_actions(df, name, 'balanced')))['ann'] * 100:.2f}%)")

    print("\n" + "=" * 110)
    print("新旧执行口径对比(收盘成交 vs 开盘成交, 延迟1, 成本0.1%) — v2_balanced 年化/回撤")
    print("=" * 110)
    print(f"{'资产':<10}{'close年化':>10}{'close回撤':>10}{'open年化':>10}{'open回撤':>10}")
    for name, df in assets.items():
        if name == "恒生科技":
            continue
        acts = v2_shadow_actions(df, name, "balanced")
        sc = metrics(simulate(df, acts, exec_mode="close"))
        so = metrics(simulate(df, acts, exec_mode="open"))
        print(f"{name:<10}{fmt(sc['ann'] if sc else None):>10}{fmt(sc['mdd'] if sc else None):>10}"
              f"{fmt(so['ann'] if so else None):>10}{fmt(so['mdd'] if so else None):>10}")


def main():
    run_full_report(exec_mode="close")
    run_sensitivity()


if __name__ == "__main__":
    main()
