# -*- coding: utf-8 -*-
"""成分股市场宽度快照(阶段 C): 白酒/医疗/科创50 当前截面宽度指标。

背景: 无历史成分股快照, 无法回填历史(任务书 §11) -> 从本期起每周缓存一次
当前截面统计, 为 8~12 周影子观察积累真实历史宽度。
指标(第一版, 简单可验证):
  - above_ma250: 250日均线以上成分比例
  - up20_ratio:  近20日上涨成分比例
  - direction:   等权20日涨跌 vs 指数(市值加权)20日涨跌 方向(1 同向 / -1 背离 / 0 中性)
输出: data/cons_snapshots/<date>.json(只存聚合统计, 不存原始K线)。
"""
import json, os, sys, time, datetime as dt
import requests
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import datahub

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "data", "cons_snapshots")
HDR = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Referer": "https://gu.qq.com/"}

# 指数代码 -> 名称 (中证官网成分接口)
CONS_INDEXES = {
    "399997": "中证白酒",
    "399989": "中证医疗",
    "000688": "科创50",
}
# 指数行情 symbol(算等权 vs 市值加权方向)
INDEX_PX = {"399997": "sz399997", "399989": "sz399989", "000688": "sh000688"}


def tencent_kline(symbol, days=800):
    """腾讯前复权日 K -> (dates, closes)。失败返回 None。"""
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},day,,,{days},qfq"
    try:
        r = requests.get(url, headers=HDR, timeout=20)
        j = r.json()
        data = j.get("data", {}).get(symbol, {})
        arr = data.get("qfqday") or data.get("day") or []
        if len(arr) < 260:
            return None
        return [row[0] for row in arr], [float(row[2]) for row in arr]
    except Exception as e:
        print(f"  [snapshot] K线失败 {symbol}: {e}")
        return None


def breadth_stats(closes):
    """closes: 个股前复权日K升序 -> (above_ma250, up20_ratio 布尔)"""
    if len(closes) < 251:
        return None
    last = closes[-1]
    ma250 = sum(closes[-250:]) / 250
    above = last > ma250
    up20 = last > closes[-21]
    return above, up20


def main():
    today = dt.date.today().isoformat()
    os.makedirs(OUT_DIR, exist_ok=True)
    out_path = os.path.join(OUT_DIR, today + ".json")
    if os.path.exists(out_path):
        print(f"{out_path} 已存在, 跳过(当天快照已有)")
        return

    snap = {"date": today, "indexes": {}}
    for icode, iname in CONS_INDEXES.items():
        cons = datahub.index_cons(icode)
        if cons is None:
            snap["indexes"][iname] = {"error": "成分股列表获取失败"}
            continue
        rows = []
        above_n = up20_n = valid = 0
        eq_ret20 = 0.0
        for _, row in cons.iterrows():
            code = str(row["成分券代码"]).zfill(6)
            symbol = ("sh" if code[0] in "6" else "sz") + code
            k = tencent_kline(symbol)
            if k is None:
                continue
            dates, closes = k
            stats = breadth_stats(closes)
            if stats is None:
                continue
            above, up20 = stats
            above_n += 1 if above else 0
            up20_n += 1 if up20 else 0
            valid += 1
            eq_ret20 += closes[-1] / closes[-21] - 1
            rows.append({"code": code, "name": str(row["成分券名称"]), "above_ma250": above, "up20": up20})
            time.sleep(0.08)   # 温和限速
        if valid == 0:
            snap["indexes"][iname] = {"error": "成分股行情不可用", "cons": len(cons)}
            continue
        eq_ret20 /= valid
        # 指数自身(市值加权)20日涨跌
        idx_k = tencent_kline(INDEX_PX[icode])
        idx_ret20 = None
        if idx_k is not None:
            idx_ret20 = idx_k[1][-1] / idx_k[1][-21] - 1
        direction = None
        if idx_ret20 is not None:
            direction = 1 if (eq_ret20 > 0) == (idx_ret20 > 0) and abs(eq_ret20 - idx_ret20) < 0.05 else \
                        -1 if (eq_ret20 > 0) != (idx_ret20 > 0) and abs(eq_ret20 - idx_ret20) > 0.02 else 0
        snap["indexes"][iname] = {
            "cons": len(cons), "valid": valid,
            "above_ma250_ratio": round(above_n / valid, 3) if valid else None,
            "up20_ratio": round(up20_n / valid, 3) if valid else None,
            "eq_ret20": round(eq_ret20, 4),
            "index_ret20": round(idx_ret20, 4) if idx_ret20 is not None else None,
            "direction": direction,
        }
        print(f"  {iname}: 成分{len(cons)} 有效{valid} 均线上方{snap['indexes'][iname]['above_ma250_ratio']} "
              f"近20日上涨{snap['indexes'][iname]['up20_ratio']} 方向{direction}")

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=1)
    print(f"已保存 {out_path}")


if __name__ == "__main__":
    main()
