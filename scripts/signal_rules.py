# -*- coding: utf-8 -*-
"""基金信号的纯规则层。

这里不抓数据、不发邮件，只负责把同一组输入转换成同一组分数。
网页数据生成、每日信号和回测都应复用这里，避免同一资产出现两套估值结论。
"""
from __future__ import annotations

import math
import statistics
from typing import Iterable, Mapping


VALUE_WINDOW = 1250       # 约 5 个交易年
VALUE_MIN_PERIODS = 60
LONG_TREND_HORIZONS = (160, 200, 240)
TRADING_DAYS = 244


def _clean_tail(values: Iterable[float], window: int = VALUE_WINDOW) -> list[float]:
    clean = []
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            clean.append(number)
    return clean[-window:]


def percentile_rank(values: Iterable[float], value: float, *, window: int = VALUE_WINDOW,
                    min_periods: int = VALUE_MIN_PERIODS) -> float | None:
    """返回 value 在最近 window 个有效值中的百分位(0~100)。"""
    try:
        current = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(current):
        return None
    sample = _clean_tail(values, window)
    if len(sample) < min_periods:
        return None
    return sum(item < current for item in sample) / len(sample) * 100


def percentile_bucket(percentile: float | None) -> int | None:
    """把百分位映射到 -2..+2；百分位越高，分数越高。"""
    if percentile is None:
        return None
    if percentile >= 70:
        return 2
    if percentile >= 50:
        return 1
    if percentile >= 30:
        return 0
    if percentile >= 15:
        return -1
    return -2


def valuation_percentile_score(values: Iterable[float], value: float, *,
                               higher_is_cheaper: bool = False) -> int | None:
    """历史分位估值分；PE/PB 越低越便宜，股息率/股债利差越高越便宜。"""
    bucket = percentile_bucket(percentile_rank(values, value))
    if bucket is None:
        return None
    return bucket if higher_is_cheaper else -bucket


def combine_scores(components: Mapping[str, int | None], *, min_components: int = 1) -> tuple[int | None, str]:
    """等权合并估值子项，并返回数据质量说明。

    Python 的 round 在 x.5 时会向偶数靠拢，正好让两项意见相反或证据不足时更保守地靠近 0。
    """
    valid = {name: int(score) for name, score in components.items() if score is not None}
    total = len(components)
    if len(valid) < min_components:
        return None, f"数据不足({len(valid)}/{total})"
    score = int(round(sum(valid.values()) / len(valid)))
    quality = "完整" if len(valid) == total else f"降级({len(valid)}/{total})"
    return max(-2, min(2, score)), quality


def redli_value_score(pe_history: Iterable[float], pe_now: float,
                      dividend_history: Iterable[float] = (), dividend_now: float | None = None) -> tuple[int | None, dict]:
    """红利低波：PE 分位为可回测主项；有足够股息率历史时才合并股息率。"""
    pe_score = valuation_percentile_score(pe_history, pe_now)
    dividend_score = valuation_percentile_score(
        dividend_history, dividend_now, higher_is_cheaper=True
    )
    if dividend_score is None:
        score = pe_score
        quality = "完整(PE；股息率仅作背景)" if pe_score is not None else "数据不足"
    else:
        score, quality = combine_scores(
            {"pe": pe_score, "dividend": dividend_score}, min_components=1
        )
    return score, {"pe": pe_score, "dividend": dividend_score, "quality": quality}


def hs300_value_score(pe_history: Iterable[float], pe_now: float,
                      pb_history: Iterable[float], pb_now: float,
                      erp_history: Iterable[float], erp_now: float) -> tuple[int | None, dict]:
    """沪深300：PE、PB、股债利差三项等权；至少两项可用才给分。"""
    components = {
        "pe": valuation_percentile_score(pe_history, pe_now),
        "pb": valuation_percentile_score(pb_history, pb_now),
        "erp": valuation_percentile_score(erp_history, erp_now, higher_is_cheaper=True),
    }
    score, quality = combine_scores(components, min_components=2)
    return score, {**components, "quality": quality}


