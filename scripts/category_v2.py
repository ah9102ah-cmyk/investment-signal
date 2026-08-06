# -*- coding: utf-8 -*-
"""category_v2_shadow 策略引擎（阶段 B 影子版本 + 2026-08 更正版，纯规则层）。

更正版要点(2026-08 复审修正):
- 动能口径: 5日收益 = close[t]/close[t-5]-1(此前少1天); 20日波动率 = 最近20个完整日收益(此前19个)。
- 综合分: 直接基于原始分项 × 原始配置权重 / 可用权重(P2-1: 此前结构轮混合后按估值权重加权, 宽度权重丢失)。
  新增独立字段 valuation_score / macro_score / breadth_score / trend_score / momentum_score /
  structural_score / technical_score / composite_score / available_weight_ratio。
- 类别规则全部落实: 高PE不直接卖科技(只封顶买入强度)、红利趋势保护、行业便宜+下跌不买、
  黄金宏观缺失/严重陈旧不出买入强信号、沪深300/中证500进入门槛分开。
- 数据协议: 每个外部字段携带 source/data_date/fetched_at/staleness_days/status/fallback_source;
  超过陈旧阈值不得标记"完整", 严重陈旧不得输出买入强信号。

设计原则(任务书):
- 只做指数层, 不输出个性化仓位/交易数量。
- 分项分数 -2..+2; 缺失项不静默按 0 处理(剔除并标注 degraded)。
- 波动率标准化是 V2 第一优先级: 动能 = 5日标准化变化, 只研究 ±0.5/±1.0/±1.5σ 三档。
- 黄金结构轮 G = 宏观环境(美债方向+美元方向) + 价格位置(250日线乖离), 250日线只计一次, 不进价格轮。
- 双轮确认: 结构轮(structural) + 价格轮(technical); 三态为固定规则观察结果。
- 恒生科技走数据不足通道, 不伪造估值。
这里不抓数据、不发邮件; Python 回测与页面 JS 共用同一套判定。
"""
from __future__ import annotations

import datetime as dt
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


def available_weight_ratio(raw, weights, name=None, category=None):
    """可用权重/配置总权重。可用于判断数据覆盖率(P2-1 规则4/5)。

    v4 修正(2026-08-06): 与 composite_score 使用同一张有效权重映射
    (_component_map), 行业主题的盈利代理槽位(与宽度对半/全额)不再算 0 权重,
    否则\"仅有盈利代理可用\"时可用权重会被低估。"""
    total = sum(weights.values())
    if total <= 0:
        return 0.0
    if name is None:
        usable = sum(weights.get(key, 0.0) for key, score in raw.items()
                     if score is not None and math.isfinite(float(score)))
    else:
        comps, comp_w = _component_map(name, category, weights, raw)
        usable = sum(comp_w.get(key, 0.0) for key in comps)
    return usable / total


def _component_map(name, category, weights, raw):
    """原始分项 -> (components, comp_weights): 综合分与可用权重共用的唯一映射。

    黄金: 结构轮 G(=宏观+250线位置等权) 占 macro 权重槽; 250线只计一次。
    行业主题: 盈利周期与市场宽度共用 breadth 权重槽(都可用时对半拆分)。"""
    components = {}
    comp_weights = {}
    if name == "黄金":
        macro = raw.get("macro")
        position = raw.get("position")
        g = None
        if macro is not None:
            g = weighted_combine({"macro": macro, "position": position},
                                 {"macro": 1.0, "position": 1.0})
        if g is not None:
            components["macro"] = g
            comp_weights["macro"] = weights.get("macro", 1.0)
    else:
        val = raw.get("valuation")
        if val is not None:
            components["valuation"] = val
            comp_weights["valuation"] = weights.get("valuation", 1.0)
        brd = raw.get("breadth")
        earn = raw.get("earnings")
        w_slot = weights.get("breadth", 0.0)
        if brd is not None and earn is not None and category == "行业主题":
            components["breadth"] = brd
            comp_weights["breadth"] = w_slot / 2.0
            components["earnings"] = earn
            comp_weights["earnings"] = w_slot / 2.0
        elif brd is not None:
            components["breadth"] = brd
            comp_weights["breadth"] = w_slot
        elif earn is not None and category == "行业主题":
            components["earnings"] = earn
            comp_weights["earnings"] = w_slot
    trend = raw.get("trend")
    if trend is not None:
        components["trend"] = trend
        comp_weights["trend"] = weights.get("trend", 1.0)
    momentum = raw.get("momentum")
    if momentum is not None:
        components["momentum"] = momentum
        comp_weights["momentum"] = weights.get("momentum", 1.0)
    return components, comp_weights


