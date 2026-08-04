# -*- coding: utf-8 -*-
"""
红利低波 / 沪深300 / 黄金 —— 场外基金每日观察信号(盘中版)
=============================================================
规格(最终版):
  三维度评分 V(估值) T(趋势) M(动能),每维 -2..+2,总分 S(-6..+6)
  偏强: S>=+2 且 T>=0 且 M>=-1
  偏弱: S<=-4,或(T<=-2 且 V<=-1)
  其余: 中性
  观察窗口: 14:00 / 14:30 / 14:50,当日翻转>=2次则标记为反复
数据层: datahub.py(中证官网+新浪系, 当日缓存+重试+降级)
"""
import sys, os, json, time, datetime as dt
import pandas as pd
import numpy as np
import datahub
import signal_rules as rules

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.environ.get("SIGNAL_STATE_FILE", os.path.join(BASE, "state.json"))
TODAY = dt.date.today().isoformat()

# ---------------------------------------------------------------- 指标
def percentile_score(series, value, invert=False):
    """value 在 series 近5年窗口中的百分位 -> -2..+2。invert=True 表示越高越便宜"""
    s = series.dropna()
    if len(s) < 60 or pd.isna(value):
        return 0
    s = s.tail(1250)
    pct = (s < value).mean() * 100
    if invert:
        pct = 100 - pct
    if pct >= 70: return 2
    if pct >= 50: return 1
    if pct >= 30: return 0
    if pct >= 15: return -1
    return -2

def ma(series, n):
    return series.rolling(n).mean()

def rsi(series, n=14):
    """Wilder RSI；与 web/index.html 的 rsi14 保持同一初始化和递推口径。"""
    values = pd.Series(series, copy=False).astype(float)
    out = pd.Series(np.nan, index=values.index, dtype=float)
    if len(values) <= n:
        return out
    delta = values.diff()
    gains = delta.clip(lower=0)
    losses = -delta.clip(upper=0)
    avg_up = gains.iloc[1:n + 1].mean()
    avg_dn = losses.iloc[1:n + 1].mean()

    def value(up, down):
        if up == 0 and down == 0:
            return 50.0
        if down == 0:
            return 100.0
        return 100 - 100 / (1 + up / down)

    out.iloc[n] = value(avg_up, avg_dn)
    for i in range(n + 1, len(values)):
        avg_up = (avg_up * (n - 1) + gains.iloc[i]) / n
        avg_dn = (avg_dn * (n - 1) + losses.iloc[i]) / n
        out.iloc[i] = value(avg_up, avg_dn)
    return out

def clamp(x, lo, hi):
    return max(lo, min(hi, x))

def score_trend(close_series, spot):
    if close_series is None or len(close_series) < 70:
        return 0
    ma20 = close_series.rolling(20).mean().iloc[-1]
    ma60 = close_series.rolling(60).mean().iloc[-1]
    if pd.isna(ma20) or pd.isna(ma60):
        return 0
    if spot > ma20 and ma20 > ma60: return 2
    if spot > ma20 and ma20 <= ma60: return 1
    if spot <= ma20 and ma20 > ma60: return -1
    return -2

def score_momentum_values(rsi_value, chg5):
    """短期动能方向: 上涨加分、下跌扣分; 过热/超跌只做风险提示,不反向计分。"""
    m = 0
    if rsi_value is not None and not pd.isna(rsi_value):
        if rsi_value >= 60:
            m += 1
        elif rsi_value < 40:
            m -= 1
    if chg5 is not None:
        if chg5 >= 0.04:
            m += 1
        elif chg5 <= -0.04:
            m -= 1
    return clamp(m, -2, 2)


def score_momentum(close_series, spot, chg5):
    r = None
    if close_series is not None and len(close_series) > 30:
        r = rsi(pd.concat([close_series, pd.Series([spot])], ignore_index=True)).iloc[-1]
    return score_momentum_values(r, chg5)


