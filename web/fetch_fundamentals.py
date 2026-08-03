# -*- coding: utf-8 -*-
"""
个股基本面抓取: 东财F10 -> 五维评分 + 目标价 -> fundamentals.json
================================================================
- 数据源: 东财 F10 主要指标(ZYZBAjaxNew, 后端抓取, 不受浏览器CORS限制)
- 覆盖: A股(代码 6位); 港股暂不支持
- 输出: web/data/fundamentals.json, 页面 fetch 展示
"""
import json, os, time, urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
STOCKS_FILE = os.path.join(BASE, "stocks.json")          # 待抓股票列表
OUT_FILE = os.path.join(BASE, "data", "fund.json")       # 页面 fetch 的实际文件(绕CDN缓存用新名)

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
        return float(v)
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
    """最近3期净利润同比的均值(去单季噪音), 限幅[-30, 50]; 无数据返回 None"""
    vals = [num(r.get("PARENTNETPROFITTZ")) for r in rows[:3]]
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    g = sum(vals) / len(vals)
    return max(-30.0, min(50.0, g))

def ttm_eps_series(rows):
    """按报告期算 TTM 每股收益: [(date, eps_ttm)] 升序. 公式: 当期累计 + 上年年报 - 上年同期累计"""
    parsed = []
    for r in rows:
        d = str(r.get("REPORT_DATE") or "")[:10]
        eps = num(r.get("EPSJB"))
        if len(d) == 10 and eps is not None:
            parsed.append((d, eps))
    parsed.sort()
    by_year = {}
    for d, eps in parsed:
        by_year.setdefault(int(d[:4]), []).append((d, eps))
    out = []
    for d, eps in parsed:
        y = int(d[:4])
        md = d[5:]
        prev_same = None
        for pd, pe in by_year.get(y - 1, []):
            if pd[5:] == md:
                prev_same = pe
                break
        prev_annual = None
        if by_year.get(y - 1):
            prev_annual = max(pe for _, pe in by_year[y - 1])
        if prev_annual is not None:
            ttm = eps + (prev_annual - (prev_same if prev_same is not None else 0.0))
            if ttm > 0:
                out.append((d, ttm))
        else:
            if md == "12-31" and eps > 0:   # 无上年数据时, 年报本身就是 TTM
                out.append((d, eps))
    return out

def get_price_hist(code, days=800):
    """腾讯前复权日K -> [(date, close)] 升序; 失败返回 []"""
    if str(code).startswith("hk") or (str(code).isdigit() and len(str(code)) == 5):
        sym = str(code) if str(code).startswith("hk") else "hk" + str(code)
    else:
        _, sym = code_sym(code)   # 小写 sz300124 / sh600519
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,,,{days},qfq"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            j = json.loads(r.read().decode("utf-8", errors="replace"))
        k = j["data"][sym]
        arr = k.get("qfqday") or k.get("day") or []
        return [(row[0], float(row[2])) for row in arr if len(row) >= 3]
    except Exception as e:
        print(f"  K线历史失败 {sym}: {e}")
        return []