# ---------------------------------------------------------------- 统一协议
def composite_score(name, category, weights, raw):
    """综合分(P2-1 更正): 直接基于原始分项 × 原始配置权重 / 可用权重。

    v4: 分项->权重的映射统一走 _component_map, 与 available_weight_ratio 完全一致。"""
    components, comp_weights = _component_map(name, category, weights, raw)
    return weighted_combine(components, comp_weights, min_weight=cfg.COMPOSITE_MIN_WEIGHT)


# ---------------------------------------------------------------- 分项(价格轮)
def five_day_return(close, spot, days=cfg.VOL_DAYS):
    """5日收益 = close[t]/close[t-5]-1 (P1-2 统一口径, 与页面 JS 一致)。

    返回 None 表示数据不足(不静默当 0)。"""
    close = clean(close)
    if len(close) < days + 1 or spot is None:
        return None
    return spot / close[-(days + 1)] - 1


def volatility_returns(close, window=cfg.VOL_WINDOW):
    """最近 window 个完整日收益(P1-2: 必须恰好 window 个, 此前少算1个)。

    返回 [] 表示数据不足。"""
    close = clean(close)
    if len(close) < window + 1:
        return []
    out = []
    for i in range(len(close) - window, len(close)):
        if close[i - 1] > 0:
            out.append(close[i] / close[i - 1] - 1)
    return out


def std_momentum(close, spot, window=cfg.VOL_WINDOW, days=cfg.VOL_DAYS):
    """5日标准化变化 = 近5日收益 ÷ (近window日日波动率 × √5)。None=数据不足。

    更正(2026-08): 5日收益 = close[t]/close[t-5]-1(此前少1天);
    波动率窗口 = 最近 window 个完整日收益(20个, 此前19个)。"""
    close = clean(close)
    if len(close) < window + 2 or spot is None:
        return None
    ret5 = five_day_return(close, spot, days)
    if ret5 is None:
        return None
    rets = volatility_returns(close, window)
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
    """中长期趋势分(-2..+2): 160/200/240 日涨幅共识(多数向上且平均正=强)。

    v4 修正(2026-08-06): 周期差一天。close 最后一项是评估日收盘(==spot),
    h 日涨幅必须用 close[-(h+1)](h 个交易日前), 旧代码 close[-h] 实际是 (h-1) 日涨幅。
    与 common_v1_actions 的 close.iloc[i-h] 口径一致。"""
    close = clean(close)
    if len(close) < max(LONG_TREND_HORIZONS) + 1 or spot is None:
        return None, ["趋势历史不足"]
    moves = [spot / close[-(h + 1)] - 1 for h in LONG_TREND_HORIZONS]
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
    if len(close) >= max(LONG_TREND_HORIZONS) + 1 and spot is not None:
        details["moves"] = {h: round(spot / close[-(h + 1)] - 1, 4) for h in LONG_TREND_HORIZONS}
    return score, degraded, details


# ---------------------------------------------------------------- 分项(结构轮)
def valuation_score_v2(name, category, pe_history, pe_now,
                       pb_history=None, pb_now=None, erp_history=None, erp_now=None,
                       div_history=None, div_now=None):
    """结构估值分(-2..+2)。按类别口径, 缺失项标 degraded。

    P2-1: 本函数只返回纯估值分, 不得等于整个结构轮。"""
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