def momentum_risk_text(rsi_value, chg5):
    if rsi_value is not None and not pd.isna(rsi_value) and rsi_value >= 80:
        return "短期涨得偏急"
    if chg5 is not None and chg5 >= 0.08:
        return "近5日涨幅偏大"
    if rsi_value is not None and not pd.isna(rsi_value) and rsi_value < 20:
        return "短期跌得偏急"
    if chg5 is not None and chg5 <= -0.08:
        return "近5日跌幅偏大"
    return "短期波动正常"


def make_signal(v, t, m, strong_s=2, weak_s=-4):
    """v=估值分 t=趋势分 m=动能分。只给观察判断,不输出交易指令。"""
    s = v + t + m
    if s >= strong_s and t >= 0 and m >= -1:
        return "偏强"
    if s <= weak_s or (t <= -2 and v <= -1):
        return "偏弱"
    return "中性"

def _trend_text(close, spot):
    """趋势状态文字: 现价 vs 20/60日线(大白话)"""
    ma20 = close.rolling(20).mean().iloc[-1]
    ma60 = close.rolling(60).mean().iloc[-1]
    if pd.isna(ma20) or pd.isna(ma60):
        return "趋势数据不足"
    if spot > ma20 and ma20 > ma60:
        return f"上涨中(现价{spot:.0f} > 1月线{ma20:.0f} > 3月线{ma60:.0f})"
    if spot > ma20:
        return f"反弹中(现价{spot:.0f} > 1月线{ma20:.0f},仍 < 3月线{ma60:.0f})"
    if ma20 < ma60:
        return f"下跌中(现价{spot:.0f} < 1月线{ma20:.0f} < 3月线{ma60:.0f})"
    return f"回调中(现价{spot:.0f} < 1月线{ma20:.0f},仍 > 3月线{ma60:.0f})"

def _note_300(hist_300, pe_now, erp_pct, pb_df, spot_300, close_300, cn10y, value_meta):
    """沪深300 详细理由: PE/PB 历史分位 + 股债性价比 + 盈利收益率息差 + 趋势"""
    pe_s = hist_300["滚动市盈率"].dropna().tail(1250)
    pe_pct_v = (pe_s < pe_now).mean() * 100 if len(pe_s) > 60 else float("nan")
    txt = f"PE≈{pe_now:.1f}(5年分位{pe_pct_v:.0f}%)"
    if pb_df is not None and len(pb_df) > 60:
        pb_now_v = float(pb_df["市净率"].iloc[-1])
        pb_pct_v = (pb_df["市净率"].dropna().tail(1250) < pb_now_v).mean() * 100
        txt += f"; PB≈{pb_now_v:.2f}(分位{pb_pct_v:.0f}%)"
    ep = 100 / pe_now
    txt += f"; 盈利收益率≈{ep:.1f}% vs 国债{cn10y:.2f}%(息差+{ep - cn10y:.1f}pp)"
    txt += f"; 股债性价比利差百分位≈{erp_pct:.0f}%(越高越便宜)"
    txt += f"; 估值数据{value_meta['quality']}"
    txt += f"; 趋势:{_trend_text(close_300, spot_300)}"
    return txt

def _minutes_elapsed(now):
    hm = now.hour * 60 + now.minute
    if now.hour < 9 or (now.hour == 9 and now.minute < 30):
        return 0
    if now.hour < 11 or (now.hour == 11 and now.minute <= 30):
        return max(0, hm - 570)
    if now.hour < 13:
        return 120
    if now.hour <= 15:
        return 120 + max(0, hm - 780)
    return 240

