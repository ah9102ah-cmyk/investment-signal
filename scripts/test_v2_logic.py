# -*- coding: utf-8 -*-
"""category_v2_shadow 单元测试(阶段 B + 2026-08 更正版)。"""
import math
import statistics
import unittest

import category_v2 as v2
import v2_config as cfg


def rising(n=300, base=100.0, step=0.001):
    return [base * (1 + step) ** i for i in range(n)]


def falling(n=300, base=130.0, step=0.001):
    return [base * (1 - step) ** i for i in range(n)]


def flat(n=300, base=100.0):
    return [base] * n


def pe_seq(n=300, base=10.0, span=50):
    return [base + (i % span) for i in range(n)]


class CategorizeTests(unittest.TestCase):
    def test_eight_indexes_categorized(self):
        expected = {
            "沪深300": "宽基核心", "中证500": "宽基核心",
            "红利低波": "红利因子", "科创50": "高成长科技",
            "恒生科技": "高成长科技", "中证白酒": "行业主题",
            "中证医疗": "行业主题", "黄金": "实物资产",
        }
        for name, cat in expected.items():
            self.assertEqual(cfg.CATEGORY_MAP[name][0], cat)

    def test_each_category_has_three_candidates(self):
        for name in cfg.CANDIDATES:
            self.assertEqual(sorted(cfg.CANDIDATES[name].keys()), ["balanced", "trend", "value"])
            for weights in cfg.CANDIDATES[name].values():
                self.assertAlmostEqual(sum(weights.values()), 1.0, places=6)

    def test_hstech_limited(self):
        self.assertEqual(cfg.DATA_AVAILABILITY["恒生科技"], "limited")


class ScoreTests(unittest.TestCase):
    def test_std_momentum_zero_vol(self):
        # 波动率为 0 -> 标准化变化为 0(不崩溃)
        close = flat(300)
        self.assertEqual(v2.std_momentum(close, 100.0), 0.0)
        self.assertEqual(v2.momentum_score_v2(close, 100.0)[0], 0)

    def test_momentum_strong_up(self):
        close = rising(300, step=0.01)     # 单日 1% 上涨
        m, _ = v2.momentum_score_v2(close, close[-1] * 1.01)
        self.assertGreaterEqual(m, 1)

    def test_momentum_strong_down(self):
        close = falling(300, step=0.01)
        m, _ = v2.momentum_score_v2(close, close[-1] * 0.99)
        self.assertLessEqual(m, -1)

    def test_trend_consensus_rising(self):
        score, degraded = v2.trend_score_v2(rising(300), rising(300)[-1] * 1.02)
        self.assertEqual(score, 2)
        self.assertEqual(degraded, [])

    def test_trend_disagreement(self):
        # 三周期分歧: 160日正、200日负、240日负 -> 多数向下
        base = [100.0] * 240
        base[-240] = 80.0
        base[-200] = 120.0
        base[-160] = 125.0
        score, _ = v2.trend_score_v2(base, 100.0)
        self.assertLessEqual(score, -1)

    def test_valuation_missing_not_zero(self):
        # PE 序列缺失 -> 估值分 None(不按 0 处理)
        score, degraded = v2.valuation_score_v2("中证500", "宽基核心", None, 15)
        self.assertIsNone(score)
        sig = v2.compute_signal("中证500", close=rising(300), spot=130.0,
                                pe_history=None, pe_now=15)
        self.assertEqual(sig["action"], "数据不足")
        self.assertIsNone(sig["structural_score"])

    def test_valuation_percentile_direction(self):
        # PE 历史 1..100, 当前 10 -> 便宜(高分为便宜方向: PE 低 -> +2)
        hist = list(range(1, 101))
        score, _ = v2.valuation_score_v2("中证500", "宽基核心", hist, 10)
        self.assertEqual(score, 2)
        score2, _ = v2.valuation_score_v2("中证500", "宽基核心", hist, 90)
        self.assertEqual(score2, -2)

    def test_gold_macro_missing(self):
        score, degraded = v2.gold_macro_score(None)
        self.assertIsNone(score)
        self.assertIn("宏观数据不足", degraded)

    def test_gold_macro_direction(self):
        # 利率 20 日下行 >0.05pp -> 对黄金有利(+1)
        us = [3.5] * 39 + [3.5 - 0.006 * i for i in range(21)]
        score, _ = v2.gold_macro_score(us)
        self.assertEqual(score, 1)

    def test_gold_price_position(self):
        close = rising(300)
        score, _ = v2.gold_price_position(close, close[-1])
        self.assertIsNotNone(score)


