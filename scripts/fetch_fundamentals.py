# -*- coding: utf-8 -*-
"""
基本面抓取: 东财F10/腾讯/akshare -> 五维评分 + 12个月估值区间
====================================================================
- A股: 东财财务 + 腾讯未复权日K; 港股: 腾讯行情 + akshare 财务/券商预期
- 历史PE只使用当时已经披露的财报,避免历史回看偷看未来
- 输出: data/fund.json 与 web/data/fund.json
"""
import datetime as dt
import json, math, os, statistics, time, urllib.request
import datahub
import signal_rules as rules

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
STOCKS_FILE = os.path.join(BASE, "stocks.json")          # 待抓股票列表
OUT_FILE = os.path.join(ROOT, "data", "fund.json")       # GitHub Pages 主数据
WEB_OUT_FILE = os.path.join(ROOT, "web", "data", "fund.json")

F10_URL = ("https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis/"
           "ZYZBAjaxNew?type=0&code={code}")

def http_get(url, tries=3):
    import gzip
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                "Referer": "https://emweb.securities.eastmoney.com/",
                "Accept-Encoding": "identity"})
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read()
            if raw[:2] == b"\x1f\x8b":      # gzip 兜底
                raw = gzip.decompress(raw)
            return json.loads(raw.decode("utf-8"))
        except Exception as e:
            if i == tries - 1:
                print(f"  抓取失败 {url}: {e}")
                return None
            time.sleep(3)
    return None

def num(v):
    """字段转 float, None/'--'/空 -> None"""
    if v is None or v == "--" or v == "":
        return None
    try:
        value = float(v)
        return value if math.isfinite(value) else None
    except Exception:
        return None

def code_sym(code):
    """6位代码 -> 东财F10代码(SH600519) 和 新浪symbol(sh600519)"""
    pre = "sh" if code[0] == "6" else "sz"
    return pre.upper() + code, pre + code

def fetch_f10(code):
    """返回 (最新期, 年报期, 去年同期, 全部行) 四个"""
    fcode, _ = code_sym(code)
    d = http_get(F10_URL.format(code=fcode))
    if not d or not d.get("data"):
        return None, None, None, []
    rows = d["data"]
    latest = rows[0]
    annual = None
    prev_year = None
    latest_date = str(latest.get("REPORT_DATE", ""))[:10]
    for r in rows:
        rd = str(r.get("REPORT_DATE", ""))[:10]
        if rd.endswith("12-31"):
            if annual is None:
                annual = r
        if prev_year is None and rd < latest_date and rd[-5:] == latest_date[-5:]:
            prev_year = r
    return latest, annual, prev_year, rows

def smooth_g(rows):
    """最近3个报告期累计归母净利同比的中位数,限幅[-30,50]; 无数据返回 None。

    2026-08 复审: 限幅与 HANDOFF 口径对齐(原 [-20,30] 会把高成长股增速压到30%,
    使 compute 里 g>60 的防御分支永远不可达)。"""
    vals = [num(r.get("PARENTNETPROFITTZ")) for r in rows[:3]]
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    g = statistics.median(vals)
    return max(-30.0, min(50.0, g))


def _iso_date(v):
    s = str(v or "")[:10]
    try:
        return dt.date.fromisoformat(s)
    except ValueError:
        return None


def available_date(row):
    """财报真正可被市场使用的日期;无公告日字段时使用保守滞后。"""
    for key in ("NOTICE_DATE", "ANNOUNCE_DATE", "UPDATE_DATE"):
        d = _iso_date(row.get(key))
        if d:
            return d
    report = _iso_date(row.get("REPORT_DATE"))
    if not report:
        return None
    if report.month == 12:
        lag = 120
    elif report.month == 6:
        lag = 90
    else:
        lag = 45
    return report + dt.timedelta(days=lag)

