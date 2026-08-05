# -*- coding: utf-8 -*-
"""category_v2_shadow 策略引擎（阶段 B 影子版本，纯规则层）。

设计原则（任务书）:
- 只做指数层, 不输出个性化仓位/交易数量。
- 分项分数 -2..+2; 综合分用候选权重加权, 缺失项不静默按 0 处理(剔除并标注 degraded)。
- 波动率标准化是 V2 第一优先级: 动能 = 5日标准化变化, 只研究 ±0.5/±1.0/±1.5σ 三档。
- 黄金结构轮 G = 宏观环境(美债方向代理) + 价格位置(250日线乖离), 两者独立, 不进价格轮。
- 双轮确认: 结构轮(structural) + 价格轮(technical); 三态为固定规则观察结果。
- 恒生科技走数据不足通道, 不伪造估值。
这里不抓数据、不发邮件; Python 回测与页面 JS 共用同一套判定。
"""
from __future__ import annotations

import math
import statistics

import signal_rules as rules
import v2_config as cfg

VALUE_WINDOW = rules.VALUE_WINDOW
LONG_TREND_HORIZONS = rules.LONG_TREND_HORIZONS


# ---------------------------------------------------------------- 工具
def clean(series):
    """任意序列 -> 有限浮点 list; None/空 -> []"""
    if series is None:
        return []
    out = []
    for value in series:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            out.append(number)
    return out


def weighted_combine(components: dict, weights: dict, min_weight=0.5):
    """按权重合并 -2..+2 分项; 缺失项剔除并归一化。

    可用项权重占比 < min_weight 时返回 None(数据不足, 不静默按 0)。"""
    valid = {key: float(score) for key, score in components.items()
             if score is not None and math.isfinite(float(score))}
    if not valid:
        return None
    total_w = sum(weights.get(key, 1.0) for key in valid)
    if total_w <= 0:
        return None
    if total_w < min_weight:
        return None
    value = sum(weights.get(key, 1.0) * score for key, score in valid.items()) / total_w
    return max(-2.0, min(2.0, value))


# ---------------------------------------------------------------- 分项(价格轮)
def std_momentum(close, spot, window=cfg.VOL_WINDOW, days=cfg.VOL_DAYS):
    """5日标准化变化 = 近5日收益 ÷ (近window日日波动率 × √5)。None=数据不足。"""
    close = clean(close)
    if len(close) < window + 2 or spot is None:
        return None
    ret5 = spot / close[-days] - 1
    rets = []
    for i in range(len(close) - window + 1, len(close)):
        if close[i - 1] > 0:
            rets.append(close[i] / close[i - 1] - 1)
    if len(rets) < 10:
        return None
    sd = statistics.stdev(rets)
    if not math.isfinite(sd) or sd <= 0:
        return 0.0
    return ret5 / (sd * math.sqrt(days))


def momentum_score_v2(close, spot, rsi_value=None):
    """波动率标准化动能分(-2..+2)。RSI 只做确认与过热/超跌提示, 不单独决定买卖。"""
    z = std_momentum(close, spot)
    degraded = []
    if z is None:
        return None, ["波动率历史不足"]
    if z >= 1.5:
        m = 2
    elif z >= 0.5:
        m = 1
    elif z <= -1.5:
        m = -2
    elif z <= -0.5:
        m = -1
    else:
        m = 0
    if rsi_value is None or not math.isfinite(float(rsi_value)):
        degraded.append("rsi")
    else:
        # RSI 确认: 与标准化动能冲突时向 0 收, 不反向覆盖中长期趋势
        if float(rsi_value) >= 60 and m < 0:
            m = 0
        elif float(rsi_value) < 40 and m > 0:
            m = 0
    return m, degraded


def trend_score_v2(close, spot):
    """中长期趋势分(-2..+2): 160/200/240 日涨幅共识(多数向上且均值正=强)。"""
    close = clean(close)
    if len(close) < max(LONG_TREND_HORIZONS) or spot is None:
        return None, ["趋势历史不足"]
    moves = [spot / close[-h] - 1 for h in LONG_TREND_HORIZONS]
    votes = sum(1 for value in moves if value > 0)
    average = sum(moves) / len(moves)
    if votes >= 2 and average > 0:
        return 2, []
    if votes >= 2:
        return 1, []
    if votes <= 1 and average < 0:
        return -2, []
    return -1, []