def gold_macro_score(us10y_history, dollar_history=None, dollar_now=None, window=20):
    """黄金宏观环境 G-宏观子项: 美债10Y方向 + 美元指数方向(实际利率无免费源, 显式代理)。

    利率明显下降(>0.05pp/20交易日)对黄金相对有利; 美元指数上升对黄金不利。
    美元指数只有当前值/无历史时: 不计分, 必须降级标注, 不得假装完整(P1-4)。"""
    degraded = []
    us = clean(us10y_history)
    if len(us) < window + 1:
        return None, ["宏观数据不足"]
    diff = us[-window - 1] - us[-1]     # 利率下降 -> 正值
    if diff > 0.15:
        rate_score = 2
    elif diff > 0.05:
        rate_score = 1
    elif diff < -0.15:
        rate_score = -2
    elif diff < -0.05:
        rate_score = -1
    else:
        rate_score = 0
    comps = {"us10y": rate_score}
    comp_weights = {"us10y": 1.0}
    dl = clean(dollar_history)
    if len(dl) >= window + 1:
        ddiff = dl[-1] - dl[-window - 1]    # 美元指数上升 -> 负分
        if ddiff > 2:
            dollar_score = -2
        elif ddiff > 0.8:
            dollar_score = -1
        elif ddiff < -2:
            dollar_score = 2
        elif ddiff < -0.8:
            dollar_score = 1
        else:
            dollar_score = 0
        comps["dollar"] = dollar_score
        comp_weights["dollar"] = 1.0
    elif dollar_now is not None:
        degraded.append("美元指数无历史(仅当前值)")
    else:
        degraded.append("美元指数缺失")
    return weighted_combine(comps, comp_weights, min_weight=0.5), degraded


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


def earnings_cycle_score(earnings_cycle):
    """盈利周期分(-2..+2): 成分股盈利改善比例(行业主题)。

    无可靠数据时必须返回 None + 显式标记缺失, 不得按 0 分处理(P1-4)。"""
    if earnings_cycle is None:
        return None, ["盈利周期缺失"]
    if not math.isfinite(float(earnings_cycle)):
        return None, ["盈利周期缺失"]
    ratio = float(earnings_cycle)
    if ratio >= 0.70:
        return 2, []
    if ratio >= 0.55:
        return 1, []
    if ratio >= 0.45:
        return 0, []
    if ratio >= 0.30:
        return -1, []
    return -2, []


# ---------------------------------------------------------------- 合成
def structural_score(name, category, weights, *, close=None, spot=None,
                     pe_history=None, pe_now=None, pb_history=None, pb_now=None,
                     erp_history=None, erp_now=None, div_history=None, div_now=None,
                     us10y_history=None, dollar_history=None, dollar_now=None,
                     breadth_ratio=None, earnings_cycle=None):
    """结构轮分(-2..+2, 解释字段)。子项缺失标 degraded, 可用权重不足返回 None。

    P2-1: 结构轮只作解释, 综合分不得拿它再按估值权重加权。"""
    degraded = []
    if name == "黄金":
        macro, md = gold_macro_score(us10y_history, dollar_history, dollar_now)
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
    # 非黄金: 估值 + 宽度(+行业盈利周期)
    val, vd = valuation_score_v2(name, category, pe_history, pe_now,
                                 pb_history, pb_now, erp_history, erp_now,
                                 div_history, div_now)
    degraded += vd
    breadth = breadth_score_now(breadth_ratio)
    if breadth_ratio is not None:
        degraded.append("宽度仅当前截面")
    comps = {}
    comp_weights = {}
    if val is not None:
        comps["valuation"] = val
        comp_weights["valuation"] = weights.get("valuation", 1.0)
    if breadth is not None:
        comps["breadth"] = breadth
        comp_weights["breadth"] = weights.get("breadth", 0.0)
    elif "breadth" in weights:
        degraded.append("宽度缺失")
    if category == "行业主题":
        # 盈利周期与市场宽度共用"盈利周期/市场宽度"权重槽: 都可用时对半拆分
        earn, ed = earnings_cycle_score(earnings_cycle)
        degraded += ed
        if earn is not None:
            w_slot = weights.get("breadth", 0.0)
            if breadth is not None:
                comps["earnings"] = earn
                comp_weights["earnings"] = w_slot / 2.0
                comp_weights["breadth"] = w_slot / 2.0
            else:
                comps["earnings"] = earn
                comp_weights["earnings"] = w_slot
    if val is None:
        return None, degraded + ["估值数据不足"]
    # 宽度/盈利缺失时结构轮退化为估值单因子(权重归一化), 不算 0 分
    score = weighted_combine(comps, comp_weights, min_weight=0.0)
    return score, degraded


