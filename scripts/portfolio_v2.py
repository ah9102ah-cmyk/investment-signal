# -*- coding: utf-8 -*-
"""组合层: 类内排名 + 风险集中提示(阶段 C)。

输入: 各资产 category_v2 信号(dict, compute_signal 输出) + 年化波动率映射。
输出: 每个信号补充 class_rank; 返回风险提示列表(大白话, 由页面渲染)。
只输出观察提示和类内优先级, 不输出具体仓位(任务书 §10)。
"""
from __future__ import annotations

EQUITY_CLUSTERS = {"大盘权益", "中小盘权益", "防守/红利", "高成长科技", "消费行业", "医疗行业"}


def rank_score(sig: dict, vol=None) -> float:
    """风险调整后的类内排序分: 趋势确认 - 波动惩罚 + 数据完整度。

    跨类别不比较原始综合分(任务书 §6), 排序只用风险调整趋势 + 结构确认 + 数据完整度。
    """
    technical = sig.get("technical_score")
    if technical is None:
        technical = -2.0
    score = float(technical)
    if vol is not None and vol > 0:
        score -= min(float(vol), 0.6) * 2.0        # 年化波动 0~60% -> 惩罚 0~1.2
    quality = sig.get("data_quality")
    if quality == "完整":
        score += 0.3
    elif quality == "降级":
        score -= 0.2
    return score


def apply_portfolio(signals: dict, vol_map: dict | None = None) -> list[str]:
    """signals: {资产名: category_v2 信号}; vol_map: {资产名: 年化波动(0~1)}。

    返回风险提示列表; 同时就地给每个信号补 class_rank。"""
    vol_map = vol_map or {}
    warnings = []

    # 按风险簇分组 + 类内排名
    clusters: dict[str, list] = {}
    for name, sig in signals.items():
        cluster = sig.get("risk_cluster") or "未分类"
        clusters.setdefault(cluster, []).append((name, sig))
    for cluster, items in clusters.items():
        items.sort(key=lambda pair: rank_score(pair[1], vol_map.get(pair[0])), reverse=True)
        for rank, (name, sig) in enumerate(items, 1):
            sig["class_rank"] = rank
        if len(items) >= 2:
            # 同风险簇(类)多个"买入": 给类内优先级(排名已写在 class_rank)
            buys = [name for name, sig in items if sig.get("action") == "买入"]
            if len(buys) >= 2:
                order = "、".join(f"{name}(类内第{next(r for r, (n, _) in enumerate(items, 1) if n == name)}优先)" for name in buys)
                warnings.append(f"{cluster}里同时出现多个买入，类内优先级：{order}")

    # 高成长科技同时买入 -> 科技风险集中
    buys_all = {name: sig for name, sig in signals.items() if sig.get("action") == "买入"}
    tech_buys = [name for name, sig in buys_all.items() if sig.get("risk_cluster") == "高成长科技"]
    if len(tech_buys) >= 2:
        warnings.append("科创50与恒生科技同时偏强，科技类风险集中，注意不要重复押注同一方向")

    # 沪深300 与 白酒 同时偏强 -> 成分暴露重叠
    hs300 = signals.get("沪深300")
    baijiu = signals.get("中证白酒")
    if hs300 and baijiu:
        strong = lambda s: (s.get("technical_score") is not None
                            and float(s.get("technical_score", -9)) >= 1
                            and s.get("action") in ("买入", "持有"))
        if strong(hs300) and strong(baijiu):
            warnings.append("沪深300与白酒同时偏强，成分暴露有重叠（白酒主要权重股也属大盘消费）")

    # 多个权益指数同时卖出 -> 权益整体偏弱
    equity_sells = [name for name, sig in signals.items()
                    if sig.get("action") == "卖出" and sig.get("risk_cluster") in EQUITY_CLUSTERS]
    if len(equity_sells) >= 3:
        warnings.append("多个权益指数同时走弱，权益整体偏弱，暂不适合加仓权益类")

    return warnings