def hist_pe_median(ttm_series, price_hist):
    """该股自身历史日PE中位数(锚定市场实际给过的估值), 区间 8~40; 样本不足返回 None"""
    if len(ttm_series) < 3 or len(price_hist) < 200:
        return None
    pes = []
    ti = 0
    for d, close in price_hist:
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
    return max(8.0, min(40.0, round(pes[len(pes) // 2], 1)))

def compute(latest, annual, prev_year, price, fair_pe_hist=None, g_override=None):
    """五维评分 + 目标价
    fair_pe_hist: 该股自身历史PE中位数(8~40), 有则优先; 无则用增速档位表
    g_override: 平滑后的增速(最近3期均值), 有则优先于单期
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
    if eps_annual and eps_latest and eps_prev is not None:
        eps_ttm = eps_annual + eps_latest - eps_prev
    elif eps_annual:
        eps_ttm = eps_annual
    pe = (price / eps_ttm) if (eps_ttm and eps_ttm > 0) else None
    pb = (price / bps) if (bps and bps > 0) else None

    # 目标价: 合理PE = 自身历史PE中位数(有则优先) 或 max(增速分档PE, ROE×0.8), 区间 8~40
    #   增速分档: >=30%->30, 20-30%->25, 10-20%->20, 5-10%->15, 0-5%->12, 负->10
    target = None
    fair_pe = None
    if eps_ttm and eps_ttm > 0:
        g = g_override if g_override is not None else (np_g if np_g is not None else 0)
        if g > 60:
            pass  # 超高增速超出模型适用范围, 不出目标价
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
            target = eps_ttm * (1 + g / 100.0) * fair_pe
    space = (target / price - 1) * 100 if (target and price) else None
    # 护栏: 目标价空间离谱(>100% 或 < -90%)说明模型对超高/超低增速失真, 不展示
    if space is not None and (space > 100 or space < -90):
        target = None
        space = None

    # 评分
    s = 0
    s += 2 if (roe_a is not None and roe_a >= 15) else (1 if (roe_a and roe_a >= 10) else (0 if (roe_a and roe_a >= 5) else (-2 if roe_a is not None else 0)))
    s += 2 if (rev_g is not None and rev_g >= 20) else (1 if (rev_g and rev_g >= 10) else (0 if (rev_g and rev_g >= 0) else (-2 if rev_g is not None else 0)))
    s += 2 if (debt is not None and debt < 40) else (1 if (debt and debt < 60) else (0 if (debt and debt < 75) else (-1 if debt is not None else 0)))
    s += 2 if (space is not None and space >= 20) else (1 if (space and space >= 0) else (0 if (space and space > -20) else (-2 if space is not None else 0)))
    s += 1 if (gm is not None and gm >= 60) else (0 if (gm and gm >= 30) else (-1 if gm is not None else 0))
    verdict = "好" if s >= 5 else ("中" if s >= 2 else "差")

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
        space=round(space, 1) if space is not None else None,
        score=s, verdict=verdict,
        fair_pe=round(fair_pe, 1) if fair_pe is not None else None,
        v_score=(2 if space >= 20 else 1 if space >= 0 else 0 if space > -20 else -2) if space is not None else None,
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

def hk_analyst_target(sym):
    """港股券商一致预期目标价(全部券商的 12 个月目标价中位数); 失败返回 None"""
    import akshare as ak
    try:
        df = ak.stock_hk_profit_forecast_et(symbol=str(sym).replace("hk", ""))
        tgt = []
        for v in df.get("目标价", []):
            try:
                x = float(v)
                if x > 0:
                    tgt.append(x)
            except Exception:
                pass
        if not tgt:
            return None
        tgt.sort()
        return tgt[len(tgt) // 2]
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
                # 券商一致预期目标价(12个月)优先于模型推导
                at = hk_analyst_target(sym)
                if at and price:
                    fin["target"] = round(at, 2)
                    sp = (at / price - 1) * 100
                    fin["space"] = round(sp, 1)
                    fin["v_score"] = 2 if sp >= 20 else 1 if sp >= 0 else 0 if sp > -20 else -2
                    fin["analyst"] = True
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
        hist = hist_pe_median(ttm_eps_series(rows), get_price_hist(code))
        if hist is not None:
            print(f"  {name} 历史PE中位数≈{hist}, 平滑增速≈{round(g,1) if g is not None else '—'}%")
        result[code] = compute(latest, annual, prev_year, price, fair_pe_hist=hist, g_override=g)
        result[code]["name"] = name
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
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    # 同时写 web/data/fund.json(仓库 web/ 降级副本, 本地推送用)
    web_out = os.path.join(BASE, "web", "data", "fund.json")
    os.makedirs(os.path.dirname(web_out), exist_ok=True)
    with open(web_out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n已写入 {OUT_FILE} 与 {web_out}")
    for code, r in result.items():
        if "error" in r:
            print(f"  {code} {r['name']}: {r['error']}")
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

def index_v_score(pe, div_yield, sym):
    """简化 V 分: 红利低波看股息率, 其他看 PE(绝对值口径, 非历史分位)"""
    if sym == "sh512890":
        if div_yield is None:
            return None
        return 2 if div_yield >= 5 else 1 if div_yield >= 4 else 0 if div_yield >= 3 else -1
    if pe is None:
        return None
    return 2 if pe <= 12 else 1 if pe <= 15 else 0 if pe <= 20 else -1

def fetch_index_vals():
    import akshare as ak
    out = {}
    for sym, (icode, name) in INDEXES.items():
        try:
            df = ak.stock_zh_index_value_csindex(symbol=icode)
            row = df.iloc[-1]
            pe = float(row.get("市盈率1") or 0) or None
            dy = float(row.get("股息率1") or 0) or None
            out[sym] = {"name": name, "pe": pe, "div_yield": dy,
                        "v_score": index_v_score(pe, dy, sym)}
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