def technical_score(name, weights, *, close=None, spot=None, rsi_value=None):
    """价格轮分(-2..+2, 解释字段): 中长期趋势 + 波动率标准化动能, 按候选权重合并。"""
    trend, td = trend_score_v2(close, spot)
    momentum, md = momentum_score_v2(close, spot, rsi_value)
    degraded = td + md
    if trend is None or momentum is None:
        return None, degraded
    w_t = weights.get("trend", 1.0)
    w_m = weights.get("momentum", 1.0)
    score = (trend * w_t + momentum * w_m) / (w_t + w_m)
    return max(-2.0, min(2.0, score)), degraded


# ---------------------------------------------------------------- 陈旧度
def _date_of(value):
    if value is None:
        return None
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def staleness_days(data_dates, signal_date):
    """{字段: 日期} -> {字段: 自然日陈旧数}。缺日期/无法解析的字段不输出。

    v4 修正(2026-08-06): 数据日期晚于信号日时必须保留负值(future),
    禁止用 max(0) 把未来数据伪装成新鲜数据; 未来字段由 _status_for 标 invalid,
    调用方必须把 future 字段排除在评分之外。"""
    base = _date_of(signal_date) or dt.date.today()
    out = {}
    for field, d in (data_dates or {}).items():
        dd = _date_of(d)
        if dd is None:
            continue
        out[field] = (base - dd).days
    return out


def _canonical_field(field):
    """字段 -> 陈旧度阈值别名(v4.1): pb/erp→valuation, us10y/dollar→macro。"""
    return cfg.FIELD_ALIASES.get(field, field)


def _status_for(field, days):
    """状态判断(v4.1): 阈值统一走 canonical 字段(别名共用同一函数)。"""
    th = cfg.STALENESS_THRESHOLDS.get(_canonical_field(field), {"stale": 10, "severe": 20})
    if days is None:
        return "unknown"
    if days < 0:
        return "future"
    if days >= th["severe"]:
        return "severe"
    if days >= th["stale"]:
        return "stale"
    return "ok"


def _staleness_note(field, days, status):
    """降级说明(v4.1): 与 _status_for 共用同一 canonical 字段口径。"""
    if status == "future":
        return f"{field}数据日期晚于信号日(future)"
    if status == "severe":
        return f"{field}数据严重陈旧({days}天)"
    if status == "stale":
        return f"{field}数据陈旧({days}天)"
    return None


# ---------------------------------------------------------------- 统一协议
def _asset_thresholds(name, candidate):
    t = dict(cfg.THRESHOLDS.get(candidate, cfg.THRESHOLDS["balanced"]))
    off = cfg.ASSET_ENTER_OFFSETS.get(name, {})
    for k in ("enter_structural", "enter_technical", "enter_momentum"):
        t[k] = t[k] + off.get(k, 0.0)
    return t