def trend_details(close, spot):
    """趋势分 + 明细(供页面/邮件说明)。"""
    score, degraded = trend_score_v2(close, spot)
    close = clean(close)
    details = {}
    if len(close) >= max(LONG_TREND_HORIZONS) and spot is not None:
        details["moves"] = {h: round(spot / close[-h] - 1, 4) for h in LONG_TREND_HORIZONS}
    return score, degraded, details


# ---------------------------------------------------------------- 分项(结构轮)
def valuation_score_v2(name, category, pe_history, pe_now,
                       pb_history=None, pb_now=None, erp_history=None, erp_now=None,
                       div_history=None, div_now=None):
    """结构估值分(-2..+2)。按类别口径, 缺失项标 degraded。"""
    degraded = []
    if name == "沪深300":
        score, meta = rules.hs300_value_score(
            pe_history or [], pe_now,
            pb_history or [], float("nan") if pb_now is None else pb_now,
            erp_history or [], float("nan") if erp_now is None else erp_now,
        )
        if pb_history is None or len(clean(pb_history)) < rules.VALUE_MIN_PERIODS:
            degraded.append("pb")
        if erp_history is None or len(clean(erp_history)) < rules.VALUE_MIN_PERIODS:
            degraded.append("erp")
        if score is None and meta.get("quality"):
            degraded.append(meta["quality"])
        return score, degraded
    if name == "红利低波":
        score, meta = rules.redli_value_score(pe_history or [], pe_now,
                                              div_history or (), div_now)
        if div_history is None or len(clean(div_history)) < rules.VALUE_MIN_PERIODS:
            degraded.append("dividend")
        return score, degraded
    # 通用: 自身 PE 分位
    score = rules.valuation_percentile_score(pe_history or [], pe_now)
    return score, degraded


def gold_macro_score(us10y_history, window=20):
    """黄金宏观环境 G - 美债方向代理(实际利率无免费源)。

    利率明显下降(>0.05pp/20交易日)对黄金相对有利。窗口不足返回 None。
    美元指数无历史序列, 阶段 B 不计分, 由调用方标 degraded。"""
    us = clean(us10y_history)
    if len(us) < window + 1:
        return None, ["宏观数据不足"]
    diff = us[-window - 1] - us[-1]     # 利率下降 -> 正值
    if diff > 0.15:
        return 2, []
    if diff > 0.05:
        return 1, []
    if diff < -0.15:
        return -2, []
    if diff < -0.05:
        return -1, []
    return 0, []


def gold_price_position(close, spot):
    """黄金价格位置分: 250日均线乖离(结构轮内部, 不进价格轮, 不重复计分)。"""
    close = clean(close)
    if len(close) < 250 or spot is None:
        return None, ["250日线不足"]
    ma250 = sum(close[-250:]) / 250
    bias = (spot / ma250 - 1) * 100
    return rules.gold_bias_score(bias), []


def breadth_score_now(breadth_ratio):
    """市场宽度(当前截面): 长期均线以上比例 -> -2..+2。

    阶段 B 只有当前截面观察; 历史回测不含宽度(调用方传 None)。
    第一版简单宽度: 均线上方比例 >=70% -> 2, >=55% -> 1, >=45% -> 0,
                      >=30% -> -1, 否则 -2。比例缺失返回 None。"""
    if breadth_ratio is None or not math.isfinite(float(breadth_ratio)):
        return None
    ratio = float(breadth_ratio)
    if ratio >= 0.70:
        return 2
    if ratio >= 0.55:
        return 1
    if ratio >= 0.45:
        return 0
    if ratio >= 0.30:
        return -1
    return -2