class ProtocolTests(unittest.TestCase):
    def test_missing_data_no_silent_zero(self):
        sig = v2.compute_signal("中证500", close=None, spot=None,
                                pe_history=None, pe_now=None)
        self.assertEqual(sig["action"], "数据不足")
        self.assertIsNone(sig["structural_score"])
        self.assertNotEqual(sig["data_quality"], "完整")

    def test_hstech_never_fabricates_v(self):
        sig = v2.compute_signal("恒生科技", close=rising(300), spot=130.0)
        self.assertEqual(sig["action"], "数据不足")
        self.assertIsNone(sig["valuation_score"])
        self.assertEqual(sig["data_quality"], "估值缺失")

    def test_gold_macro_missing_no_strong_buy(self):
        sig = v2.compute_signal("黄金", close=rising(300), spot=rising(300)[-1] * 1.02,
                                us10y_history=None)
        self.assertNotEqual(sig["action"], "买入")
        self.assertIn("宏观", sig["action_reason"] or "")

    def test_buy_requires_dual_confirmation(self):
        sig = v2.compute_signal("红利低波", close=rising(300), spot=rising(300)[-1] * 1.01,
                                pe_history=pe_seq(), pe_now=10,
                                rsi_value=70, candidate="balanced")
        self.assertEqual(sig["action"], "买入")
        self.assertEqual(sig["structural_score"], 2.0)
        self.assertEqual(sig["technical_score"], 2.0)

    def test_industry_no_buy_cheap_downtrend(self):
        # 便宜 + 下跌: 行业主题禁止买入
        close = falling(300)
        sig = v2.compute_signal("中证白酒", close=close, spot=close[-1] * 0.99,
                                pe_history=pe_seq(), pe_now=8,
                                rsi_value=30, candidate="balanced")
        self.assertNotEqual(sig["action"], "买入")

    def test_sell_when_trend_down_momentum_down(self):
        close = falling(300)
        sig = v2.compute_signal("中证500", close=close, spot=close[-1] * 0.995,
                                pe_history=pe_seq(), pe_now=50,
                                rsi_value=25, candidate="balanced")
        self.assertEqual(sig["action"], "卖出")

    def test_gold_position_not_double_counted(self):
        # 黄金: 250日线位置只进结构轮(valuation_score 路径), 价格轮只有 160/200/240 共识+动能
        close = rising(300)
        sig = v2.compute_signal("黄金", close=close, spot=close[-1] * 1.02,
                                us10y_history=[3.5] * 60, rsi_value=70,
                                candidate="balanced")
        self.assertIsNotNone(sig["macro_score"])
        self.assertEqual(sig["technical_score"], 2.0)   # 趋势+动能, 不含 250 线


class PortfolioTests(unittest.TestCase):
    """组合层: 类内排名 + 风险集中提示(阶段 C)"""

    def _sig(self, action, cluster, technical=0, quality="完整"):
        return {"action": action, "risk_cluster": cluster, "technical_score": technical,
                "data_quality": quality}

    def test_same_cluster_multi_buy_gives_rank(self):
        import portfolio_v2 as pf
        signals = {
            "科创50": self._sig("买入", "高成长科技", 2),
            "恒生科技": self._sig("买入", "高成长科技", 1),
        }
        warnings = pf.apply_portfolio(signals, vol_map={"科创50": 0.3, "恒生科技": 0.4})
        self.assertEqual(signals["科创50"]["class_rank"], 1)
        self.assertEqual(signals["恒生科技"]["class_rank"], 2)
        self.assertTrue(any("科技" in w for w in warnings), warnings)
        self.assertTrue(any("类内" in w for w in warnings), warnings)

    def test_three_equity_sells_warns(self):
        import portfolio_v2 as pf
        signals = {
            "红利低波": self._sig("卖出", "防守/红利"),
            "沪深300": self._sig("卖出", "大盘权益"),
            "中证医疗": self._sig("卖出", "医疗行业"),
            "黄金": self._sig("持有", "实物资产"),
        }
        warnings = pf.apply_portfolio(signals)
        self.assertTrue(any("权益整体偏弱" in w for w in warnings), warnings)

    def test_hs300_and_baijiu_overlap(self):
        import portfolio_v2 as pf
        signals = {
            "沪深300": self._sig("持有", "大盘权益", 1),
            "中证白酒": self._sig("持有", "消费行业", 1),
        }
        warnings = pf.apply_portfolio(signals)
        self.assertTrue(any("重叠" in w for w in warnings), warnings)

    def test_no_warning_when_quiet(self):
        import portfolio_v2 as pf
        signals = {
            "黄金": self._sig("持有", "实物资产"),
            "沪深300": self._sig("持有", "大盘权益"),
        }
        warnings = pf.apply_portfolio(signals)
        self.assertEqual(warnings, [])

    def test_rank_prefers_lower_volatility(self):
        import portfolio_v2 as pf
        signals = {
            "科创50": self._sig("买入", "高成长科技", 1),
            "恒生科技": self._sig("买入", "高成长科技", 1),
        }
        pf.apply_portfolio(signals, vol_map={"科创50": 0.2, "恒生科技": 0.5})
        self.assertEqual(signals["科创50"]["class_rank"], 1)
        self.assertEqual(signals["恒生科技"]["class_rank"], 2)