def final_action_v2(name, category, structural, technical, momentum, composite,
                    degraded, weights, candidate="balanced",
                    valuation_score=None, trend_score=None,
                    breadth_score=None, earnings_score=None):
    """三态判定(固定规则, 不连接账户)。

    买入: 双轮确认 + 候选门槛(估值优先重结构轮, 趋势优先重价格轮)。
    卖出: 中长期多数向下且动能转弱 / 结构轮明显恶化且价格轮转弱 / 综合弱项达门槛。
    持有: 其余。恒生科技/缺关键数据 -> 数据不足。

    类别规则(P1-4 落实, 配置里的 CATEGORY_RULES 必须真正执行):
    - 高成长科技: 高PE只限制买入强度, 不直接触发卖出(卖出由趋势+动能驱动)。
    - 红利因子: 趋势向下时不得仅因便宜买入(慢趋势保护)。
    - 行业主题: 便宜+继续下跌不买; 结构轮只剩估值(无宽度/盈利)时需更强价格确认。
    - 实物资产: 宏观缺失/严重陈旧不得输出买入强信号。
    """
    if name == "恒生科技":
        return ("数据不足", "缺少可靠历史估值且行情长度不足，暂不下结论", "估值缺失")
    if category == "实物资产" and "宏观数据不足" in degraded:
        return ("数据不足", "宏观环境数据缺失，不出强信号", "宏观缺失")
    if structural is None or technical is None:
        return ("数据不足", "结构轮或价格轮数据不完整，暂不下结论", "数据不足")

    t = _asset_thresholds(name, candidate)
    enter = (
        structural >= t["enter_structural"]
        and technical >= t["enter_technical"]
        and momentum >= t["enter_momentum"]
        and composite is not None
    )
    rules_cfg = cfg.CATEGORY_RULES.get(category, {})

    # 行业主题: "估值便宜 + 继续下跌"不得触发买入
    if rules_cfg.get("no_buy_cheap_downtrend"):
        if structural >= 0 and technical <= -1 and momentum <= 0:
            enter = False
        # 行业基本面缺失(宽度与盈利周期都没有)时, 不允许仅靠结构薄弱+价格超跌买入
        if breadth_score is None and earnings_score is None and technical < 2:
            enter = False

    # 红利因子: 趋势主要用于防止"便宜就抄底"
    if rules_cfg.get("slow_trend_guard"):
        if trend_score is not None and trend_score < 0:
            enter = False

    # 高成长科技: PE 高只限制买入强度, 不单独触发卖出
    high_pe_guard = rules_cfg.get("high_pe_no_sell")
    if high_pe_guard and valuation_score is not None:
        if valuation_score <= -2:
            enter = False
        elif valuation_score <= -1 and technical < 2:
            enter = False

    long_negative = technical <= -1
    if high_pe_guard:
        # 科技类: 估值弱不直接触发卖出(估值只作护栏);
        # 综合弱项退出也要求价格面同步走弱(卖出主要由趋势+动能转弱触发)
        structural_exit = False
        composite_exit = composite is not None and composite <= cfg.EXIT_COMPOSITE and long_negative
    else:
        structural_exit = structural <= -1 and technical <= 0
        composite_exit = composite is not None and composite <= cfg.EXIT_COMPOSITE
    exit_now = (
        (long_negative and momentum <= -1)
        or structural_exit
        or composite_exit
    )
    if exit_now:
        if long_negative and momentum <= -1:
            return ("卖出", "中长期多数周期向下，短期动能也已转弱", "")
        if structural_exit:
            return ("卖出", "估值结构与价格走势同步转弱", "")
        return ("卖出", "综合弱项达到退出门槛", "")
    if enter:
        return ("买入", "结构轮与价格轮双轮确认，短期动能没有拖后腿", "")
    return ("持有", "双轮尚未同时确认，继续观察", "")