def ttm_eps_series(rows):
    """按可获得日期输出TTM EPS,避免把尚未披露的财报放入历史PE。"""
    parsed = []
    for r in rows:
        d = str(r.get("REPORT_DATE") or "")[:10]
        known = available_date(r)
        eps = num(r.get("EPSJB"))
        if len(d) == 10 and known and eps is not None:
            parsed.append((d, known.isoformat(), eps))
    parsed.sort(key=lambda x: x[0])
    by_year = {}
    for d, known, eps in parsed:
        by_year.setdefault(int(d[:4]), []).append((d, known, eps))
    out = []
    for d, known, eps in parsed:
        y = int(d[:4])
        md = d[5:]
        prev_same = None
        for pd, _, pe in by_year.get(y - 1, []):
            if pd[5:] == md:
                prev_same = pe
                break
        prev_annual = None
        for pd, _, pe in by_year.get(y - 1, []):
            if pd.endswith("12-31"):
                prev_annual = pe
                break
        if prev_annual is not None:
            ttm = eps + (prev_annual - (prev_same if prev_same is not None else 0.0))
            if ttm > 0:
                out.append((known, ttm))
        else:
            if md == "12-31" and eps > 0:   # 无上年数据时, 年报本身就是 TTM
                out.append((known, eps))
    return sorted(out)

def get_price_hist(code, days=520):
    """腾讯未复权日K -> [(date, close)] 升序; 与历史EPS保持同一每股口径。"""
    if str(code).startswith("hk") or (str(code).isdigit() and len(str(code)) == 5):
        sym = str(code) if str(code).startswith("hk") else "hk" + str(code)
    else:
        _, sym = code_sym(code)   # 小写 sz300124 / sh600519
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,,,{days}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            j = json.loads(r.read().decode("utf-8", errors="replace"))
        k = j["data"][sym]
        arr = k.get("day") or []
        return [(row[0], float(row[2])) for row in arr if len(row) >= 3]
    except Exception as e:
        print(f"  K线历史失败 {sym}: {e}")
        return []