class MomentumPrecisionTests(unittest.TestCase):
    """P1-2 更正: 精确5日收益 / 精确20个日收益 / σ边界"""

    def test_five_day_return_exact_span(self):
        # 5日收益 = close[t]/close[t-5]-1: t-5=100, t=110 -> 恰好 +10%
        close = [100.0] * 300
        close[-6] = 100.0          # t-5
        close[-5] = 120.0          # t-4(若错用 close[-5] 会得到 -8.3%)
        close[-1] = 110.0          # t
        self.assertAlmostEqual(v2.five_day_return(close, 110.0), 0.10, places=9)
        # 对照错误实现(旧代码用 close[-5]): 110/120-1 = -0.0833
        self.assertAlmostEqual(110.0 / close[-5] - 1, -0.08333, places=4)

    def test_five_day_return_too_short(self):
        self.assertIsNone(v2.five_day_return([100.0] * 4, 100.0))

    def test_volatility_window_exact_20_returns(self):
        close = [100.0 + i for i in range(40)] + [200.0]
        rets = v2.volatility_returns(close, 20)
        self.assertEqual(len(rets), 20)
        # 与手工逐日收益一致
        manual = [close[i] / close[i - 1] - 1 for i in range(len(close) - 20, len(close))]
        for a, b in zip(rets, manual):
            self.assertAlmostEqual(a, b, places=12)

    def _close_from_returns(self, rets, base=100.0):
        c = [base]
        for r in rets:
            c.append(c[-1] * (1 + r))
        return c

    def _series_with_z(self, target_z, sd=0.01, days=5):
        # 20个日收益交替 ±sd(样本sd≈sd) + 最后一个收益 x 由二分法确定,
        # 使"手工计算的" z = 5日收益/(20日样本sd×√5) 精确命中 target_z。
        # 手工公式与引擎同口径(close[-1]/close[-6]-1, 最近20个日收益),
        # 若引擎用错下标/窗口(旧bug), 该测试会失败。
        base_rets = [sd if i % 2 == 0 else -sd for i in range(20)]

        def z_of(x):
            close = self._close_from_returns(base_rets + [x])   # 22个点
            r5 = close[-1] / close[-6] - 1
            s = statistics.stdev([close[i] / close[i - 1] - 1
                                  for i in range(len(close) - 20, len(close))])
            return r5 / (s * math.sqrt(days)) if s > 0 else 0.0

        lo, hi = -0.2, 0.2
        for _ in range(60):
            mid = (lo + hi) / 2
            if z_of(mid) < target_z:
                lo = mid
            else:
                hi = mid
        x = (lo + hi) / 2
        return self._close_from_returns(base_rets + [x])

    def test_std_momentum_formula(self):
        close = self._series_with_z(0.8)
        z = v2.std_momentum(close, close[-1])
        rets = v2.volatility_returns(close, 20)
        r5 = v2.five_day_return(close, close[-1])
        sd = statistics.stdev(rets)
        self.assertAlmostEqual(z, r5 / (sd * math.sqrt(5)), places=9)

    def test_sigma_boundaries(self):
        # ±0.5/±1.0/±1.5σ 三档阈值(任务书 §8, 只研究这三档):
        # z>=1.5->2, z>=0.5->1, z<=-1.5->-2, z<=-0.5->-1, 其余0
        cases = [(0.49, 0), (0.51, 1), (1.49, 1), (1.51, 2),
                 (-0.49, 0), (-0.51, -1), (-1.49, -1), (-1.51, -2)]
        for target_z, expected in cases:
            close = self._series_with_z(target_z)
            z = v2.std_momentum(close, close[-1])
            self.assertAlmostEqual(z, target_z, delta=0.02,
                                   msg=f"z 未达目标 {target_z}")
            m, _ = v2.momentum_score_v2(close, close[-1])
            self.assertEqual(m, expected, msg=f"z={z:.3f} 应映射到 {expected}")


