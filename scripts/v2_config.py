# -*- coding: utf-8 -*-
"""category_v2 策略配置层（阶段 B 影子版本）。

common_v1 保持不变、可独立运行；本文件只定义 V2 候选策略的参数。
所有权重/阈值都是任务书 §5 范围内的候选研究值，不是最终参数。
每类三套候选：value(估值优先) / balanced(均衡) / trend(趋势优先)。
"""
from __future__ import annotations

# 指数 -> (策略类别, 风险簇)
CATEGORY_MAP = {
    "沪深300": ("宽基核心", "大盘权益"),
    "中证500": ("宽基核心", "中小盘权益"),
    "红利低波": ("红利因子", "防守/红利"),
    "科创50": ("高成长科技", "高成长科技"),
    "恒生科技": ("高成长科技", "高成长科技"),
    "中证白酒": ("行业主题", "消费行业"),
    "中证医疗": ("行业主题", "医疗行业"),
    "黄金": ("实物资产", "实物资产"),
}

# 恒生科技在阶段 B 无可靠估值/行情历史 -> 数据不足通道
DATA_AVAILABILITY = {
    "沪深300": "full",
    "中证500": "full",
    "红利低波": "full",
    "科创50": "full",
    "中证白酒": "full",
    "中证医疗": "full",
    "恒生科技": "limited",     # 只有 320 天行情、无估值、无成分股
    "黄金": "full",
}

# 每类三套候选权重(合计 1.0)。分项键名:
#   valuation 估值结构 / macro 宏观结构(黄金) / trend 中长期趋势 / momentum 短期动能 / breadth 市场宽度
# 说明: 宽度(成分股截面)阶段 B 只做当前观察, 历史回测不含宽度 -> 回测时按剩余项归一化,
#       线上 shadow 运行时有宽度数据则按权重参与。
CANDIDATES = {
    "沪深300": {
        "value":    {"valuation": 0.45, "trend": 0.35, "momentum": 0.10, "breadth": 0.10},
        "balanced": {"valuation": 0.40, "trend": 0.40, "momentum": 0.10, "breadth": 0.10},
        "trend":    {"valuation": 0.35, "trend": 0.45, "momentum": 0.10, "breadth": 0.10},
    },
    "中证500": {
        "value":    {"valuation": 0.30, "trend": 0.45, "momentum": 0.12, "breadth": 0.13},
        "balanced": {"valuation": 0.25, "trend": 0.50, "momentum": 0.12, "breadth": 0.13},
        "trend":    {"valuation": 0.20, "trend": 0.55, "momentum": 0.12, "breadth": 0.13},
    },
    "红利低波": {
        "value":    {"valuation": 0.60, "trend": 0.30, "momentum": 0.10},
        "balanced": {"valuation": 0.50, "trend": 0.40, "momentum": 0.10},
        "trend":    {"valuation": 0.45, "trend": 0.40, "momentum": 0.15},
    },
    "科创50": {
        "value":    {"valuation": 0.20, "trend": 0.45, "momentum": 0.10, "breadth": 0.25},
        "balanced": {"valuation": 0.15, "trend": 0.47, "momentum": 0.10, "breadth": 0.28},
        "trend":    {"valuation": 0.10, "trend": 0.52, "momentum": 0.15, "breadth": 0.23},
    },
    "中证白酒": {
        "value":    {"valuation": 0.40, "trend": 0.25, "momentum": 0.10, "breadth": 0.25},
        "balanced": {"valuation": 0.32, "trend": 0.25, "momentum": 0.11, "breadth": 0.32},
        "trend":    {"valuation": 0.25, "trend": 0.35, "momentum": 0.10, "breadth": 0.30},
    },
    "中证医疗": {
        "value":    {"valuation": 0.40, "trend": 0.25, "momentum": 0.10, "breadth": 0.25},
        "balanced": {"valuation": 0.32, "trend": 0.25, "momentum": 0.11, "breadth": 0.32},
        "trend":    {"valuation": 0.25, "trend": 0.35, "momentum": 0.10, "breadth": 0.30},
    },
    "黄金": {
        "value":    {"macro": 0.50, "trend": 0.35, "momentum": 0.15},
        "balanced": {"macro": 0.42, "trend": 0.42, "momentum": 0.16},
        "trend":    {"macro": 0.35, "trend": 0.50, "momentum": 0.15},
    },
}

# 恒生科技: 阶段 B 无候选权重(数据不足通道); 候选配置留待替代结构轮接入后再定
CANDIDATES["恒生科技"] = {
    "value":    {"valuation": 0.20, "trend": 0.45, "momentum": 0.10, "breadth": 0.25},
    "balanced": {"valuation": 0.15, "trend": 0.47, "momentum": 0.10, "breadth": 0.28},
    "trend":    {"valuation": 0.10, "trend": 0.52, "momentum": 0.15, "breadth": 0.23},
}

# 波动率标准化候选阈值(任务书 §8, 只研究这三档, 不连续搜索)
VOL_LEVELS = (0.5, 1.0, 1.5)      # ±0.5σ / ±1.0σ / ±1.5σ
VOL_WINDOW = 20                     # 日波动率窗口(20 或 60, 研究 20 优先)
VOL_DAYS = 5                        # 标准化变化跨度

# 三态判定门槛(V2 shadow 统一退出门槛; 进入门槛按候选体现结构轮/价格轮强弱配比)
#   value(估值优先): 结构轮要求更严(structural>=1), 价格轮放宽(technical>=0)
#   balanced(均衡):  双轮对等(structural>=0, technical>=1)
#   trend(趋势优先): 价格轮要求更严(technical>=1 且 momentum>=0), 结构轮放宽(structural>=-1)
THRESHOLDS = {
    "value":    {"enter_structural": 1, "enter_technical": 0, "enter_momentum": -1},
    "balanced": {"enter_structural": 0, "enter_technical": 1, "enter_momentum": -1},
    "trend":    {"enter_structural": -1, "enter_technical": 1, "enter_momentum": 0},
}
EXIT_COMPOSITE = -1.0

# 类别特有禁止条件(任务书 §5)
CATEGORY_RULES = {
    "行业主题": {
        "no_buy_cheap_downtrend": True,   # "估值便宜+继续下跌"不触发买入
    },
    "红利因子": {
        "slow_trend_guard": True,         # 趋势只防"便宜就抄底"
    },
    "实物资产": {
        "macro_missing_no_strong_buy": True,  # 宏观缺失/陈旧不得输出强买入
    },
    "高成长科技": {
        "high_pe_no_sell": True,          # PE 高不直接判卖(估值只作护栏)
    },
}