def gold_bias_score(bias_percent: float) -> int | None:
    """黄金没有普通 PE，用相对 250 日均线的乖离作为价格位置分。"""
    try:
        bias = float(bias_percent)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(bias):
        return None
    if bias < -15:
        return 2
    if bias < -5:
        return 1
    if bias < 5:
        return 0
    if bias < 15:
        return -1
    return -2


def select_positive_momentum(momentum: Mapping[str, float], top_k: int = 2) -> list[str]:
    """只返回正动量资产，最多 top_k 个；全部为负时返回空列表。"""
    positive = [(name, value) for name, value in momentum.items() if value > 0]
    positive.sort(key=lambda item: item[1], reverse=True)
    return [name for name, _ in positive[:top_k]]


def multi_horizon_trend(history: Iterable[float], spot: float,
                        horizons: tuple[int, ...] = LONG_TREND_HORIZONS) -> dict | None:
    """计算多个中长期涨幅；过半周期向上且平均为正才算趋势共识。"""
    prices = _clean_tail(history, window=max(horizons))
    try:
        current = float(spot)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(current) or len(prices) < max(horizons):
        return None
    changes = {horizon: current / prices[-horizon] - 1 for horizon in horizons}
    average = sum(changes.values()) / len(changes)
    positive_votes = sum(value > 0 for value in changes.values())
    required_votes = len(horizons) // 2 + 1
    return {
        "changes": changes,
        "average": average,
        "positive_votes": positive_votes,
        "required_votes": required_votes,
        "eligible": positive_votes >= required_votes and average > 0,
    }


def annualized_volatility(history: Iterable[float], spot: float, window: int = 60) -> float | None:
    """近 window 个交易日的年化波动率，供风险提示和逆波动研究使用。"""
    prices = _clean_tail(history, window=window)
    try:
        current = float(spot)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(current) or len(prices) < window:
        return None
    path = prices + [current]
    returns = [path[i] / path[i - 1] - 1 for i in range(1, len(path)) if path[i - 1] > 0]
    if len(returns) < 2:
        return None
    return statistics.stdev(returns) * math.sqrt(TRADING_DAYS)


def select_multi_horizon_trend(signals: Mapping[str, dict | None], top_k: int = 2) -> list[str]:
    """从已经形成中长期共识的资产里取平均趋势较强者，仅用于观察排名。"""
    eligible = [
        (name, signal["average"])
        for name, signal in signals.items()
        if signal is not None and signal.get("eligible")
    ]
    eligible.sort(key=lambda item: item[1], reverse=True)
    return [name for name, _ in eligible[:top_k]]


def final_action(v: int | None, t: int | None, m: int | None,
                 long_average: float | None, long_votes: int | None) -> tuple[str, str]:
    """把双轮判断收束为三态策略结论，不连接账户也不执行交易。

    “持有”表示没有触发换向：已有仓位可理解为继续持有，未持有则理解为继续观望。
    """
    if None in (v, t, m, long_average, long_votes):
        return "数据不足", "估值或中长期趋势数据不完整，暂不下结论"

    total = int(v) + int(t) + int(m)
    long_positive = int(long_votes) >= 2 and float(long_average) > 0
    long_negative = int(long_votes) <= 1 and float(long_average) < 0
    enter = long_positive and int(v) >= -1 and int(t) >= 0 and int(m) >= -1
    exit_now = (
        total <= -4
        or (int(t) <= -2 and int(v) <= -1)
        or (long_negative and int(t) <= -1)
    )

    if exit_now:
        if long_negative and int(t) <= -1:
            return "卖出", "中长期多数周期向下，短期趋势也已转弱"
        return "卖出", "估值与短期走势的弱项已经达到退出门槛"
    if enter:
        return "买入", "中长期多数周期向上，估值不过分贵，短期走势没有拖后腿"
    if long_positive:
        return "持有", "中长期方向仍向上，但估值或短期走势尚未同时确认"
    return "持有", "中长期方向尚未形成明确反转，继续等待双轮确认"