class CompositeWeightTests(unittest.TestCase):
    """P2-1 更正: 综合分必须基于原始分项×原始配置权重, 宽度权重不丢失"""

    def _hs500_signal(self, breadth_ratio=0.8, pe_now=95):
        close = rising(300, step=0.001)
        return v2.compute_signal(
            "中证500", close=close, spot=close[-1] * 1.02,
            pe_history=[10.0] * 100, pe_now=pe_now,   # pe_now 高于全部历史 -> 贵 -> valuation=-2
            breadth_ratio=breadth_ratio, rsi_value=50, candidate="balanced")

    def test_breadth_weight_not_lost(self):
        # 中证500 balanced: valuation 0.25 / breadth 0.13 / trend 0.50 / momentum 0.12
        # 输入: valuation=-2, breadth=+2, trend=+2, momentum=+2
        # 综合分 = (-2*0.25 + 2*0.13 + 2*0.50 + 2*0.12)/1.0 = 1.00 (宽度权重不丢失)
        sig = self._hs500_signal()
        self.assertEqual(sig["valuation_score"], -2)
        self.assertEqual(sig["breadth_score"], 2)
        self.assertEqual(sig["trend_score"], 2)
        self.assertAlmostEqual(sig["composite_score"], 1.0, places=2)
        self.assertAlmostEqual(sig["available_weight_ratio"], 1.0, places=3)
        # 对照: 没有宽度时按剩余权重归一化 -> 0.85(权重归一化正确)
        sig2 = self._hs500_signal(breadth_ratio=None)
        self.assertAlmostEqual(sig2["available_weight_ratio"], 0.87, places=2)
        self.assertAlmostEqual(sig2["composite_score"], 0.85, places=2)

    def test_valuation_score_is_pure_not_structural(self):
        # 估值分只能代表估值; 结构轮(解释字段)允许混合
        sig = self._hs500_signal()
        self.assertEqual(sig["valuation_score"], -2)
        # 结构轮 = (-2*0.25 + 2*0.13)/0.38 = -0.63
        self.assertAlmostEqual(sig["structural_score"], -0.63, places=2)
        self.assertNotEqual(sig["valuation_score"], sig["structural_score"])

    def test_trend_score_is_pure_not_technical(self):
        close = rising(300, step=0.001)
        sig = v2.compute_signal("中证500", close=close, spot=close[-1] * 1.02,
                                pe_history=[10.0] * 100, pe_now=95,
                                breadth_ratio=0.8, rsi_value=50, candidate="balanced")
        self.assertEqual(sig["trend_score"], 2)
        # technical = (2*0.50 + 2*0.12)/0.62 = 2.0 (这里恰好相同, 检查字段语义)
        self.assertEqual(sig["momentum_score"], 2)

    def test_insufficient_available_weight_data_insufficient(self):
        # 黄金 balanced: macro 0.42/trend 0.42/momentum 0.16; 只有宏观 -> 可用权重 0.42 < 0.5
        sig = v2.compute_signal("黄金", close=None, spot=None,
                                us10y_history=[3.5] * 60, signal_date="2026-08-05")
        self.assertEqual(sig["action"], "数据不足")
        self.assertIsNone(sig["composite_score"])
        self.assertAlmostEqual(sig["available_weight_ratio"], 0.42, places=2)

    def test_composite_uses_raw_weights_for_hs300(self):
        # 沪深300 balanced: valuation 0.40/trend 0.40/momentum 0.10/breadth 0.10
        # 输入: valuation=-2(PE/PB/ERP 三项全贵), breadth=+2, trend=+2, momentum=+2
        close = rising(300, step=0.001)
        sig = v2.compute_signal(
            "沪深300", close=close, spot=close[-1] * 1.02,
            pe_history=[10.0] * 100, pe_now=95,
            pb_history=[1.5] * 100, pb_now=3.0,
            erp_history=[5.0] * 100, erp_now=0.5,
            breadth_ratio=0.8, rsi_value=50, candidate="balanced")
        # (-2*0.40 + 2*0.10 + 2*0.40 + 2*0.10)/1.0 = 0.40 (宽度权重不丢失)
        self.assertEqual(sig["valuation_score"], -2)
        self.assertAlmostEqual(sig["composite_score"], 0.40, places=2)


