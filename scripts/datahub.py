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


def index_history(symbol, cache_name=None):
    """中证指数历史行情与滚动PE。"""
    return cached(cache_name or f"hist_index_{symbol.lower()}", lambda: _csindex(symbol))


def hist_hl():
    """红利低波 H30269 日线(收盘/成交金额/滚动市盈率)"""
    return index_history("H30269", "hist_hl")


def hist_300():
    """沪深300 000300 日线(收盘/成交金额/滚动市盈率)"""
    return index_history("000300", "hist_300")


def hist_au():
    """沪金主力连续 日线(收盘价/成交量)"""
    def loader():
        import akshare as ak
        df = ak.futures_main_sina(symbol="AU0")
        df["日期"] = pd.to_datetime(df["日期"])
        df = df.rename(columns={"收盘价": "收盘"})
        return df.sort_values("日期").reset_index(drop=True)
    return cached("hist_au", loader)


def hist_etf(symbol):
    """场内基金前复权日线；避免把分红除权误判成真实下跌。"""
    def loader():
        try:
            import akshare as ak
            df = ak.fund_etf_hist_em(
                symbol=symbol,
                period="daily",
                start_date="20130101",
                end_date=dt.date.today().strftime("%Y%m%d"),
                adjust="qfq",
            )
            df["日期"] = pd.to_datetime(df["日期"])
            return df[["日期", "收盘"]].sort_values("日期").reset_index(drop=True)
        except Exception as primary_error:
            # 东财偶发代理/限流时，降级到与网页相同的腾讯前复权800日K线。
            market = "sh" if symbol.startswith(("5", "6")) else "sz"
            code = market + symbol
            url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,800,qfq"
            response = requests.get(url, headers=HDR, timeout=20)
            response.raise_for_status()
            payload = response.json().get("data", {}).get(code, {})
            rows = payload.get("qfqday") or payload.get("day") or []
            if len(rows) < 100:
                raise RuntimeError(f"东财失败({primary_error}); 腾讯数据不足")
            return pd.DataFrame({
                "日期": pd.to_datetime([row[0] for row in rows]),
                "收盘": [float(row[2]) for row in rows],
            })

    return cached(f"hist_etf_{symbol}_qfq", loader)


def hist_etf_unadjusted(symbol):
    """ETF 完整原始日线，供研究回测在前复权长历史不可用时降级。"""
    def loader():
        import akshare as ak
        df = ak.fund_etf_hist_em(
            symbol=symbol,
            period="daily",
            start_date="20130101",
            end_date=dt.date.today().strftime("%Y%m%d"),
            adjust="",
        )
        df["日期"] = pd.to_datetime(df["日期"])
        return df[["日期", "收盘"]].sort_values("日期").reset_index(drop=True)

    return cached(f"hist_etf_{symbol}", loader)


def repair_etf_unit_changes(df, threshold=0.30):
    """修复ETF份额折算造成的非市场价格断点，仅用于研究回测。

    普通非杠杆ETF单日超过30%的跳变视为份额折算；把折算日前价格按断点比例衔接。
    返回 (修复后数据, 修复日期列表)，不改写原始缓存。
    """
    out = df[["日期", "收盘"]].copy().sort_values("日期").reset_index(drop=True)
    out["收盘"] = pd.to_numeric(out["收盘"], errors="coerce")
    repaired = []
    raw_return = out["收盘"].pct_change()
    for i in raw_return[raw_return.abs() > threshold].index:
        factor = out.loc[i, "收盘"] / out.loc[i - 1, "收盘"]
        out.loc[:i - 1, "收盘"] *= factor
        repaired.append(str(pd.to_datetime(out.loc[i, "日期"]).date()))
    return out, repaired


def hist_etf_research(symbol, preferred_min_rows=1200):
    """研究用长历史：优先完整前复权，短历史时降级为断点修复后的原始日线。"""
    qfq = hist_etf(symbol)
    if len(qfq) >= preferred_min_rows:
        return qfq, {"source": "完整前复权", "repaired": []}
    raw = hist_etf_unadjusted(symbol)
    repaired_df, repaired = repair_etf_unit_changes(raw)
    return repaired_df, {"source": "原始日线+份额折算修复", "repaired": repaired}


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


# ---------------------------------------------------------------- V2 新增数据源(阶段 B)
def dollar_index():
    """美元指数实时(新浪 DINIW)。只有当前值, 无历史序列; 失败返回 None。

    V2 黄金结构轮 G 的代理指标之一(与美债方向组合)。G 只在宏观数据可用时输出,
    缺失/陈旧时不允许给强买入(任务书 §5.5)。"""
    try:
        r = requests.get("https://hq.sinajs.cn/list=DINIW", headers=HDR, timeout=15)
        r.encoding = "gbk"
        body = r.text.split('="')[1].rstrip('";')
        parts = body.split(",")
        # 新浪美元指数格式: 时间,买价,卖价,最新价,涨跌量,开盘,最高,最低,收盘,名称,日期
        if len(parts) >= 9:
            return float(parts[3])
    except Exception as e:
        print(f"[datahub:dollar_index] 美元指数获取失败: {e}")
    return None


def hist_pe_legu(symbol="中证500", cache_name=None):
    """乐咕指数 PE 历史(含等权/加权滚动PE与中位数), 用于与中证官网 PE 交叉验证。

    乐咕为海外源, GitHub Actions 上可能失败 -> 调用方必须提供中证 PE 降级路径。
    返回 DataFrame(日期, 滚动市盈率, 等权滚动市盈率, 市盈率中位数) 或 None。"""
    def loader():
        import akshare as ak
        df = ak.stock_index_pe_lg(symbol=symbol)
        df = df.rename(columns={"日期": "日期"})
        df["日期"] = pd.to_datetime(df["日期"])
        cols = ["日期"]
        for src, dst in [("滚动市盈率", "滚动市盈率"),
                         ("等权滚动市盈率", "等权滚动市盈率"),
                         ("滚动市盈率中位数", "市盈率中位数")]:
            if src in df.columns:
                cols.append(dst)
        return df[cols].sort_values("日期").reset_index(drop=True)
    try:
        return cached(cache_name or f"pe_legu_{symbol.lower()}", loader)
    except Exception as e:
        print(f"[datahub:hist_pe_legu] {symbol} PE 历史不可用(乐咕), 使用中证口径: {e}")
        return None


def bond_zh_us():
    """中美主要期限利率长历史(akshare 统一表, 约7年), 供黄金宏观 G 与沪深300 利差。

    失败返回 None, 调用方降级回 bond_cn()/bond_us()(新浪4年)。"""
    def loader():
        import akshare as ak
        df = ak.bond_zh_us_rate(start_date="20180101")
        df["日期"] = pd.to_datetime(df["日期"])
        return df.sort_values("日期").reset_index(drop=True)
    try:
        return cached("bond_zh_us", loader)
    except Exception as e:
        print(f"[datahub:bond_zh_us] 中美利率长历史不可用: {e}")
        return None


def index_cons(symbol):
    """中证指数成分股当前快照(阶段 A 已实测: 中证500 500只/科创50 50只/白酒 17只/医疗 50只)。

    只用于当前截面观察, 不得回填历史(任务书 §11: 无历史成分快照)。失败返回 None。"""
    try:
        import akshare as ak
        df = ak.index_stock_cons_csindex(symbol=symbol)
        df["日期"] = pd.to_datetime(df["日期"])
        return df.sort_values("日期").reset_index(drop=True)
    except Exception as e:
        print(f"[datahub:index_cons] 成分股快照失败 {symbol}: {e}")
        return None