# ---------------------------------------------------------------- 主流程
def main():
    now = dt.datetime.now()
    today = now.date().isoformat()
    print("=" * 74)
    print(f"场外基金每日信号  {today} {now.strftime('%H:%M:%S')}")
    print("=" * 74)

    hm = now.strftime("%H:%M")
    if "14:50" <= hm < "15:00":
        cp = "14:50"
    elif "14:30" <= hm < "14:50":
        cp = "14:30"
    elif "14:00" <= hm < "14:30":
        cp = "14:00"
    else:
        cp = None
    if cp:
        print(f"检查点: {cp}(决策窗口 14:00-14:50,15:00 前提交按今日净值)")
    else:
        print(f"当前 {hm} 不在决策窗口(14:00-14:50),以下为最新快照,仅供参考")

    # ---------- 实时行情 ----------
    try:
        hq = datahub.hq(["sh512890", "sh000300", "sh518880"])
        def f(code, idx):
            parts = hq.get(code, (None, []))[1]
            try:
                return float(parts[idx])
            except (TypeError, ValueError, IndexError):
                return None
        etf_hl = f("sh512890", 3)      # 512890 现价
        spot_300 = f("sh000300", 3)
        spot_au = f("sh518880", 3)      # 黄金ETF现价,与基金页同口径
        hq_date = (hq.get("sh000300", (None, []))[1][30] if len(hq.get("sh000300", (None, []))[1]) > 30 else "") or today
        if hq_date != today:
            print(f"注意: 实时数据日期 {hq_date}(非今日交易数据)")
    except Exception as e:
        print(f"[错误] 实时行情获取失败: {e}")
        return

    # ---------- 历史/估值 ----------
    try:
        hist_hl = datahub.hist_etf("512890")
        hist_300 = datahub.hist_300()
        hist_au = datahub.hist_etf("518880")
        value_hl = datahub.hist_hl()
        div_yield, pe_hl_latest = datahub.div_yield_hl()
        cn10y_df = datahub.bond_cn()
        us10y_df = datahub.bond_us()
        pb_df = datahub.pb_lg()   # 可能为 None
        cn10y = float(cn10y_df["收益率"].iloc[-1])
        us10y = float(us10y_df["收益率"].iloc[-1])
        cn10y_date = str(cn10y_df["日期"].iloc[-1].date())
        us10y_date = str(us10y_df["日期"].iloc[-1].date())
        print(f"中债10Y {cn10y}%({cn10y_date})  美债10Y {us10y}%({us10y_date})"
              + ("  [PB数据缺失, 沪深300 V分降级]" if pb_df is None else ""))
    except Exception as e:
        print(f"[错误] 历史/估值数据获取失败: {e}")
        return

    results = {}

    # ========== 1. 红利低波(价格与基金页统一使用 ETF 512890) ==========
    close_hl = hist_hl["收盘"]
    spot_hl = etf_hl
    chg_hl = (spot_hl / close_hl.iloc[-1] - 1)
    chg5_hl = spot_hl / close_hl.iloc[-6] - 1 if len(close_hl) > 6 else None
    # V 使用每日官方估值，不用盘中价格近似改写；网页、邮件和回测保持同一口径。
    pe_hl_now = pe_hl_latest
    div_now = div_yield
    v_hl, v_hl_meta = rules.redli_value_score(
        value_hl["滚动市盈率"], pe_hl_now
    )
    t_hl = score_trend(close_hl, spot_hl)
    m_hl = score_momentum(close_hl, spot_hl, chg5_hl)
    sig_hl = make_signal(v_hl, t_hl, m_hl)
    pe_pct_hl = (value_hl["滚动市盈率"].dropna().tail(1250) < pe_hl_now).mean() * 100
    results["红利低波"] = dict(spot=spot_hl, chg=chg_hl, v=v_hl, t=t_hl, m=m_hl,
                                s=v_hl + t_hl + m_hl, sig=sig_hl,
                                note=f"股息率≈{div_now:.2f}%(较10年国债高{div_now - cn10y:+.2f}个百分点,仅作背景); "
                                     f"PE≈{pe_hl_now:.1f}(5年分位{pe_pct_hl:.0f}%,分{v_hl_meta['pe']:+d}); "
                                     f"估值数据{v_hl_meta['quality']}; 趋势:{_trend_text(close_hl, spot_hl)}")

    # ========== 2. 沪深300 ==========
    close_300 = hist_300["收盘"]
    chg_300 = spot_300 / close_300.iloc[-1] - 1
    chg5_300 = spot_300 / close_300.iloc[-6] - 1 if len(close_300) > 6 else None
    # 估值按每日官方收盘口径，技术面继续使用盘中现价。
    pe_now = float(hist_300["滚动市盈率"].iloc[-1])
    ep_now = 100 / pe_now
    erp_now = ep_now - cn10y
    # 历史利差百分位(中债覆盖期约2022起)
    erp_pct = float("nan")
    erp_hist = pd.Series(dtype=float)
    try:
        p = hist_300[["日期", "滚动市盈率"]].merge(cn10y_df, on="日期", how="inner")
        p = p[p["日期"] >= (dt.datetime.now() - dt.timedelta(days=1825)).strftime("%Y-%m-%d")]
        erp_hist = 100 / p["滚动市盈率"] - p["收益率"]
        if len(erp_hist) > 60:
            erp_pct = (erp_hist < erp_now).mean() * 100
    except Exception:
        pass
    pb_history = pb_df["市净率"] if pb_df is not None else []
    pb_now = float(pb_df["市净率"].iloc[-1]) if pb_df is not None else float("nan")
    v_300, v_300_meta = rules.hs300_value_score(
        hist_300["滚动市盈率"], pe_now, pb_history, pb_now, erp_hist, erp_now
    )
    m_300 = score_momentum(close_300, spot_300, chg5_300)
    t_300 = score_trend(close_300, spot_300)
    sig_300 = make_signal(v_300, t_300, m_300)
    results["沪深300"] = dict(spot=spot_300, chg=chg_300, v=v_300, t=t_300, m=m_300,
                               s=v_300 + t_300 + m_300, sig=sig_300,
                               note=_note_300(hist_300, pe_now, erp_pct, pb_df, spot_300, close_300,
                                              cn10y, v_300_meta))

    # ========== 3. 黄金 ==========
    close_au = hist_au["收盘"]
    chg_au = spot_au / close_au.iloc[-1] - 1
    chg5_au = spot_au / close_au.iloc[-6] - 1 if len(close_au) > 6 else None
    ma250 = ma(close_au, 250).iloc[-1]
    bias = (spot_au / ma250 - 1) * 100 if not pd.isna(ma250) else 0
    v_au = rules.gold_bias_score(bias)
    us_dir = 0
    if len(us10y_df) > 30:
        us_now = float(us10y_df["收益率"].iloc[-1])
        us_prev = float(us10y_df["收益率"].iloc[-20])
        us_dir = 1 if us_now < us_prev - 0.05 else -1 if us_now > us_prev + 0.05 else 0
    t_au = score_trend(close_au, spot_au)
    m_au = score_momentum(close_au, spot_au, chg5_au)
    sig_au = make_signal(v_au, t_au, m_au)
    high250 = close_au.tail(250).max()
    dd250 = (spot_au / high250 - 1) * 100 if not pd.isna(high250) else 0
    rsi_au = rsi(pd.concat([close_au, pd.Series([spot_au])])).iloc[-1]
    us_txt = {1: "下降(对黄金相对有利)", 0: "持平", -1: "上升(对黄金相对不利)"}.get(us_dir, "持平")
    results["黄金"] = dict(spot=spot_au, chg=chg_au, v=v_au, t=t_au, m=m_au,
                           s=v_au + t_au + m_au, sig=sig_au,
                           note=f"乖离250日≈{bias:+.1f}%(现价{spot_au:.0f} vs 年线{ma250:.0f}, 距250日高点{dd250:.1f}%); "
                                f"美债10Y {us10y:.2f}% 较20个交易日前{us_txt}(只作背景,不重复计入V); "
                                f"RSI≈{rsi_au:.0f}; 趋势:{_trend_text(close_au, spot_au)}")

    # ========== 中长期趋势共识(160/200/240日) ==========
    print()
    print("-" * 74)
    print("中长期趋势共识(160/200/240日；至少2个周期向上)")
    print("-" * 74)
    trend_data = {}
    vol_data = {}
    for name, (spot, close) in [("红利低波", (spot_hl, close_hl)),
                                ("沪深300", (spot_300, close_300)),
                                ("黄金", (spot_au, close_au))]:
        trend_data[name] = rules.multi_horizon_trend(close.tolist(), spot)
        vol_data[name] = rules.annualized_volatility(close.tolist(), spot)
    today_pos = []
    available = {name: signal for name, signal in trend_data.items() if signal is not None}
    if available:
        trend_sorted = sorted(available.items(), key=lambda x: -x[1]["average"])
        today_pos = rules.select_multi_horizon_trend(available, top_k=2)
        print(f"{'排名':<5}{'资产':<8}{'平均趋势':>10}{'周期共识':>12}{'60日波动':>10}")
        for rank, (name, signal) in enumerate(trend_sorted, 1):
            consensus = f"{signal['positive_votes']}/3向上" + ("" if signal["eligible"] else "(不足)")
            vol = vol_data.get(name)
            vol_text = f"{vol*100:.1f}%" if vol is not None else "--"
            print(f"{rank:<5}{name:<8}{signal['average']*100:>+9.1f}%{consensus:>12}{vol_text:>10}")
    else:
        print(">> 历史数据不足(需240个交易日)")

    for name, result in results.items():
        long_signal = trend_data.get(name)
        action, action_reason = rules.final_action(
            result.get("v"), result.get("t"), result.get("m"),
            long_signal.get("average") if long_signal else None,
            long_signal.get("positive_votes") if long_signal else None,
        )
        result["action"] = action
        result["action_reason"] = action_reason

    # ---------- 状态与翻转 ----------
    flips = update_state(results, cp, today_pos)
    final_sig = {}
    for name, r in results.items():
        final_sig[name] = "反复" if (cp and flips.get(name, 0) >= 2) else r["sig"]

    # ---------- 输出 ----------
    print("-" * 74)
    print(f"{'资产':<8}{'现价':>10}{'今日%':>8}{'V':>4}{'T':>4}{'M':>4}{'S':>5}  {'判断':<4}{'结果':>6}")
    print("-" * 74)
    for name, r in results.items():
        chg = f"{r['chg'] * 100:+.2f}" if r["chg"] is not None else "--"
        action = "持有" if (cp and flips.get(name, 0) >= 2) else r["action"]
        print(f"{name:<8}{r['spot']:>10.3f}{chg:>8}{r['v']:>+4d}{r['t']:>+4d}{r['m']:>+4d}{r['s']:>+5d}  {r['sig']:<4}{action:>6}")
    print("-" * 74)
    for name, r in results.items():
        print(f"  {name}: {r['note']}")
    print()
    if cp:
        print("今日观察结论(仅供参考):")
        for name, r in results.items():
            if final_sig[name] == "反复":
                print(f"  {name}: 盘中判断反复,暂不下结论")
            else:
                print(f"  {name}: {final_sig[name]}；策略结果 {r['action']} —— {r['action_reason']}")
    else:
        print("非观察窗口: 当前仅为数据快照。")

def update_state(results, cp, momentum_pos=None):
    flips = {name: 0 for name in results}
    try:
        state = json.load(open(STATE_FILE, encoding="utf-8"))
    except Exception:
        state = {}
    if state.get("date") != TODAY:
        old_pos = state.get("momentum_pos", [])
        state = {"date": TODAY, "seq": [], "flips": {}, "momentum_pos": old_pos}
    if momentum_pos is not None:
        state["momentum_pos"] = momentum_pos
    if cp:
        entry = {"cp": cp, "sig": {n: r["sig"] for n, r in results.items()}}
        seq = state["seq"]
        if seq and seq[-1]["cp"] == cp:
            seq[-1] = entry
        else:
            seq.append(entry)
        f = {n: 0 for n in results}
        prev = None
        for e in seq:
            if prev is None:
                prev = e["sig"]
                continue
            for n in results:
                if e["sig"].get(n) != prev.get(n):
                    f[n] += 1
            prev = e["sig"]
        state["flips"] = f
        flips = f
    json.dump(state, open(STATE_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    return flips

if __name__ == "__main__":
    main()
