# -*- coding: utf-8 -*-
"""
统一数据层: 稳定主源 + 当日缓存 + 重试 + 降级
================================================
原则:
  1. 主源只用本机实测稳定的: 中证官网(csindex) + 新浪系(hq/国债/期货)
  2. 所有日线数据当日缓存(cache/*.csv), 盘中多次运行不重复拉取
  3. 每个源失败重试3次; 仍失败用旧缓存兜底
  4. 乐咕(legulegu)限流严重, 仅 PB 一个指标使用, 失败自动降级
"""
import os, time, datetime as dt
import pandas as pd
import requests

BASE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BASE, "cache")
os.makedirs(CACHE_DIR, exist_ok=True)
HDR = {"Referer": "https://finance.sina.com.cn",
       "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def cached(name, loader, ttl_days=1, min_rows=100):
    """当日缓存包装。loader() 返回带 '日期' 列的 DataFrame。"""
    path = os.path.join(CACHE_DIR, name + ".csv")
    today = dt.date.today()
    try:
        if os.path.exists(path):
            mtime = dt.datetime.fromtimestamp(os.path.getmtime(path)).date()
            if (today - mtime).days <= ttl_days:
                df = pd.read_csv(path, parse_dates=["日期"])
                if len(df) > 0:
                    return df
    except Exception:
        pass
    last_err = None
    for attempt in range(3):
        try:
            df = loader()
            if df is not None and len(df) >= min_rows:
                df.to_csv(path, index=False)
                return df
            last_err = "数据行数不足"
        except Exception as e:
            last_err = e
            print(f"[datahub:{name}] 第{attempt + 1}次拉取失败: {e}")
        time.sleep(8)
    try:
        df = pd.read_csv(path, parse_dates=["日期"])
        print(f"[datahub:{name}] 拉取失败({last_err}), 使用旧缓存")
        return df
    except Exception:
        raise RuntimeError(f"数据源 {name} 不可用: {last_err}")


def _csindex(symbol):
    import akshare as ak
    start_date, end_date = "20130101", dt.date.today().strftime("%Y%m%d")
    primary = getattr(ak, "stock_zh_index_hist_csindex", None)
    if primary is not None:
        df = primary(symbol=symbol, start_date=start_date, end_date=end_date)
        df["日期"] = pd.to_datetime(df["日期"])
        return df.sort_values("日期").reset_index(drop=True)

    # AKShare接口变动时降级: 东财指数行情 + 中证估值日表,按日期合并。
    daily_api = getattr(ak, "stock_zh_index_daily_em", None)
    value_api = getattr(ak, "stock_zh_index_value_csindex", None)
    if daily_api is None or value_api is None:
        raise RuntimeError("AKShare缺少中证历史行情及降级接口")
    px = daily_api(symbol="csi" + symbol, start_date=start_date, end_date=end_date)
    px = px.rename(columns={"date": "日期", "close": "收盘", "amount": "成交金额"})
    px["日期"] = pd.to_datetime(px["日期"])
    val = value_api(symbol=symbol).rename(columns={"市盈率1": "滚动市盈率"})
    val["日期"] = pd.to_datetime(val["日期"])
    cols = [c for c in ("日期", "滚动市盈率") if c in val.columns]
    if len(cols) < 2:
        raise RuntimeError("中证估值接口缺少日期或市盈率字段")
    out = px.merge(val[cols], on="日期", how="left")
    out["滚动市盈率"] = out["滚动市盈率"].ffill()
    if "成交金额" not in out:
        out["成交金额"] = 0.0
    return out.sort_values("日期").reset_index(drop=True)


def hist_hl():
    """红利低波 H30269 日线(收盘/成交金额/滚动市盈率)"""
    return cached("hist_hl", lambda: _csindex("H30269"))


def hist_300():
    """沪深300 000300 日线(收盘/成交金额/滚动市盈率)"""
    return cached("hist_300", lambda: _csindex("000300"))


def hist_au():
    """沪金主力连续 日线(收盘价/成交量)"""
    def loader():
        import akshare as ak
        df = ak.futures_main_sina(symbol="AU0")
        df["日期"] = pd.to_datetime(df["日期"])
        df = df.rename(columns={"收盘价": "收盘"})
        return df.sort_values("日期").reset_index(drop=True)
    return cached("hist_au", loader)


def div_yield_hl():
    """H30269 最新股息率(%)与滚动PE"""
    def loader():
        import akshare as ak
        df = ak.stock_zh_index_value_csindex(symbol="H30269")
        df = df.sort_values("日期")
        out = df.iloc[[-1]][["日期", "股息率1", "市盈率1"]].reset_index(drop=True)
        out["日期"] = pd.to_datetime(out["日期"])
        return out
    df = cached("div_yield_hl", loader, ttl_days=3, min_rows=1)
    return float(df["股息率1"].iloc[-1]), float(df["市盈率1"].iloc[-1])


def bond_cn():
    """中国10年期国债收益率历史(约4年)"""
    def loader():
        import akshare as ak
        df = ak.bond_gb_zh_sina(symbol="中国10年期国债")
        df = df.rename(columns={"date": "日期", "close": "收益率"})
        df["日期"] = pd.to_datetime(df["日期"])
        return df[["日期", "收益率"]].sort_values("日期").reset_index(drop=True)
    return cached("bond_cn", loader)


def bond_us():
    """美国10年期国债收益率历史(约4年)"""
    def loader():
        import akshare as ak
        df = ak.bond_gb_us_sina(symbol="美国10年期国债")
        df = df.rename(columns={"date": "日期", "close": "收益率"})
        df["日期"] = pd.to_datetime(df["日期"])
        return df[["日期", "收益率"]].sort_values("日期").reset_index(drop=True)
    return cached("bond_us", loader)


def pb_lg():
    """沪深300 市净率历史(乐咕, 限流源)。失败返回 None, 调用方降级 PB=0。"""
    path = os.path.join(CACHE_DIR, "pb_lg.csv")
    try:
        if os.path.exists(path):
            mtime = dt.datetime.fromtimestamp(os.path.getmtime(path)).date()
            if (dt.date.today() - mtime).days <= 1:
                df = pd.read_csv(path, parse_dates=["日期"])
                if len(df) > 100:
                    return df
    except Exception:
        pass
    import akshare as ak
    for attempt in range(2):
        try:
            df = ak.stock_index_pb_lg(symbol="沪深300")
            if df is not None and len(df) > 100:
                df["日期"] = pd.to_datetime(df["日期"])
                df = df[["日期", "市净率"]].sort_values("日期").reset_index(drop=True)
                df.to_csv(path, index=False)
                return df
            print(f"[datahub:pb_lg] 第{attempt + 1}次数据异常")
        except Exception as e:
            print(f"[datahub:pb_lg] 第{attempt + 1}次失败: {e}")
        time.sleep(12)
    # 降级: 旧缓存
    try:
        df = pd.read_csv(path, parse_dates=["日期"])
        print("[datahub:pb_lg] 拉取失败, 使用旧缓存")
        return df
    except Exception:
        print("[datahub:pb_lg] 不可用, PB 分降级为 0")
        return None


def hq(codes):
    """新浪实时行情: {code: (名称, 字段列表)}"""
    url = "https://hq.sinajs.cn/list=" + ",".join(codes)
    r = requests.get(url, headers=HDR, timeout=15)
    r.encoding = "gbk"
    out = {}
    for line in r.text.strip().split("\n"):
        if "hq_str_" not in line:
            continue
        code = line.split("hq_str_")[1].split("=")[0].strip()
        body = line.split('="')[1].rstrip('";')
        parts = body.split(",")
        if len(parts) >= 10:
            out[code] = (parts[0], parts)
    return out