class CategoryRuleTests(unittest.TestCase):
    """P1-4: 类别特有规则必须真实执行(不再只是配置文件里的标志)"""

    def test_kcb_high_pe_no_direct_sell(self):
        # 科创50: 高PE(估值-2) + 价格轮中性偏弱(趋势-1, 动能0 -> technical≈-0.8)
        # 旧逻辑 structural<=-1 且 technical<=0 会卖出; 更正后估值弱不直接触发卖出,
        # 综合弱项退出也要求价格面同步走弱(long_negative), 此处应为持有
        close = [100.0] * 240
        close[-240] = 90.0
        close[-200] = 100.0
        close[-160] = 100.0
        spot = 100.0
        sig = v2.compute_signal("科创50", close=close, spot=spot,
                                pe_history=list(range(1, 101)), pe_now=98,
                                rsi_value=50, candidate="balanced")
        self.assertNotEqual(sig["action"], "卖出")
        self.assertEqual(sig["valuation_score"], -2)
        self.assertLess(sig["technical_score"], 0)

    def test_kcb_sell_still_driven_by_trend_momentum(self):
        # 科创50: 趋势向下 + 动能转弱 -> 正常卖出(卖出由价格面触发, 与估值无关)
        close = falling(300)
        sig = v2.compute_signal("科创50", close=close, spot=close[-1] * 0.995,
                                pe_history=list(range(1, 101)), pe_now=30,
                                rsi_value=25, candidate="balanced")
        self.assertEqual(sig["action"], "卖出")

    def test_kcb_high_pe_caps_buy_intensity(self):
        # 科创50 trend 候选: 高PE(-1) + 强趋势(technical≈1.6) -> 高PE只限制买入强度
        close = [100.0] * 240
        close[-240] = 80.0
        close[-200] = 90.0
        close[-160] = 92.0
        spot = 100.0
        sig = v2.compute_signal("科创50", close=close, spot=spot,
                                pe_history=list(range(1, 101)), pe_now=90,
                                rsi_value=50, candidate="trend")
        self.assertNotEqual(sig["action"], "买入")

    def test_redli_slow_trend_guard(self):
        # 红利因子: 便宜 + 趋势弱 -> 不买(趋势保护, 防止"便宜就抄底")
        weights = cfg.CANDIDATES["红利低波"]["value"]
        action, _, _ = v2.final_action_v2(
            "红利低波", "红利因子", structural=1.5, technical=0.0, momentum=1,
            composite=1.0, degraded=[], weights=weights, candidate="value",
            valuation_score=2, trend_score=-1)
        self.assertNotEqual(action, "买入")
        action2, _, _ = v2.final_action_v2(
            "红利低波", "红利因子", structural=1.5, technical=0.0, momentum=1,
            composite=1.0, degraded=[], weights=weights, candidate="value",
            valuation_score=2, trend_score=1)
        self.assertEqual(action2, "买入")

    def test_hs300_vs_cs500_different_enter_gate(self):
        # 同一输入在不同指数上有不同进入门槛(沪深300 重结构 / 中证500 重价格)
        w300 = cfg.CANDIDATES["沪深300"]["balanced"]
        w500 = cfg.CANDIDATES["中证500"]["balanced"]
        a300, _, _ = v2.final_action_v2(
            "沪深300", "宽基核心", structural=0.6, technical=1.0, momentum=0,
            composite=0.8, degraded=[], weights=w300, candidate="balanced",
            valuation_score=0, trend_score=1)
        a500, _, _ = v2.final_action_v2(
            "中证500", "宽基核心", structural=0.6, technical=1.0, momentum=0,
            composite=0.8, degraded=[], weights=w500, candidate="balanced",
            valuation_score=0, trend_score=1)
        self.assertEqual(a300, "买入")
        self.assertEqual(a500, "持有")

    def test_industry_no_buy_cheap_downtrend_existing(self):
        # 行业主题: "估值便宜+继续下跌"不触发买入(既有规则继续有效)
        close = falling(300)
        sig = v2.compute_signal("中证白酒", close=close, spot=close[-1] * 0.99,
                                pe_history=pe_seq(), pe_now=8,
                                rsi_value=30, candidate="balanced")
        self.assertNotEqual(sig["action"], "买入")
        self.assertIn("盈利周期缺失", sig["degraded_fields"])