def hist_pe_median(ttm_series, price_hist):
    """近约2年、可获得时点一致的历史日PE中位数;仅作主数据源失败时的降级。"""
    if len(ttm_series) < 3 or len(price_hist) < 200:
        return None
    pes = []
    ti = 0
    for d, close in price_hist:
        if d < ttm_series[0][0]:
            continue
        while ti + 1 < len(ttm_series) and ttm_series[ti + 1][0] <= d:
            ti += 1
        ttm = ttm_series[ti][1]
        if ttm and ttm > 0 and close and close > 0:
            pe = close / ttm
            if 0 < pe < 100:      # 排除异常样本
                pes.append(pe)
    if len(pes) < 150:
        return None
    pes.sort()
    return max(8.0, min(80.0, round(pes[len(pes) // 2], 1)))


def market_hist_pe_median(code, days=730):
    """直接读取市场发布的历史TTM PE,避免公司行为造成价格/EPS复权口径错配。"""
    import akshare as ak
    try:
        df = ak.stock_zh_valuation_baidu(
            symbol=str(code), indicator="市盈率(TTM)", period="近三年")
        if "date" not in df.columns or "value" not in df.columns:
            return None
        cutoff = dt.date.today() - dt.timedelta(days=days)
        vals = []
        for _, row in df.iterrows():
            d = _iso_date(row.get("date"))
            v = num(row.get("value"))
            if d and d >= cutoff and v is not None and math.isfinite(v) and 0 < v < 150:
                vals.append(v)
        if len(vals) < 150:
            return None
        return max(8.0, min(80.0, round(statistics.median(vals), 1)))
    except Exception as e:
        print(f"  市场历史PE失败 {code}: {e}")
        return None


def market_hist_pe_pctl(code, days=730):
    """当前TTM PE(序列末值)在近约2年历史TTM PE序列中的分位(0-100)。

    用于估值双轮交叉的第二只眼(第二期 v1.2): 与目标价空间分互相独立,
    防止单一目标价模型算错时连带把买卖信号带偏。返回 0-100 或 None。
    """
    import akshare as ak
    try:
        df = ak.stock_zh_valuation_baidu(
            symbol=str(code), indicator="市盈率(TTM)", period="近三年")
        if "date" not in df.columns or "value" not in df.columns:
            return None
        cutoff = dt.date.today() - dt.timedelta(days=days)
        vals = []
        for _, row in df.iterrows():
            d = _iso_date(row.get("date"))
            v = num(row.get("value"))
            if d and d >= cutoff and v is not None and math.isfinite(v) and 0 < v < 150:
                vals.append(v)
        if len(vals) < 150:
            return None
        cur = vals[-1]
        below = sum(1 for x in vals if x <= cur)
        return round(below / len(vals) * 100.0, 1)
    except Exception as e:
        print(f"  历史PE分位失败 {code}: {e}")
        return None

def valuation_range_score(price, low, mid, high):
    if not price or not low or not mid or not high:
        return None
    if price <= low:
        return 2
    if price <= mid:
        return 1
    if price <= high:
        return 0
    return -2


def quality_score(roe, rev_g, debt, gm, v_score):
    s = 0
    s += 2 if (roe is not None and roe >= 15) else (1 if (roe is not None and roe >= 10) else (0 if (roe is not None and roe >= 5) else (-2 if roe is not None else 0)))
    s += 2 if (rev_g is not None and rev_g >= 20) else (1 if (rev_g is not None and rev_g >= 10) else (0 if (rev_g is not None and rev_g >= 0) else (-2 if rev_g is not None else 0)))
    s += 2 if (debt is not None and debt < 40) else (1 if (debt is not None and debt < 60) else (0 if (debt is not None and debt < 75) else (-1 if debt is not None else 0)))
    s += v_score if v_score is not None else 0
    s += 1 if (gm is not None and gm >= 60) else (0 if (gm is not None and gm >= 30) else (-1 if gm is not None else 0))
    return s, "好" if s >= 5 else ("中" if s >= 2 else "差")


def compute(latest, annual, prev_year, price, fair_pe_hist=None, g_override=None):
    """五维评分 + 12个月估值情景区间。

    历史PE使用公告日可获得的TTM EPS与未复权价格;中心增速为最近3个报告期
    累计归母净利同比中位数。区间用于展示输入不确定性,不是承诺价格。
    """
    out = {}
    # 财务
    roe_a = num(annual.get("ROEJQ")) if annual else None
    rev_g = num(latest.get("TOTALOPERATEREVETZ"))
    np_g = num(latest.get("PARENTNETPROFITTZ"))
    debt = num(latest.get("ZCFZL"))
    gm = num(latest.get("XSMLL"))
    bps = num(latest.get("BPS"))
    eps_latest = num(latest.get("EPSJB"))
    eps_annual = num(annual.get("EPSJB")) if annual else None
    eps_prev = num(prev_year.get("EPSJB")) if prev_year else None
    # EPS TTM
    eps_ttm = None
    eps_ttm_direct = num(latest.get("EPS_TTM"))
    latest_report = str(latest.get("REPORT_DATE") or "")[:10]
    if eps_ttm_direct and eps_ttm_direct > 0:
        eps_ttm = eps_ttm_direct
    elif latest_report.endswith("12-31") and eps_latest and eps_latest > 0:
        # 最新期本身是年报时就是完整12个月,不能再叠加一次年报EPS。
        eps_ttm = eps_latest
    elif eps_annual and eps_latest and eps_prev is not None:
        eps_ttm = eps_annual + eps_latest - eps_prev
    elif eps_annual:
        eps_ttm = eps_annual
    pe = (price / eps_ttm) if (eps_ttm and eps_ttm > 0) else None
    pb = (price / bps) if (bps and bps > 0) else None

    # 12个月估值: 未来情景EPS × 历史PE区间
    #   增速分档: >=30%->30, 20-30%->25, 10-20%->20, 5-10%->15, 0-5%->12, 负->10
    target = target_low = target_high = None
    fair_pe = None
    g = g_override if g_override is not None else (np_g if np_g is not None else 0)
    if eps_ttm and eps_ttm > 0:
        if g > 60:
            pass
        else:
            if g >= 30: gp = 30
            elif g >= 20: gp = 25
            elif g >= 10: gp = 20
            elif g >= 5: gp = 15
            elif g >= 0: gp = 12
            else: gp = 10
            rp = (roe_a * 0.8) if (roe_a is not None and roe_a > 0) else 0
            if fair_pe_hist is not None:
                fair_pe = fair_pe_hist
            else:
                fair_pe = min(max(max(gp, rp), 8.0), 40.0)
            g_low, g_high = max(-30.0, g - 10.0), min(50.0, g + 10.0)
            pe_low = max(6.0, fair_pe * 0.85)
            pe_high = min(90.0, fair_pe * 1.15)
            target_low = eps_ttm * (1 + g_low / 100.0) * pe_low
            target = eps_ttm * (1 + g / 100.0) * fair_pe
            target_high = eps_ttm * (1 + g_high / 100.0) * pe_high
    space = (target / price - 1) * 100 if (target and price) else None
    space_low = (target_low / price - 1) * 100 if (target_low and price) else None
    space_high = (target_high / price - 1) * 100 if (target_high and price) else None
    # 护栏(HANDOFF): 目标价空间 >100% 或 < -90% 时模型假设失真, 不展示目标价
    if space is not None and (space > 100.0 or space < -90.0):
        target = target_low = target_high = None
        space = space_low = space_high = None
    v_score = valuation_range_score(price, target_low, target, target_high)
    s, verdict = quality_score(roe_a, rev_g, debt, gm, v_score)

    out = dict(
        name=latest.get("SECURITY_NAME_ABBR"),
        report=latest.get("REPORT_DATE_NAME"),
        roe=round(roe_a, 1) if roe_a is not None else None,
        rev_g=round(rev_g, 1) if rev_g is not None else None,
        np_g=round(np_g, 1) if np_g is not None else None,
        debt=round(debt, 1) if debt is not None else None,
        gm=round(gm, 1) if gm is not None else None,
        bps=round(bps, 2) if bps is not None else None,
        eps_ttm=round(eps_ttm, 2) if eps_ttm is not None else None,
        pe=round(pe, 1) if pe is not None else None,
        pb=round(pb, 2) if pb is not None else None,
        target=round(target, 2) if target is not None else None,
        target_low=round(target_low, 2) if target_low is not None else None,
        target_high=round(target_high, 2) if target_high is not None else None,
        space=round(space, 1) if space is not None else None,
        space_low=round(space_low, 1) if space_low is not None else None,
        space_high=round(space_high, 1) if space_high is not None else None,
        score=s, verdict=verdict,
        fair_pe=round(fair_pe, 1) if fair_pe is not None else None,
        growth_mid=round(g, 1) if g is not None else None,
        target_method="model_12m_range" if target is not None else None,
        v_score=v_score,
    )
    return out

def norm_date(v):
    """'20260331' -> '2026-03-31'"""
    s = str(v or "")[:10]
    import re
    m = re.match(r"^(\d{4})(\d{2})(\d{2})$", s)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else s

def hk_f10_rows(df):
    """akshare 港股财务分析指标 -> compute() 认识的字段名(对齐东财F10)"""
    rows = []
    for _, r in df.iterrows():
        rd = norm_date(r.get("REPORT_DATE"))
        rows.append(dict(
            REPORT_DATE=rd, REPORT_DATE_NAME=rd,
            SECURITY_NAME_ABBR=r.get("SECURITY_NAME_ABBR"),
            ROEJQ=r.get("ROE_AVG"),
            TOTALOPERATEREVETZ=r.get("OPERATE_INCOME_YOY"),
            PARENTNETPROFITTZ=r.get("HOLDER_PROFIT_YOY"),
            ZCFZL=r.get("DEBT_ASSET_RATIO"),
            XSMLL=r.get("GROSS_PROFIT_RATIO"),
            BPS=r.get("BPS"),
            EPSJB=r.get("BASIC_EPS"),
            EPS_TTM=r.get("EPS_TTM"),
        ))
    return rows

def _quantile(values, q):
    values = sorted(values)
    if not values:
        return None
    pos = (len(values) - 1) * q
    lo, hi = int(pos), min(int(pos) + 1, len(values) - 1)
    frac = pos - lo
    return values[lo] * (1 - frac) + values[hi] * frac


def hk_analyst_target(sym):
    """港股近一年券商目标价样本: 去机构重复、IQR异常值后返回四分位区间。"""
    import akshare as ak
    try:
        df = ak.stock_hk_profit_forecast_et(symbol=str(sym).replace("hk", ""))
        if "目标价" not in df.columns:
            return None
        date_cols = [c for c in df.columns if "日期" in str(c) or "时间" in str(c)]
        firm_cols = [c for c in df.columns if "机构" in str(c) or "券商" in str(c) or "证券商" in str(c)]
        cutoff = dt.date.today() - dt.timedelta(days=365)
        records = []
        for _, row in df.iterrows():
            try:
                x = float(row.get("目标价"))
            except Exception:
                continue
            if not math.isfinite(x) or x <= 0:
                continue
            report_date = None
            for c in date_cols:
                report_date = _iso_date(row.get(c))
                if report_date:
                    break
            if report_date and report_date < cutoff:
                continue
            firm = str(row.get(firm_cols[0]) or "") if firm_cols else ""
            records.append((firm, report_date or cutoff, x))
        if firm_cols:
            latest_by_firm = {}
            for firm, report_date, x in records:
                key = firm or f"anonymous-{len(latest_by_firm)}"
                if key not in latest_by_firm or report_date >= latest_by_firm[key][0]:
                    latest_by_firm[key] = (report_date, x)
            tgt = [v[1] for v in latest_by_firm.values()]
        else:
            tgt = [x for _, _, x in records]
        if not tgt:
            return None
        q1, q3 = _quantile(tgt, 0.25), _quantile(tgt, 0.75)
        iqr = q3 - q1
        clean = [x for x in tgt if q1 - 1.5 * iqr <= x <= q3 + 1.5 * iqr] if iqr else tgt
        if not clean:
            return None
        mid = _quantile(clean, 0.5)
        low = _quantile(clean, 0.25) if len(clean) > 1 else mid * 0.85
        high = _quantile(clean, 0.75) if len(clean) > 1 else mid * 1.15
        return {"low": low, "mid": mid, "high": high, "count": len(clean)}
    except Exception as e:
        print(f"  券商预期失败 {sym}: {e}")
        return None

def fetch_hk_vals():
    """港股: 腾讯 qt.gtimg.cn 实时行情(PE/PB/市值) + 东财港股财务分析(akshare) -> 五维评分+目标价
    字段(按 ~ 分割, 0基): [1]=名称 [3]=现价 [4]=昨收 [39]=PE [43]=PB [44]=总市值(亿)"""
    import akshare as ak
    out = {}
    for sym, name in HK_SYMS.items():
        try:
            url = "https://qt.gtimg.cn/q=" + sym
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                "Referer": "https://gu.qq.com/"})
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read().decode("gbk", errors="replace")
            m = raw.split('="')[1].split('"')[0] if '="' in raw else ""
            f = m.split("~")
            if len(f) < 45:
                print(f"  港股字段不足 {name}({sym}): {len(f)} 段")
                continue
            price = float(f[3]) if f[3] else None
            pe = float(f[39]) if f[39] else None
            pb = float(f[43]) if f[43] else None
            mcap = float(f[44]) if f[44] else None
            if price is None:
                print(f"  港股无价格 {name}({sym}): 跳过")
                continue
            # 财务数据 -> 五维评分 + 目标价(与 A 股同一套 compute)
            try:
                df = ak.stock_financial_hk_analysis_indicator_em(symbol=sym.replace("hk", ""))
                rows = hk_f10_rows(df)
                latest = rows[0]
                annual = None
                prev_year = None
                latest_date = str(latest["REPORT_DATE"])[:10]
                for r in rows:
                    rd = str(r["REPORT_DATE"])[:10]
                    if rd.endswith("12-31") and annual is None:
                        annual = r
                    if prev_year is None and rd < latest_date and rd[-5:] == latest_date[-5:]:
                        prev_year = r
                fin = compute(latest, annual, prev_year, price,
                              fair_pe_hist=hist_pe_median(ttm_eps_series(rows), get_price_hist(sym)),
                              g_override=smooth_g(rows))
                fin["mcap"] = round(mcap, 0) if mcap else None
                # 券商一致预期目标价(12个月)优先于模型推导; 空间超护栏时不展示(HANDOFF)
                at = hk_analyst_target(sym)
                if at and price:
                    sp_mid = (at["mid"] / price - 1) * 100
                    if sp_mid > 100 or sp_mid < -90:
                        print(f"  港股券商预期 {name}: 空间{sp_mid:.0f}% 超护栏, 保留模型口径")
                    else:
                        fin["target_low"] = round(at["low"], 2)
                        fin["target"] = round(at["mid"], 2)
                        fin["target_high"] = round(at["high"], 2)
                        fin["space_low"] = round((at["low"] / price - 1) * 100, 1)
                        fin["space"] = round((at["mid"] / price - 1) * 100, 1)
                        fin["space_high"] = round((at["high"] / price - 1) * 100, 1)
                        fin["v_score"] = valuation_range_score(price, at["low"], at["mid"], at["high"])
                        fin["analyst"] = True
                        fin["analyst_count"] = at["count"]
                        fin["target_method"] = "analyst_12m_range"
                        fin["score"], fin["verdict"] = quality_score(
                            fin.get("roe"), fin.get("rev_g"), fin.get("debt"), fin.get("gm"), fin.get("v_score"))
                        print(f"  港股券商预期 {name}: 目标价≈{fin['target']} 空间{fin['space']}%")
                out[sym.replace("hk", "")] = fin
                print(f"  港股财务OK {name}({sym}): 评分{fin.get('score')}({fin.get('verdict')}) 目标价≈{fin.get('target')}")
                continue
            except Exception as e:
                print(f"  港股财务失败 {name}({sym}), 降级为仅估值: {e}")
            # 降级: 仅 PE/PB 估值
            if pe is None or pe <= 0:
                if pb is None:
                    print(f"  港股无PE/PB {name}({sym}): 跳过")
                    continue
                out[sym.replace("hk", "")] = dict(
                    name=name, price=price, pe=None, pb=round(pb, 2) if pb else None,
                    mcap=round(mcap, 0) if mcap else None,
                    v_score=2 if pb <= 3 else 1 if pb <= 5 else 0 if pb <= 8 else -1)
                print(f"  港股PE为负 {name}({sym}): PE={pe}, 用PB={pb} 打分")
                continue
            out[sym.replace("hk", "")] = dict(
                name=name, price=price, pe=round(pe, 1), pb=round(pb, 2) if pb else None,
                mcap=round(mcap, 0) if mcap else None,
                v_score=index_v_score(pe, None, None))
        except Exception as e:
            print(f"  港股估值失败 {name}({sym}): {e}")
        time.sleep(1)
    return out


# 港股估值覆盖: 网页 symbol -> 名称 (只加有 PE/PB 数据的个股; 指数无)
HK_SYMS = {
    "hk00700": "腾讯控股",
    "hk09992": "泡泡玛特",
    "hk03690": "美团",
}

def main():
    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
    if not os.path.exists(STOCKS_FILE):
        with open(STOCKS_FILE, "w", encoding="utf-8") as f:
            json.dump({"600519": "贵州茅台"}, f, ensure_ascii=False, indent=2)
    stocks = json.load(open(STOCKS_FILE, encoding="utf-8"))
    result = {}
    for code, name in stocks.items():
        print(f"抓取 {name}({code}) ...")
        latest, annual, prev_year, rows = fetch_f10(code)
        if latest is None:
            result[code] = {"name": name, "error": "数据获取失败"}
            continue
        # 现价: 最新收盘(用 F10 拿不到, 从 K 线算 -> 这里用新浪K线)
        price = get_price(code)
        g = smooth_g(rows)
        hist = market_hist_pe_median(code)
        if hist is None:
            hist = hist_pe_median(ttm_eps_series(rows), get_price_hist(code))
        if hist is not None:
            print(f"  {name} 历史PE中位数≈{hist}, 平滑增速≈{round(g,1) if g is not None else '—'}%")
        result[code] = compute(latest, annual, prev_year, price, fair_pe_hist=hist, g_override=g)
        result[code]["name"] = name
        result[code]["pe_pctl"] = market_hist_pe_pctl(code)  # 第二期: 估值双轮第二只眼(当前PE历史分位)
        time.sleep(1)
    # 指数估值 V 分(akshare, 失败不影响股票数据)
    try:
        result.update(fetch_index_vals())
    except Exception as e:
        print("指数估值跳过(akshare 不可用):", e)
    # 港股估值 V 分(腾讯 qt.gtimg.cn, 免费)
    try:
        result.update(fetch_hk_vals())
    except Exception as e:
        print("港股估值跳过:", e)
    # 美债 10Y(黄金宏观 G 供页面 shadow 用; 失败不影响主数据, 页面降级为宏观缺失)
    try:
        us_df = datahub.bond_us()
        us_vals = us_df["收益率"].dropna()
        if len(us_vals) > 21:
            result["us10y"] = {
                "now": round(float(us_vals.iloc[-1]), 3),
                "prev20": round(float(us_vals.iloc[-21]), 3),
                "date": str(us_df["日期"].iloc[-1])[:10],
            }
    except Exception as e:
        print("美债10Y 写入跳过:", e)
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    # 同时写 web/data/fund.json(本地页面与仓库 web/ 副本)
    os.makedirs(os.path.dirname(WEB_OUT_FILE), exist_ok=True)
    with open(WEB_OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n已写入 {OUT_FILE} 与 {WEB_OUT_FILE}")
    for code, r in result.items():
        if "error" in r:
            print(f"  {code} {r['name']}: {r['error']}")
        elif "name" not in r:
            continue   # 非资产条目(如 us10y 宏观字段)
        else:
            v = r.get("v_score")
            vtxt = "—" if v is None else ("+" + str(v) if v > 0 else str(v))
            if "target" in r and r.get("target"):
                print(f"  {code} {r['name']}: V{vtxt} 评分{r['score']}({r['verdict']}) 目标价≈{r['target']} 空间{r['space']}%")
            else:
                print(f"  {code} {r['name']}: V{vtxt} PE={r.get('pe')} 股息率={r.get('div_yield')}")

# 指数估值(akshare 中证官网): 网页 symbol -> (中证代码, 名称)
INDEXES = {
    "sh512890": ("H30269", "红利低波"),
    "sh000300": ("000300", "沪深300"),
    "sh000905": ("000905", "中证500"),
    "sh000688": ("000688", "科创50"),
    "sz399997": ("399997", "中证白酒"),
    "sz399989": ("399989", "中证医疗"),
}

def index_v_score(history, current_row, sym, pb_df=None, cn10y_df=None):
    """按指数自身历史区间计算 V，返回 (分数, 元数据)。"""
    import pandas as pd

    history = history.copy()
    history["日期"] = pd.to_datetime(history["日期"])
    history = history.sort_values("日期")
    pe = num(current_row.get("市盈率1"))
    valuation_date = str(pd.to_datetime(current_row.get("日期")).date())
    pe_history = history["滚动市盈率"]

    if sym == "sh512890":
        score, details = rules.redli_value_score(pe_history, pe)
        method = "自身5年PE分位(股息率仅作背景)"
    elif sym == "sh000300":
        pb_history = pb_df["市净率"] if pb_df is not None else []
        pb_now = float(pb_df["市净率"].iloc[-1]) if pb_df is not None else float("nan")
        erp_history = []
        erp_now = float("nan")
        if cn10y_df is not None and pe:
            cn = cn10y_df[["日期", "收益率"]].copy()
            cn["日期"] = pd.to_datetime(cn["日期"])
            pe_hist = history[["日期", "滚动市盈率"]].rename(columns={"滚动市盈率": "PE"})
            merged = pe_hist.merge(cn, on="日期", how="inner").dropna()
            erp_history = 100 / merged["PE"] - merged["收益率"]
            erp_now = 100 / pe - float(cn["收益率"].iloc[-1])
        score, details = rules.hs300_value_score(
            pe_history, pe, pb_history, pb_now, erp_history, erp_now
        )
        method = "自身5年PE/PB/股债利差分位"
    else:
        score = rules.valuation_percentile_score(pe_history, pe)
        details = {"pe": score, "quality": "完整" if score is not None else "数据不足"}
        method = "自身5年PE分位"

    return score, {
        "v_method": method,
        "v_quality": details.pop("quality"),
        "v_components": details,
        "valuation_date": valuation_date,
    }

def fetch_index_vals():
    import akshare as ak
    out = {}
    try:
        pb_df = datahub.pb_lg()
    except Exception as e:
        print(f"  沪深300 PB 历史不可用,估值将降级: {e}")
        pb_df = None
    try:
        cn10y_df = datahub.bond_cn()
    except Exception as e:
        print(f"  中债历史不可用,股债利差将降级: {e}")
        cn10y_df = None
    for sym, (icode, name) in INDEXES.items():
        try:
            df = ak.stock_zh_index_value_csindex(symbol=icode).sort_values("日期")
            row = df.iloc[-1]
            pe = num(row.get("市盈率1"))
            dy = num(row.get("股息率1"))
            history = datahub.index_history(icode)
            score, meta = index_v_score(
                history, row, sym, pb_df=pb_df, cn10y_df=cn10y_df
            )
            out[sym] = {"name": name, "pe": pe, "div_yield": dy,
                        "v_score": score, **meta}
        except Exception as e:
            print(f"  指数估值失败 {name}: {e}")
        time.sleep(1)
    return out

def get_price(code):
    """A股最新收盘价: 新浪K线(JSONP) -> 腾讯K线(fetch) 降级"""
    _, sym = code_sym(code)
    url = ("https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20x=/CN_MarketDataService."
           f"getKLineData?symbol={sym}&scale=240&ma=no&datalen=2")
    raw = None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read().decode("utf-8", errors="replace")
    except Exception:
        pass
    if raw:
        import re
        m = re.search(r"\[.*\]", raw, re.S)
        if m:
            try:
                arr = json.loads(m.group(0))
                if arr:
                    return float(arr[-1]["close"])
            except Exception:
                pass
    # 备用: 腾讯
    try:
        url2 = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,,,2,qfq"
        req = urllib.request.Request(url2, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            j = json.loads(r.read().decode("utf-8", errors="replace"))
        k = j["data"][sym]
        arr = (k.get("qfqday") or k.get("day") or [])
        if arr:
            return float(arr[-1][2])
    except Exception:
        pass
    return None

if __name__ == "__main__":
    main()