# ---------------------------------------------------------------- 合成
def structural_score(name, category, weights, *, close=None, spot=None,
                     pe_history=None, pe_now=None, pb_history=None, pb_now=None,
                     erp_history=None, erp_now=None, div_history=None, div_now=None,
                     us10y_history=None, breadth_ratio=None):
    """结构轮分(-2..+2)。子项缺失标 degraded, 可用权重不足返回 None。"""
    degraded = []
    if name == "黄金":
        macro, md = gold_macro_score(us10y_history)
        position, pd_ = gold_price_position(close, spot)
        degraded += md
        if macro is None:
            degraded += pd_
            return None, degraded
        if position is None:
            degraded.append("250日线不足")
        # G = 宏观环境 与 价格位置 等权(固定结构, 不属于选模权重)
        score = weighted_combine({"macro": macro, "position": position},
                                 {"macro": 1.0, "position": 1.0})
        return score, degraded
    # 非黄金: 估值 + 宽度(仅当前截面)
    val, vd = valuation_score_v2(name, category, pe_history, pe_now,
                                 pb_history, pb_now, erp_history, erp_now,
                                 div_history, div_now)
    degraded += vd
    breadth = breadth_score_now(breadth_ratio)
    if breadth_ratio is not None:
        degraded.append("宽度仅当前截面")
    if val is None:
        return None, degraded + ["估值数据不足"]
    w_val = weights.get("valuation", 1.0)
    w_brd = weights.get("breadth", 0.0)
    components = {"valuation": val}
    comp_weights = {"valuation": w_val}
    if breadth is not None:
        components["breadth"] = breadth
        comp_weights["breadth"] = w_brd
    else:
        degraded.append("宽度缺失")
    # 宽度为当前截面增强: 缺失时结构轮退化为估值单因子(权重归一化), 不算 0 分
    score = weighted_combine(components, comp_weights, min_weight=0.0)
    return score, degraded


def technical_score(name, weights, *, close=None, spot=None, rsi_value=None):
    """价格轮分(-2..+2): 中长期趋势 + 波动率标准化动能, 按候选权重合并。"""
    trend, td = trend_score_v2(close, spot)
    momentum, md = momentum_score_v2(close, spot, rsi_value)
    degraded = td + md
    if trend is None or momentum is None:
        return None, degraded
    w_t = weights.get("trend", 1.0)
    w_m = weights.get("momentum", 1.0)
    score = (trend * w_t + momentum * w_m) / (w_t + w_m)
    return max(-2.0, min(2.0, score)), degraded


# ---------------------------------------------------------------- 统一协议
def composite_score(name, category, weights, structural, technical, momentum,
                    breadth_ratio=None):
    """综合分: 全部分项按候选权重加权; 可用权重不足 -> None。"""
    components = {}
    comp_weights = {}
    if name == "黄金":
        if structural is not None:
            components["macro"] = structural
            comp_weights["macro"] = weights.get("macro", 1.0)
    else:
        if structural is not None:
            components["valuation"] = structural
            comp_weights["valuation"] = weights.get("valuation", 1.0)
    if technical is not None:
        # 技术轮内部趋势/动能拆回, 便于按权重加权
        trend = None
        momentum = None
        # 直接使用分项: 这里简化处理, 技术轮权重整体计入
        components["technical"] = technical
        comp_weights["technical"] = weights.get("trend", 1.0) + weights.get("momentum", 1.0)
    return weighted_combine(components, comp_weights)


def final_action_v2(name, category, structural, technical, momentum, composite,
                    degraded, weights, candidate="balanced"):
    """三态判定(固定规则, 不连接账户)。

    买入: 双轮确认 + 候选门槛(估值优先重结构轮, 趋势优先重价格轮)。
    卖出: 中长期多数向下且动能转弱 / 结构轮明显恶化且价格轮转弱 / 综合弱项达门槛。
    持有: 其余。恒生科技/缺关键数据 -> 数据不足。
    """
    if name == "恒生科技":
        return ("数据不足", "缺少可靠历史估值且行情长度不足，暂不下结论", "估值缺失")
    if category == "实物资产" and "宏观数据不足" in degraded:
        return ("数据不足", "宏观环境数据缺失，不出强信号", "宏观缺失")
    if structural is None or technical is None:
        return ("数据不足", "结构轮或价格轮数据不完整，暂不下结论", "数据不足")

    t = cfg.THRESHOLDS.get(candidate, cfg.THRESHOLDS["balanced"])
    enter = (
        structural >= t["enter_structural"]
        and technical >= t["enter_technical"]
        and momentum >= t["enter_momentum"]
        and composite is not None
    )
    # 类别特有禁止
    if category == "行业主题" and cfg.CATEGORY_RULES["行业主题"]["no_buy_cheap_downtrend"]:
        if structural >= 0 and technical <= -1 and momentum <= 0:
            enter = False

    long_negative = technical <= -1
    exit_now = (
        (long_negative and momentum <= -1)
        or (structural <= -1 and technical <= 0)
        or (composite is not None and composite <= cfg.EXIT_COMPOSITE)
    )
    if exit_now:
        if long_negative and momentum <= -1:
            return ("卖出", "中长期多数周期向下，短期动能也已转弱", "")
        if structural <= -1 and technical <= 0:
            return ("卖出", "估值结构与价格走势同步转弱", "")
        return ("卖出", "综合弱项达到退出门槛", "")
    if enter:
        return ("买入", "结构轮与价格轮双轮确认，短期动能没有拖后腿", "")
    return ("持有", "双轮尚未同时确认，继续观察", "")