class StalenessTests(unittest.TestCase):
    """P2-2: 陈旧度控制——数据日期、陈旧天数、超过阈值不得标记完整/输出强信号"""

    def test_stale_price_not_complete(self):
        close = rising(300)
        sig = v2.compute_signal(
            "红利低波", close=close, spot=close[-1] * 1.01,
            pe_history=pe_seq(), pe_now=10,
            signal_date="2026-08-10",
            data_dates={"price": "2026-08-01", "valuation": "2026-08-09"})
        self.assertEqual(sig["data_sources"]["price"]["staleness_days"], 9)
        self.assertEqual(sig["data_sources"]["price"]["status"], "stale")
        self.assertEqual(sig["data_quality"], "降级")
        self.assertTrue(any("价格数据陈旧" in d or "price数据陈旧" in d for d in sig["degraded_fields"]))

    def test_severe_macro_no_buy_gold(self):
        close = rising(300)
        sig = v2.compute_signal(
            "黄金", close=close, spot=close[-1] * 1.02,
            us10y_history=[3.5] * 60,
            signal_date="2026-08-30",
            data_dates={"us10y": "2026-08-01", "price": "2026-08-29"})
        # 宏观陈旧29天 >= severe(15) -> 即使双轮确认也不输出买入
        self.assertEqual(sig["data_sources"]["us10y"]["status"], "severe")
        self.assertNotEqual(sig["action"], "买入")

    def test_severe_breadth_no_buy(self):
        close = rising(300)
        sig = v2.compute_signal(
            "中证500", close=close, spot=close[-1] * 1.02,
            pe_history=pe_seq(), pe_now=10, breadth_ratio=0.8,
            signal_date="2026-08-30",
            data_dates={"breadth": "2026-07-20", "price": "2026-08-29"})
        self.assertEqual(sig["data_sources"]["breadth"]["status"], "severe")
        self.assertNotEqual(sig["action"], "买入")

    def test_data_sources_protocol_fields(self):
        close = rising(300)
        sig = v2.compute_signal(
            "中证500", close=close, spot=close[-1] * 1.02,
            pe_history=pe_seq(), pe_now=10, breadth_ratio=0.8,
            signal_date="2026-08-10",
            data_dates={"price": "2026-08-07", "valuation": "2026-08-08", "breadth": "2026-08-08"},
            data_meta={"valuation": {"source": "中证官网", "fallback_source": None, "fetched_at": "2026-08-10T09:00:00"}})
        val_src = sig["data_sources"]["valuation"]
        for field in ("source", "data_date", "fetched_at", "staleness_days", "status", "fallback_source"):
            self.assertIn(field, val_src, f"data_sources 缺字段 {field}")
        self.assertEqual(sig["data_sources"]["valuation"]["source"], "中证官网")
        self.assertEqual(sig["price_date"], "2026-08-07")


