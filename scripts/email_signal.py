# -*- coding: utf-8 -*-
"""
每日信号邮件: 运行 daily_signal.py -> 判断是否交易日 -> 发送结果到邮箱
======================================================================
- 非交易日(节假日/周末)自动跳过, 不发邮件
- 用户已预授权本定时任务自动发送(两阶段确认在脚本内自动完成)
- 用法: py -3 email_signal.py [--force]   (--force 强制发送, 用于测试)
"""
import sys, os, subprocess, datetime as dt

BASE = os.path.dirname(os.path.abspath(__file__))
SIGNAL_SCRIPT = os.path.join(BASE, "daily_signal.py")
RECIPIENT = "thfwj@qq.com"
SUBJECT_PREFIX = "投资信号"

def run_signal():
    """运行 daily_signal.py, 返回 (stdout文本, 是否交易日)"""
    r = subprocess.run([sys.executable, SIGNAL_SCRIPT],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=300, cwd=BASE)
    out = (r.stdout or "") + (r.stderr or "")
    # 交易日判断: 实时数据日期非今日 -> 非交易日
    is_trading_day = "非今日交易数据" not in out and "实时行情获取失败" not in out
    return out, is_trading_day

AGENTLY_CMD = r"C:\Users\thfwj\.workbuddy\binaries\node\versions\22.22.2\agently-cli.cmd"

def agently(*args):
    """调用 agently-cli(在 BASE 目录运行, 支持相对路径参数), 返回 (exit_code, stdout)"""
    r = subprocess.run(["cmd", "/c", AGENTLY_CMD] + list(args), capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=120, cwd=BASE)
    return r.returncode, (r.stdout or "") + (r.stderr or "")

def send_mail(subject, body):
    """云端模式: QQ_SMTP_CODE 环境变量存在 -> SMTP 直发; 否则本地 agently 两阶段。"""
    smtp_code = os.environ.get("QQ_SMTP_CODE")
    if smtp_code:
        import smtplib
        from email.header import Header
        from email.mime.text import MIMEText
        msg = MIMEText(body, "html", "utf-8")
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = RECIPIENT
        msg["To"] = RECIPIENT
        try:
            with smtplib.SMTP_SSL("smtp.qq.com", 465, timeout=30) as s:
                s.login(RECIPIENT, smtp_code)
                s.send_message(msg)
            return True, "SMTP ok"
        except Exception as e:  # noqa: BLE001
            return False, f"SMTP fail: {e}"
    """两阶段: 拿 ctk -> 带 ctk 发送。body 走相对路径文件避免 cmd 转义问题。"""
    import re
    body_file = "signal_body.html"  # 相对路径, 相对 BASE(cwd)
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

