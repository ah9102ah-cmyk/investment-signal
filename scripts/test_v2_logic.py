# -*- coding: utf-8 -*-
"""category_v2_shadow 单元测试(阶段 B)。"""
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


if __name__ == "__main__":
    unittest.main()