class GoldenFixtureTests(unittest.TestCase):
    """P1-2 跨端一致性 golden fixtures(Python 是 V2 唯一计算源)。

    页面只读取 v2_signals.json; 这里的冻结期望值保证引擎改动时跨端合同不漂移。
    若引擎被改动导致分项/综合分变化, 必须先更新 fixtures 并重新验证页面。"""

    REQUIRED_FIELDS = [
        "strategy_version", "strategy_type", "asset_name",
        "valuation_score", "macro_score", "breadth_score", "earnings_score",
        "trend_score", "momentum_score",
        "structural_score", "technical_score", "composite_score",
        "available_weight_ratio", "action", "action_reason", "data_quality",
        "degraded_fields", "data_sources", "candidate",
    ]

    def test_protocol_completeness_for_all_8_indexes(self):
        close = rising(300, step=0.001)
        for name in cfg.CATEGORY_MAP:
            kwargs = dict(close=close, spot=close[-1] * 1.01,
                          pe_history=pe_seq(), pe_now=15,
                          rsi_value=55, candidate="balanced",
                          signal_date="2026-08-10",
                          data_dates={"price": "2026-08-07", "valuation": "2026-08-08"})
            if name == "黄金":
                kwargs.update(us10y_history=[3.5] * 60)
            if name == "沪深300":
                kwargs.update(pb_history=[1.5] * 100, pb_now=1.8,
                              erp_history=[4.0] * 100, erp_now=4.2)
            sig = v2.compute_signal(name, **kwargs)
            for f in self.REQUIRED_FIELDS:
                self.assertIn(f, sig, f"{name} 缺协议字段 {f}")
            for f in ("valuation_score", "macro_score", "breadth_score", "trend_score",
                      "momentum_score", "structural_score", "technical_score"):
                if sig[f] is not None:
                    self.assertGreaterEqual(sig[f], -2.0)
                    self.assertLessEqual(sig[f], 2.0)
            self.assertGreaterEqual(sig["available_weight_ratio"], 0.0)
            self.assertLessEqual(sig["available_weight_ratio"], 1.0)
            for field, src in sig["data_sources"].items():
                for k in ("source", "data_date", "fetched_at", "staleness_days", "status", "fallback_source"):
                    self.assertIn(k, src, f"{name}.data_sources.{field} 缺 {k}")

    def test_frozen_fixture_redli_buy(self):
        # 冻结: 红利低波 上涨+便宜+RSI70 balanced -> 买入, 各分项固定
        close = rising(300)
        sig = v2.compute_signal("红利低波", close=close, spot=close[-1] * 1.01,
                                pe_history=pe_seq(), pe_now=10,
                                rsi_value=70, candidate="balanced")
        self.assertEqual(sig["action"], "买入")
        self.assertEqual(sig["valuation_score"], 2.0)
        self.assertEqual(sig["trend_score"], 2.0)
        self.assertEqual(sig["structural_score"], 2.0)
        self.assertEqual(sig["technical_score"], 2.0)
        self.assertEqual(sig["composite_score"], 2.0)

    def test_frozen_fixture_hs300_cheap_weak(self):
        # 冻结: 沪深300 便宜 + 价格中性偏弱(趋势-1/动能0 -> technical≈-0.8)
        # -> 持有(双轮未确认, 不因便宜买入, 也不因弱价格直接卖)
        close = [100.0] * 240
        close[-240] = 95.0
        close[-200] = 100.0
        close[-160] = 100.0
        sig = v2.compute_signal("沪深300", close=close, spot=100.0,
                                pe_history=pe_seq(), pe_now=8,
                                pb_history=[1.0] * 100, pb_now=1.1,
                                erp_history=[5.0] * 100, erp_now=6.0,
                                rsi_value=30, candidate="balanced")
        # PE便宜(2) + PB贵(-2) + 利差便宜(2) 三项等权 -> round((2-2+2)/3)=1
        self.assertEqual(sig["valuation_score"], 1)   # 偏便宜
        self.assertLess(sig["technical_score"], 0)    # 价格偏弱
        self.assertNotEqual(sig["action"], "买入")     # 便宜但价格未确认 -> 不买
        self.assertEqual(sig["action"], "持有")

    def test_frozen_fixture_kcb_high_pe(self):
        # 冻结: 科创50 高PE + 中性价格 -> 持有(高PE不直接卖)
        close = [100.0] * 240
        close[-240] = 90.0
        close[-200] = 100.0
        close[-160] = 100.0
        sig = v2.compute_signal("科创50", close=close, spot=100.0,
                                pe_history=list(range(1, 101)), pe_now=98,
                                rsi_value=50, candidate="balanced")
        self.assertEqual(sig["valuation_score"], -2.0)
        self.assertEqual(sig["action"], "持有")

    def test_frozen_fixture_gold_macro_missing(self):
        sig = v2.compute_signal("黄金", close=rising(300), spot=rising(300)[-1] * 1.02,
                                us10y_history=None, candidate="balanced")
        self.assertEqual(sig["action"], "数据不足")
        self.assertIsNone(sig["macro_score"])
        self.assertEqual(sig["data_quality"], "宏观缺失")


if __name__ == "__main__":
    unittest.main()