def format_body(out):
    """解析 daily_signal 输出 -> 美观 HTML 邮件正文"""
    mom, mom_notes, sigs, notes, tips = parse_output(out)
    today = dt.date.today().isoformat()
    # 中国习惯: 红涨绿跌
    UP, DOWN = "#dc2626", "#16a34a"
    sig_color = {"偏强": "#dc2626", "偏弱": "#16a34a", "中性": "#d97706", "反复": "#9ca3af"}

    h = []
    h.append(f'<div style="max-width:620px;margin:0 auto;font-family:-apple-system,Segoe UI,Microsoft YaHei,sans-serif;color:#1f2937;background:#f8fafc;padding:16px;border-radius:12px;">')
    h.append(f'<div style="text-align:center;padding:14px 0 6px;"><span style="font-size:22px;font-weight:700;">📈 投资参考</span><br>'
             f'<span style="font-size:13px;color:#64748b;">{today}</span></div>')

    # 动量数据
    h.append(f'<div style="background:#fff;border-radius:10px;padding:14px 16px;margin-top:12px;border:1px solid #e2e8f0;">')
    h.append(f'<div style="font-size:15px;font-weight:700;color:#0f766e;margin-bottom:8px;">■ 动量数据(40日涨幅)</div>')
    if mom:
        h.append('<table style="width:100%;border-collapse:collapse;font-size:14px;">')
        h.append('<tr style="color:#64748b;font-size:12px;"><th style="text-align:left;padding:4px 6px;">排名</th>'
                 '<th style="text-align:left;padding:4px 6px;">资产</th><th style="text-align:right;padding:4px 6px;">40日动量</th></tr>')
        for rank, name, mp, _sg in mom:
            mcol = UP if not mp.startswith("-") else DOWN
            h.append(f'<tr><td style="padding:5px 6px;color:#64748b;">{rank}</td>'
                     f'<td style="padding:5px 6px;font-weight:600;">{name}</td>'
                     f'<td style="padding:5px 6px;text-align:right;font-weight:600;color:{mcol};">{mp}</td></tr>')
        h.append('</table>')
    # 数据说明
    h.append('<div style="margin-top:8px;font-size:11.5px;color:#94a3b8;">40日动量 = 最近40个交易日涨幅,数字越大说明近期涨得越好。</div>')
    h.append('</div>')

    # 三维度信号
    if sigs:
        h.append(f'<div style="background:#fff;border-radius:10px;padding:14px 16px;margin-top:10px;border:1px solid #e2e8f0;">')
        h.append(f'<div style="font-size:15px;font-weight:700;color:#1d4ed8;margin-bottom:8px;">■ 三维度信号(参考)</div>')
        h.append('<table style="width:100%;border-collapse:collapse;font-size:13px;">')
        h.append('<tr style="color:#64748b;font-size:11px;"><th style="text-align:left;padding:4px;">资产</th>'
                 '<th style="text-align:right;padding:4px;">现价</th><th style="text-align:right;padding:4px;">今日%</th>'
                 '<th style="text-align:center;padding:4px;">V</th><th style="text-align:center;padding:4px;">T</th>'
                 '<th style="text-align:center;padding:4px;">M</th><th style="text-align:center;padding:4px;">S</th>'
                 '<th style="text-align:center;padding:4px;">判断</th></tr>')
        for name, spot, chg, v, t, m, s, sg in sigs:
            scol = sig_color.get(sg, "#1f2937")
            sval = f'<span style="font-weight:700;color:{scol};">{sg}</span>'
            vc = UP if (v.startswith("+")) else (DOWN if v.startswith("-") else "#64748b")
            h.append(f'<tr><td style="padding:4px;font-weight:600;">{name}</td>'
                     f'<td style="padding:4px;text-align:right;">{spot}</td>'
                     f'<td style="padding:4px;text-align:right;">{chg}</td>'
                     f'<td style="padding:4px;text-align:center;color:{vc};">{v}</td>'
                     f'<td style="padding:4px;text-align:center;">{t}</td>'
                     f'<td style="padding:4px;text-align:center;">{m}</td>'
                     f'<td style="padding:4px;text-align:center;font-weight:700;">{s}</td>'
                     f'<td style="padding:4px;text-align:center;">{sval}</td></tr>')
        h.append('</table>')
        h.append('</div>')
        # 指标说明
        h.append('<div style="background:#eff6ff;border-left:3px solid #1d4ed8;border-radius:6px;padding:10px 12px;margin-top:8px;'
                 'font-size:12px;color:#334155;line-height:1.9;">'
                 '<b style="color:#1d4ed8;">💡 指标怎么看</b><br>'
                 'V 估值分: +2 = 便宜,-2 = 贵(看PE/PB/股息率的历史位置)<br>'
                 'T 趋势分: +2 = 上涨趋势,-2 = 下跌趋势(看20/60日均线)<br>'
                 'M 动能分: +2 = 短期上涨有延续,-2 = 下跌动能较强(看RSI和近5日涨跌)<br>'
                 '过热/超跌只作风险提示,不反向计入M分<br>'
                 'S 总分: V+T+M,正数偏强,负数偏弱；仅供观察,不构成买卖指令</div>')

    # 理由
    if notes:
        h.append(f'<div style="background:#fff;border-radius:10px;padding:14px 16px;margin-top:10px;border:1px solid #e2e8f0;">')
        h.append(f'<div style="font-size:15px;font-weight:700;color:#7c3aed;margin-bottom:6px;">■ 理由</div>')
        for n in notes:
            name, _, rest = n.partition(":")
            items = [x.strip() for x in rest.split(";") if x.strip()]
            h.append(f'<div style="margin-top:6px;font-size:13px;">')
            h.append(f'<b style="color:#1f2937;">{name}</b>')
            for it in items:
                h.append(f'<div style="margin-left:14px;color:#475569;line-height:1.7;">· {it}</div>')
            h.append('</div>')
        h.append('</div>')

    # 提示
    if tips:
        t = " | ".join(tips)
        h.append(f'<div style="margin-top:10px;font-size:11px;color:#94a3b8;text-align:center;">{t}</div>')

    h.append('</div>')
    return "\n".join(h)

def parse_output(out):
    """解析 daily_signal 输出 -> (mom, mom_note, sigs, notes, tips)"""
    mom, mom_notes, sigs, notes, tips = [], [], [], [], []
    in_mom, seen_sep = False, False
    for raw in out.splitlines():
        l = raw.strip()
        if "动量数据(" in l or "动量轮动(" in l:
            in_mom = True
            continue
        if in_mom:
            if l.startswith("--"):
                if not seen_sep:
                    seen_sep = True
                    continue
                else:
                    in_mom = False
                    continue
            if l.startswith(">>"):
                mom_notes.append(l)
                continue
            if l.startswith("排名") or not l:
                continue
            parts = l.split()
            if len(parts) >= 3 and parts[0].isdigit():
                mom.append((parts[0], parts[1], parts[2],
                            parts[3] if len(parts) >= 4 else ""))
            continue
        if l.startswith(("红利低波", "沪深300", "黄金")) and any(x in l for x in ("偏强", "偏弱", "中性", "反复")) and "≈" not in l:
            parts = l.split()
            if len(parts) >= 8:
                sigs.append((parts[0], parts[1], parts[2], parts[3], parts[4], parts[5], parts[6], parts[7]))
            continue
        if "股息率" in l or "PE≈" in l or "乖离" in l:
            notes.append(l)
            continue
        if "非观察窗口" in l or "注意:" in l or "中债10Y" in l:
            tips.append(l)
    return mom, mom_notes, sigs, notes, tips

def main():
    force = "--force" in sys.argv or os.path.exists(os.path.join(BASE, "FORCE_SEND"))
    out, is_trading_day = run_signal()
    today = dt.date.today().isoformat()

    if not is_trading_day and not force:
        print(f"[{today}] 非交易日, 跳过发送")
        return

    subject = f"{SUBJECT_PREFIX} {today} ({dt.datetime.now().strftime('%H:%M')})"
    ok, resp = send_mail(subject, format_body(out))
    if ok:
        print(f"[{today}] 信号邮件已发送 -> {RECIPIENT}")
    else:
        print(f"[{today}] 发送失败: {resp[:500]}")
        sys.exit(1)

if __name__ == "__main__":
    main()