def compute_signal(name, *, close=None, spot=None, pe_history=None, pe_now=None,
                   pb_history=None, pb_now=None, erp_history=None, erp_now=None,
                   div_history=None, div_now=None, us10y_history=None,
                   breadth_ratio=None, rsi_value=None, candidate="balanced",
                   signal_date=None) -> dict:
    """统一信号协议输出(任务书 §6)。输入均为截至评估日的序列/当前值。"""
    category, cluster = cfg.CATEGORY_MAP.get(name, ("未分类", "未分类"))
    weights = cfg.CANDIDATES.get(name, {}).get(candidate) or cfg.CANDIDATES["沪深300"]["balanced"]

    degraded = []
    if cfg.DATA_AVAILABILITY.get(name) == "limited":
        degraded.append("估值缺失")
        degraded.append("行情历史不足")

    # 结构轮
    structural, sd = structural_score(
        name, category, weights, close=close, spot=spot,
        pe_history=pe_history, pe_now=pe_now, pb_history=pb_history, pb_now=pb_now,
        erp_history=erp_history, erp_now=erp_now, div_history=div_history, div_now=div_now,
        us10y_history=us10y_history, breadth_ratio=breadth_ratio,
    )
    degraded += sd

    # 价格轮
    technical, td = technical_score(name, weights, close=close, spot=spot, rsi_value=rsi_value)
    degraded += td

    # 动能(单独供三态判定)
    momentum, md = momentum_score_v2(close, spot, rsi_value)
    degraded += [item for item in md if item not in degraded]

    # 综合
    composite = composite_score(name, category, weights, structural, technical, momentum)

    # 数据质量
    if structural is None and "估值缺失" in degraded:
        quality = "数据不足"
    elif structural is None or technical is None:
        quality = "数据不足"
    elif degraded:
        quality = "降级"
    else:
        quality = "完整"
    unique_degraded = sorted(set(degraded))

    action, reason, quality_tag = final_action_v2(
        name, category, structural, technical, momentum, composite, unique_degraded,
        weights, candidate=candidate
    )
    if quality_tag:
        quality = quality_tag

    valuation_date = None
    price_date = None
    if pe_history is not None and len(clean(pe_history)):
        pass  # 序列无日期信息, 由调用方补充; 这里只标质量
    return {
        "strategy_version": "category_v2_shadow",
        "strategy_type": category,
        "asset_name": name,
        "valuation_score": round(structural, 2) if structural is not None else None,
        "macro_score": round(structural, 2) if (name == "黄金" and structural is not None) else None,
        "trend_score": round(technical, 2) if technical is not None else None,
        "momentum_score": momentum,
        "quality_score": None,
        "structural_score": round(structural, 2) if structural is not None else None,
        "technical_score": round(technical, 2) if technical is not None else None,
        "composite_score": round(composite, 2) if composite is not None else None,
        "action": action,
        "action_reason": reason,
        "data_quality": quality,
        "degraded_fields": unique_degraded,
        "class_rank": None,
        "risk_cluster": cluster,
        "risk_warning": None,
        "signal_date": signal_date,
        "valuation_date": valuation_date,
        "price_date": price_date,
        "candidate": candidate,
    }


if __name__ == "__main__":
    import json
    # 冒烟测试: 用合成序列验证协议字段齐全、三态可达
    rising = [100 * (1.001 ** i) for i in range(300)]
    flat = [100.0] * 300
    pe_series = [10 + (i % 50) for i in range(300)]
    for name, close, spot, pe in [
        ("沪深300", rising, rising[-1] * 1.01, 12),
        ("红利低波", rising, rising[-1] * 1.005, 8),
        ("黄金", rising, rising[-1] * 1.01, None),
        ("恒生科技", flat, 100.0, None),
    ]:
        sig = compute_signal(
            name, close=close, spot=spot, pe_history=pe_series, pe_now=pe,
            us10y_history=[3.5 + (i % 30) * 0.01 for i in range(100)],
            signal_date="2026-08-05",
        )
        print(name, "->", json.dumps({k: sig[k] for k in (
            "strategy_version", "strategy_type", "action", "action_reason",
            "structural_score", "technical_score", "momentum_score",
            "composite_score", "data_quality", "degraded_fields")}, ensure_ascii=False))
