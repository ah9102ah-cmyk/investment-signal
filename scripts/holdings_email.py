# -*- coding: utf-8 -*-
"""
每日持仓观察邮件: 持仓 + 技术面(净值动量) + 基本面(估值分位) -> 双轮判断 -> HTML 邮件
==================================================================================
- 交易日判断: 新浪沪深300行情日期 == 今日, 否则跳过(非交易日/节假日)
- 数据: 爱基金持仓(work token) / 东财净值动量 / 蛋卷估值分位
- 发送: agently-cli 两阶段确认(ctk), 同 invest/email_signal.py
- 用法: py -3 holdings_email.py [--force] [--dry]
  --force 非交易日也发送(测试); --dry 只生成 HTML 不发送
- 缓存: invest/cache/holdings_{mom,eva}_YYYYMMDD.json, 当日重复运行不重复抓取
"""
import datetime as dt
import json
import os
import re
import subprocess
import sys
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(BASE, "cache")
RECIPIENT = "thfwj@qq.com"
SUBJECT_PREFIX = "持仓观察"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0 Safari/537.36"}

AGGREGATE = os.environ.get("AIJIJIN_AGGREGATE") or r"C:\Users\thfwj\AppData\Local\hermes\skills\fund-holding-list\scripts\aggregate_holdings.py"
AGENTLY_CMD = r"C:\Users\thfwj\.workbuddy\binaries\node\versions\22.22.2\agently-cli.cmd"
HOLD_API = "https://trade.5ifund.com/openapi/holdposition/v2/ai/list"
WALLET_API = "https://trade.5ifund.com/openapi/v1/query_wallet_home"
SENT_MARKER = os.path.join(CACHE, f"holdings_sent_{dt.date.today().strftime('%Y%m%d')}.json")
RUN_LOG = os.path.join(CACHE, "holdings_run.log")
SMTP_USER = "thfwj@qq.com"


def ensure_cloud_credentials():
    """云端模式: 把 AIJIJIN_CREDS 环境变量写入 ~/.aijijin/credentials.json"""
    creds = os.environ.get("AIJIJIN_CREDS")
    if creds:
        p = os.path.expanduser("~/.aijijin")
        os.makedirs(p, exist_ok=True)
        with open(os.path.join(p, "credentials.json"), "w", encoding="utf-8") as f:
            f.write(creds)


def log_run(msg):
    os.makedirs(CACHE, exist_ok=True)
    with open(RUN_LOG, "a", encoding="utf-8") as f:
        f.write(f"[{dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")

# 基金 -> 估值指数映射(蛋卷 index_eva/dj 代码)
EVA_MAP = {
    "020602": "CSIH30269", "007466": "CSIH30269",   # 中证红利低波
    "012414": "SZ399997", "161725": "SZ399997",     # 中证白酒
    "013127": "HKHSTECH",                            # 恒生科技
    "017937": "SZ399989",                            # 中证医疗
    "460300": "SH000300", "110020": "SH000300",     # 沪深300
    "007028": "SH000905",                            # 中证500
}
# 无估值指数但需要特别文案
EVA_NOTE = {"008163": "红利类,估值普遍偏高", "024616": "现金流红利类,估值参考偏高"}
# 债券/货币类: 不做估值,只描述组合角色
BOND_SET = {"019872", "007194", "016871", "013790", "001235", "590009", "007167",
            "001299", "003547", "050027", "485119", "007520", "002341", "006961",
            "110017", "019769"}
SPECIAL = {
    "006961": ("利率敏感", "利率历史低位,长债在利率反弹时回撤可能较大"),
    "110017": ("中性", "固收+含约12%股票,同时受股债两方面影响"),
    "002341": ("待确认", "份额状态待确认"),
    "008701": ("偏弱", "商品无常规估值;历史技术面较弱"),
    "013127": ("估值中低", "PE分位中低,但近期反弹力度偏弱"),
    "019769": ("现金类", "货币基金用于流动性管理,收益率随市场变化"),
}
# 债券(除特别处理)统一文案
BOND_ACTION = ("稳健类", "债基主要承担组合稳定作用;利率低位时收益空间有限")


