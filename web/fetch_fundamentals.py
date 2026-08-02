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
    """返回 (最新期, 年报期, 去年同期) 三个 dict"""
    fcode, _ = code_sym(code)
    d = http_get(F10_URL.format(code=fcode))
    if not d or not d.get("data"):
        return None, None, None
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
    return latest, annual, prev_year

def compute(latest, annual, prev_year, price):
    """五维评分 + 目标价"""
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

    # 目标价: 合理PE = max(增速分档PE, ROE×0.8), 区间 8~40
    #   增速分档: >=30%->30, 20-30%->25, 10-20%->20, 5-10%->15, 0-5%->12, 负->10
    target = None
    fair_pe = None
    if eps_ttm and eps_ttm > 0:
        g = np_g if np_g is not None else 0
        if g >= 30: gp = 30
        elif g >= 20: gp = 25
        elif g >= 10: gp = 20
        elif g >= 5: gp = 15
        elif g >= 0: gp = 12
        else: gp = 10
        rp = (roe_a * 0.8) if (roe_a is not None and roe_a > 0) else 0
        fair_pe = min(max(max(gp, rp), 8.0), 40.0)
        target = eps_ttm * (1 + g / 100.0) * fair_pe
    space = (target / price - 1) * 100 if (target and price) else None

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

def fetch_hk_vals():
    """港股估值: 腾讯 qt.gtimg.cn 实时行情(免费, 无需key)
    字段(按 ~ 分割, 0基): [1]=名称 [3]=现价 [4]=昨收 [39]=PE [43]=PB [44]=总市值(亿)
    只返回有 PE/PB 的港股; 恒生科技等指数该接口无 PE/PB -> 跳过
    """
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
            if pe is None or pe <= 0:
                # PE 无效(亏损 TTM 或数据缺失): 保留条目, PE 显示为 None(页面显示 —), V 分改用 PB
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
        latest, annual, prev_year = fetch_f10(code)
        if latest is None:
            result[code] = {"name": name, "error": "数据获取失败"}
            continue
        # 现价: 最新收盘(用 F10 拿不到, 从 K 线算 -> 这里用新浪K线)
        price = get_price(code)
        result[code] = compute(latest, annual, prev_year, price)
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