def compute_signal(name, *, close=None, spot=None, pe_history=None, pe_now=None,
                   pb_history=None, pb_now=None, erp_history=None, erp_now=None,
                   div_history=None, div_now=None, us10y_history=None,
                   dollar_history=None, dollar_now=None,
                   breadth_ratio=None, earnings_cycle=None,
                   rsi_value=None, candidate="balanced",
                   signal_date=None, data_dates=None, data_meta=None) -> dict:
    """统一信号协议输出(任务书 §6 + P2-1/P2-2 更正)。

    输入均为截至评估日的序列/当前值。data_dates = {字段: "YYYY-MM-DD"}(行情/估值/宏观/宽度日期),
    data_meta = {字段: {"source","data_date","fetched_at","fallback_source"}}。
    返回信号 dict, 包含全部原始分项、结构轮/价格轮/综合分、可用权重占比与每个字段的陈旧度状态。
    """
    category, cluster = cfg.CATEGORY_MAP.get(name, ("未分类", "未分类"))
    # no_candidate: 计算仍用该资产自身 balanced 权重作研究参考, 动作由调用方沿用 common_v1(P1-3)
    cand_for_weights = "balanced" if candidate == "no_candidate" else candidate
    weights = cfg.CANDIDATES.get(name, {}).get(cand_for_weights) or cfg.CANDIDATES["沪深300"]["balanced"]

    degraded = []
    if cfg.DATA_AVAILABILITY.get(name) == "limited":
        degraded.append("估值缺失")
        degraded.append("行情历史不足")

    # v4.1: 计算所有分项之前, 按字段日期过滤未来输入。
    # 未来估值/PB/ERP/宽度/美债/美元/盈利 必须等同于缺失: 置 None 后不参与任何分项/结构轮/综合分。
    # 生产路径(build_signal_for)已按信号日截断, 此处是引擎层硬保障, 防止直接调用方混入未来数据。
    future_fields = sorted(f for f, d in (staleness_days(data_dates, signal_date) or {}).items()
                           if d is not None and d < 0)
    if future_fields:
        degraded.append("未来数据已按缺失处理: " + ",".join(future_fields))
        if any(f in future_fields for f in ("valuation", "pb", "erp")):
            pe_history = pe_now = None
            pb_history = pb_now = None
            erp_history = erp_now = None
            div_history = div_now = None
        if "breadth" in future_fields:
            breadth_ratio = None
        if any(f in future_fields for f in ("us10y", "dollar")):
            us10y_history = None
            dollar_history = None
            dollar_now = None
        if "earnings" in future_fields:
            earnings_cycle = None

    # 原始分项
    val = vd = None
    if name != "黄金":
        val, vd = valuation_score_v2(
            name, category, pe_history, pe_now,
            pb_history, pb_now, erp_history, erp_now, div_history, div_now)
        degraded += vd
    macro = md = None
    if name == "黄金":
        macro, md = gold_macro_score(us10y_history, dollar_history, dollar_now)
        degraded += md
    breadth = breadth_score_now(breadth_ratio)
    if breadth_ratio is not None:
        degraded.append("宽度仅当前截面")
    elif breadth is None and name != "恒生科技" and "breadth" in weights:
        degraded.append("宽度缺失")
    earn = ed = None
    if category == "行业主题":
        earn, ed = earnings_cycle_score(earnings_cycle)
        degraded += ed

    # 价格轮分项
    trend, td = trend_score_v2(close, spot)
    degraded += td
    momentum, md2 = momentum_score_v2(close, spot, rsi_value)
    degraded += [item for item in md2 if item not in degraded]

    # 结构轮/价格轮(解释字段, P2-1: 不用于综合分加权)
    structural, sd = structural_score(
        name, category, weights, close=close, spot=spot,
        pe_history=pe_history, pe_now=pe_now, pb_history=pb_history, pb_now=pb_now,
        erp_history=erp_history, erp_now=erp_now, div_history=div_history, div_now=div_now,
        us10y_history=us10y_history, dollar_history=dollar_history, dollar_now=dollar_now,
        breadth_ratio=breadth_ratio, earnings_cycle=earnings_cycle)
    degraded += [item for item in sd if item not in degraded]
    technical, td2 = technical_score(name, weights, close=close, spot=spot, rsi_value=rsi_value)
    degraded += [item for item in td2 if item not in degraded]

    # 黄金 250线位置(结构轮内部, 不进价格轮)
    position = None
    if name == "黄金":
        position, _ = gold_price_position(close, spot)

    # 综合分: 原始分项 × 原始配置权重(P2-1)
    raw = {"valuation": val, "macro": macro, "breadth": breadth,
           "trend": trend, "momentum": momentum, "earnings": earn,
           "position": position}
    composite = composite_score(name, category, weights, raw)
    avail_ratio = available_weight_ratio(raw, weights, name, category)

    # 陈旧度协议(P2-2 + v4 future 标记)
    stale = staleness_days(data_dates, signal_date)
    sources = {}
    for field in ("price", "valuation", "pb", "erp", "us10y", "dollar", "breadth", "earnings"):
        days = stale.get(field)
        if days is None and field not in (data_dates or {}):
            continue
        meta = (data_meta or {}).get(field, {})
        sources[field] = {
            "source": meta.get("source"),
            "data_date": meta.get("data_date") or (data_dates or {}).get(field),
            "fetched_at": meta.get("fetched_at"),
            "staleness_days": days,
            "status": _status_for(field, days),
            "fallback_source": meta.get("fallback_source"),
        }
        note = _staleness_note(field, days, sources[field]["status"])
        if note:
            degraded.append(note)
    # 严重陈旧/未来数据 -> 不得标记完整 / 不得输出买入强信号
    severe_fields = [f for f, s in sources.items()
                     if s["status"] in ("severe", "future")
                     and f in ("price", "valuation", "pb", "erp", "us10y", "dollar", "breadth")]
    price_stale = sources.get("price", {}).get("status") in ("severe", "future")
    future_fields = [f for f, s in sources.items() if s["status"] == "future"]

    # 数据质量
    if structural is None and "估值缺失" in degraded:
        quality = "数据不足"
    elif structural is None or technical is None:
        quality = "数据不足"
    elif price_stale:
        quality = "数据不足"
    elif future_fields:
        quality = "降级"
    elif degraded:
        quality = "降级"
    else:
        quality = "完整"
    unique_degraded = sorted(set(degraded))

    action, reason, quality_tag = final_action_v2(
        name, category, structural, technical, momentum, composite, unique_degraded,
        weights, candidate=candidate,
        valuation_score=val, trend_score=trend,
        breadth_score=breadth, earnings_score=earn)
    if quality_tag:
        quality = quality_tag

    # 严重陈旧/未来数据封顶: 宏观/结构数据严重陈旧或晚于信号日不得输出买入强信号(P2-2 规则3 + v4)
    macro_or_struct_severe = any(f in severe_fields for f in ("us10y", "dollar", "valuation", "pb", "erp", "breadth"))
    if action == "买入" and macro_or_struct_severe:
        action = "持有"
        reason = "估值或宏观数据严重陈旧/日期晚于信号日，暂不出买入信号"
        if quality == "完整":
            quality = "降级"
    if action == "买入" and price_stale:
        action = "持有"
        reason = "行情数据严重陈旧/日期晚于信号日，暂不出买入信号"
        if quality == "完整":
            quality = "降级"

    return {
        "strategy_version": cfg.STRATEGY_VERSION,
        "strategy_type": category,
        "asset_name": name,
        "valuation_score": round(val, 2) if val is not None else None,
        "macro_score": round(macro, 2) if macro is not None else None,
        "breadth_score": round(breadth, 2) if breadth is not None else None,
        "earnings_score": round(earn, 2) if earn is not None else None,
        "trend_score": round(trend, 2) if trend is not None else None,
        "momentum_score": momentum,
        "quality_score": None,
        "structural_score": round(structural, 2) if structural is not None else None,
        "technical_score": round(technical, 2) if technical is not None else None,
        "composite_score": round(composite, 2) if composite is not None else None,
        "available_weight_ratio": round(avail_ratio, 3),
        "action": action,
        "action_reason": reason,
        "data_quality": quality,
        "degraded_fields": unique_degraded,
        "data_sources": sources,
        "class_rank": None,
        "risk_cluster": cluster,
        "risk_warning": None,
        "signal_date": signal_date,
        "valuation_date": (data_dates or {}).get("valuation"),
        "price_date": (data_dates or {}).get("price"),
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
            "valuation_score", "macro_score", "breadth_score",
            "trend_score", "momentum_score",
            "structural_score", "technical_score", "composite_score",
            "available_weight_ratio", "data_quality", "degraded_fields")}, ensure_ascii=False))