def http_get(url, headers=None, timeout=25):
    req = urllib.request.Request(url, headers=headers or UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def is_trading_day():
    try:
        txt = http_get("https://hq.sinajs.cn/list=sh000300",
                       headers={**UA, "Referer": "https://finance.sina.com.cn"}, timeout=15).decode("gbk", "replace")
        m = re.search(r'"([\d-]+),', txt)
        return bool(m and m.group(1) == dt.date.today().isoformat())
    except Exception:
        return True  # 取不到行情时按交易日处理, 交给发送环节判断


def cached(path_key, fetch, ttl_days=1):
    """按日期缓存: 当日存在则复用"""
    os.makedirs(CACHE, exist_ok=True)
    fp = os.path.join(CACHE, f"{path_key}_{dt.date.today().strftime('%Y%m%d')}.json")
    if os.path.exists(fp):
        try:
            return json.load(open(fp, encoding="utf-8"))
        except Exception:
            pass
    data = fetch()
    json.dump(data, open(fp, "w", encoding="utf-8"), ensure_ascii=False)
    return data


# ---------- 1. 持仓 ----------
def fetch_holdings():
    from aijijin_sdk import get_work_token
    token = get_work_token()

    def post(url, body):
        req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                     headers={**UA, "Authorization": f"Bearer {token}",
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    def get(url):
        req = urllib.request.Request(url, headers={**UA, "Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    wallet = get(WALLET_API)
    funds = {c: post(HOLD_API, {"shareCategory": c}) for c in ["01", "02", "03", "04", "05", "06", "07"]}
    raw = os.path.join(CACHE, f"holdings_raw_{dt.date.today().strftime('%Y%m%d')}.json")
    os.makedirs(CACHE, exist_ok=True)
    with open(raw, "w", encoding="utf-8") as f:
        json.dump({"wallet": wallet, "funds": funds}, f, ensure_ascii=False)
    out = os.path.join(CACHE, f"holdings_agg_{dt.date.today().strftime('%Y%m%d')}.json")
    r = subprocess.run([sys.executable, AGGREGATE, "--input", raw, "--output", out],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"aggregate failed: {r.stderr[:300]}")
    agg = json.load(open(out, encoding="utf-8"))
    return agg


# ---------- 2. 净值动量 ----------
def fetch_momentum(codes):
    def do():
        res = {}
        for c in codes:
            try:
                js = http_get(f"https://fund.eastmoney.com/pingzhongdata/{c}.js",
                              headers={**UA, "Referer": "https://fund.eastmoney.com/"}).decode("utf-8", "replace")
                m = re.search(r"Data_netWorthTrend\s*=\s*(\[.*?\]);", js, re.S)
                if not m:
                    continue
                navs = [float(x["y"]) for x in json.loads(m.group(1))]
                if len(navs) < 30:
                    continue
                last = navs[-1]
                res[c] = {
                    "chg_20d": (last / navs[-21] - 1) * 100 if len(navs) > 21 else None,
                    "chg_60d": (last / navs[-61] - 1) * 100 if len(navs) > 61 else None,
                    "dd_250": (last / max(navs[-250:]) - 1) * 100 if len(navs) > 250 else None,
                }
            except Exception:
                continue
        return res
    return cached("holdings_mom", do)


# ---------- 3. 估值 ----------
def fetch_valuation():
    def do():
        try:
            raw = http_get("https://danjuanfunds.com/djapi/index_eva/dj",
                           headers={**UA, "Referer": "https://danjuanfunds.com/"}).decode("utf-8", "replace")
            d = json.loads(raw)
            items = d["data"]["items"] if isinstance(d.get("data"), dict) else d.get("data", [])
            return {it["index_code"]: {"pe": it.get("pe"), "pe_pct": it.get("pe_percentile"),
                                       "pb_pct": it.get("pb_percentile"), "yield": it.get("yeild")}
                    for it in items}
        except Exception:
            return {}
    return cached("holdings_eva", do)


# ---------- 4. 双轮观察规则 ----------
def decide(fund, mom, eva):
    code = fund["fundCode"]
    amt = float(fund["totalAmount"] or 0)
    if code in SPECIAL:
        act, why = SPECIAL[code]
        return act, why
    if code in BOND_SET:
        return BOND_ACTION
    if amt < 300:
        return "小额持仓", "金额不足300元,对组合影响很小"
    chg20 = mom.get(code, {}).get("chg_20d") if code in mom else None
    chg60 = mom.get(code, {}).get("chg_60d") if code in mom else None
    pe_pct = None
    if code in EVA_MAP:
        ev = eva.get(EVA_MAP[code])
        if ev:
            pe_pct = ev.get("pe_pct")
    elif code in EVA_NOTE:
        pe_pct = 0.75  # 无直接估值, 按"偏高"处理
    why = []
    if code in EVA_MAP and pe_pct is not None:
        why.append(f"PE分位{pe_pct:.0%}")
    elif code in EVA_NOTE:
        why.append(EVA_NOTE[code])
    if chg20 is not None:
        why.append(f"20日{chg20:+.1f}%")
    if chg60 is not None:
        why.append(f"60日{chg60:+.1f}%")

    if pe_pct is not None and pe_pct < 0.40 and chg20 is not None and chg20 > 2:
        act = "双轮偏强"
    elif pe_pct is not None and pe_pct > 0.70 and chg60 is not None and chg60 < -8:
        act = "双轮偏弱"
    elif pe_pct is not None and pe_pct > 0.70:
        act = "估值偏高"
    elif chg20 is not None and chg20 < -3:
        act = "趋势偏弱"
    else:
        act = "中性"
    return act, "；".join(why)


# ---------- 5. HTML ----------
def render_html(agg, mom, eva):
    funds = agg.get("funds", [])
    wallet = (agg.get("wallet") or {}).get("data") or {}
    today = dt.date.today().isoformat()
    UP, DOWN = "#dc2626", "#16a34a"
    act_color = {"双轮偏强": "#dc2626", "双轮偏弱": "#16a34a", "中性": "#d97706",
                 "估值偏高": "#d97706", "趋势偏弱": "#16a34a", "稳健类": "#0f766e",
                 "利率敏感": "#d97706", "现金类": "#64748b", "小额持仓": "#64748b"}

    total = sum(float(f["totalAmount"] or 0) for f in funds) + float(wallet.get("avaiableVol") or 0)
    dec = []
    for f in sorted(funds, key=lambda x: -float(x["totalAmount"] or 0)):
        dec.append((f, *decide(f, mom, eva)))
    n_equity = sum(1 for f, _, _ in dec if f["fundCode"] not in BOND_SET)
    n_bond = sum(1 for f, _, _ in dec if f["fundCode"] in BOND_SET and f["fundCode"] != "019769")
    n_wallet = 1 if wallet else 0

    h = []
    h.append('<div style="max-width:640px;margin:0 auto;font-family:-apple-system,Segoe UI,Microsoft YaHei,sans-serif;color:#1f2937;background:#f8fafc;padding:16px;border-radius:12px;">')
    h.append('<div style="text-align:center;padding:14px 0 6px;"><span style="font-size:22px;font-weight:700;">📋 持仓观察</span><br>'
             f'<span style="font-size:13px;color:#64748b;">{today} · 数据截至上一交易日收盘 · 非投资建议,自己判断</span></div>')

    # 总览
    h.append('<div style="background:#fff;border-radius:10px;padding:12px 16px;margin-top:12px;border:1px solid #e2e8f0;font-size:13px;color:#475569;">'
             f'总资产 <b style="color:#1f2937;">{total:,.0f}</b> 元 ｜ 持仓 {len(funds) + n_wallet} 笔 ｜ '
             f'权益/黄金 <b style="color:#1f2937;">{n_equity}</b> 笔 ｜ 债基 {n_bond} 笔 ｜ 货币 1 笔</div>')

    # 权益/黄金
    h.append('<div style="background:#fff;border-radius:10px;padding:14px 16px;margin-top:10px;border:1px solid #e2e8f0;">')
    h.append('<div style="font-size:15px;font-weight:700;color:#0f766e;margin-bottom:8px;">■ 权益 / 黄金</div>')
    h.append('<table style="width:100%;border-collapse:collapse;font-size:13px;">')
    h.append('<tr style="color:#64748b;font-size:11px;"><th style="text-align:left;padding:4px;">基金</th>'
             '<th style="text-align:right;padding:4px;">金额</th><th style="text-align:right;padding:4px;">20日</th>'
             '<th style="text-align:center;padding:4px;">双轮判断</th><th style="text-align:left;padding:4px;">理由</th></tr>')
    for f, act, why in dec:
        if f["fundCode"] in BOND_SET:
            continue
        mcol = "#64748b"
        chg = mom.get(f["fundCode"], {}).get("chg_20d")
        chg_txt = f"{chg:+.1f}%" if chg is not None else "—"
        if chg is not None:
            mcol = UP if chg >= 0 else DOWN
        acol = act_color.get(act, "#1f2937")
        h.append(f'<tr><td style="padding:5px 4px;font-weight:600;">{f["fundName"]}</td>'
                 f'<td style="padding:5px 4px;text-align:right;">{float(f["totalAmount"] or 0):,.0f}</td>'
                 f'<td style="padding:5px 4px;text-align:right;font-weight:600;color:{mcol};">{chg_txt}</td>'
                 f'<td style="padding:5px 4px;text-align:center;font-weight:700;color:{acol};">{act}</td>'
                 f'<td style="padding:5px 4px;color:#64748b;font-size:12px;">{why}</td></tr>')
    h.append('</table>')
    h.append('<div style="margin-top:6px;font-size:11.5px;color:#94a3b8;">红=相对偏强,绿=相对偏弱,橙=中性或需留意。估值分位越低越便宜,PE分位&lt;40%为低位,&gt;70%为偏高。</div>')
    h.append('</div>')

    # 固收+ / 债券 / 货币
    h.append('<div style="background:#fff;border-radius:10px;padding:14px 16px;margin-top:10px;border:1px solid #e2e8f0;">')
    h.append('<div style="font-size:15px;font-weight:700;color:#7c3aed;margin-bottom:8px;">■ 债基 / 固收+ / 货币</div>')
    h.append('<table style="width:100%;border-collapse:collapse;font-size:13px;">')
    h.append('<tr style="color:#64748b;font-size:11px;"><th style="text-align:left;padding:4px;">基金</th>'
             '<th style="text-align:right;padding:4px;">金额</th><th style="text-align:center;padding:4px;">类别/状态</th>'
             '<th style="text-align:left;padding:4px;">理由</th></tr>')
    for f, act, why in dec:
        if f["fundCode"] not in BOND_SET:
            continue
        acol = act_color.get(act, "#1f2937")
        h.append(f'<tr><td style="padding:5px 4px;font-weight:600;">{f["fundName"]}</td>'
                 f'<td style="padding:5px 4px;text-align:right;">{float(f["totalAmount"] or 0):,.0f}</td>'
                 f'<td style="padding:5px 4px;text-align:center;font-weight:700;color:{acol};">{act}</td>'
                 f'<td style="padding:5px 4px;color:#64748b;font-size:12px;">{why}</td></tr>')
    if wallet:
        acol = act_color.get("现金类", "#64748b")
        h.append(f'<tr><td style="padding:5px 4px;font-weight:600;">{wallet.get("fundName","货币钱包")}</td>'
                 f'<td style="padding:5px 4px;text-align:right;">{float(wallet.get("avaiableVol") or 0):,.0f}</td>'
                 f'<td style="padding:5px 4px;text-align:center;font-weight:700;color:{acol};">现金类</td>'
                 f'<td style="padding:5px 4px;color:#64748b;font-size:12px;">昨日收益 {wallet.get("profits","—")} 元,累计 {wallet.get("holdProfits","—")} 元</td></tr>')
    h.append('</table>')
    h.append('</div>')

    # 规则说明
    h.append('<div style="background:#eff6ff;border-left:3px solid #1d4ed8;border-radius:6px;padding:10px 12px;margin-top:10px;font-size:12px;color:#334155;line-height:1.9;">'
             '<b style="color:#1d4ed8;">💡 判断规则(技术面+基本面双轮)</b><br>'
             '1. 技术面: 看基金净值 20/60 日动量 — 向上=趋势好, 向下=趋势差<br>'
             '2. 基本面: 看对应指数 PE 历史分位 — 分位越低代表相对便宜,越高代表相对偏贵<br>'
             '3. 估值低 + 动量向上记为双轮偏强;估值贵 + 动量向下记为双轮偏弱<br>'
             '4. 债基/货币只说明组合角色和利率风险;黄金无常规估值,主要看趋势<br>'
             '5. 所有结论均为观察标签,不是交易指令</div>')

    h.append('<div style="margin-top:10px;font-size:11px;color:#94a3b8;text-align:center;">'
             '数据来源: 爱基金持仓 / 天天基金净值 / 蛋卷估值。数据可能存在延迟,仅供观察参考,不构成投资建议。</div>')
    h.append('</div>')
    return "\n".join(h)


# ---------- 6. 发送 ----------
def agently(*args):
    r = subprocess.run(["cmd", "/c", AGENTLY_CMD] + list(args), capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=120, cwd=BASE)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def send_mail(subject, body):
    code = os.environ.get("QQ_SMTP_CODE")
    if code:  # 云端模式: QQ 邮箱 SMTP 直发
        import smtplib
        from email.header import Header
        from email.mime.text import MIMEText
        msg = MIMEText(body, "html", "utf-8")
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = SMTP_USER
        msg["To"] = SMTP_USER
        try:
            with smtplib.SMTP_SSL("smtp.qq.com", 465, timeout=30) as s:
                s.login(SMTP_USER, code)
                s.send_message(msg)
            return True, "SMTP ok"
        except Exception as e:  # noqa: BLE001
            return False, f"SMTP fail: {e}"
    # 本地模式: agently-cli 两阶段确认
    body_file = "holdings_body.html"
    with open(os.path.join(BASE, body_file), "w", encoding="utf-8") as f:
        f.write(body)
    code1, out1 = agently("message", "+send", "--to", RECIPIENT, "--subject", subject,
                          "--body-file", body_file)
    if code1 != 0:
        return False, out1
    m = re.search(r'"confirmation_token":\s*"([^"]+)"', out1)
    if not m:
        return False, f"无法获取确认令牌: {out1[:300]}"
    ctk = m.group(1)
    code2, out2 = agently("message", "+send", "--to", RECIPIENT, "--subject", subject,
                          "--body-file", body_file, "--confirmation-token", ctk)
    return code2 == 0, out2


# ---------- main ----------
def main():
    force = "--force" in sys.argv
    dry = "--dry" in sys.argv
    today = dt.date.today().isoformat()

    try:
        ensure_cloud_credentials()
        if not force and not is_trading_day():
            print(f"[{today}] 非交易日, 跳过发送")
            return
        if not force and os.path.exists(SENT_MARKER):
            print(f"[{today}] 今日已发送过, 跳过(防重复)")
            return

        agg = fetch_holdings()
        codes = [f["fundCode"] for f in agg.get("funds", [])]
        mom = fetch_momentum(codes)
        eva = fetch_valuation()

        subject = f"{SUBJECT_PREFIX} {today} ({dt.datetime.now().strftime('%H:%M')})"
        body = render_html(agg, mom, eva)

        if dry:
            fp = os.path.join(BASE, "holdings_dry.html")
            with open(fp, "w", encoding="utf-8") as f:
                f.write(body)
            print(f"[{today}] DRY: 已生成 {fp}")
            return

        ok, resp = send_mail(subject, body)
        if ok:
            with open(SENT_MARKER, "w", encoding="utf-8") as f:
                f.write(json.dumps({"sent_at": dt.datetime.now().isoformat()}, ensure_ascii=False))
            print(f"[{today}] 持仓观察邮件已发送 -> {RECIPIENT}")
            log_run("SENT ok")
        else:
            print(f"[{today}] 发送失败: {resp[:500]}")
            log_run(f"SEND FAIL {resp[:300]}")
            sys.exit(1)
    except Exception as e:
        log_run(f"ERROR {type(e).__name__}: {e}")
        print(f"[{today}] 异常: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
