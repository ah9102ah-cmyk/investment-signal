# -*- coding: utf-8 -*-
import unittest

import daily_signal as ds
import email_signal
import fetch_fundamentals as ff
import signal_rules as rules


class SignalTests(unittest.TestCase):
    def test_rsi_wilder_direction_and_flat_case(self):
        import pandas as pd
        rising = pd.Series([100 * (1.01 ** i) for i in range(61)])
        flat = pd.Series([100.0] * 61)
        self.assertAlmostEqual(ds.rsi(rising).iloc[-1], 100.0)
        self.assertAlmostEqual(ds.rsi(flat).iloc[-1], 50.0)

    def test_momentum_direction_is_consistent(self):
        self.assertEqual(ds.score_momentum_values(65, 0.05), 2)
        self.assertEqual(ds.score_momentum_values(35, -0.05), -2)
        self.assertEqual(ds.score_momentum_values(85, 0.00), 1)

    def test_signal_labels_are_neutral(self):
        self.assertEqual(ds.make_signal(1, 1, 1), "偏强")
        self.assertEqual(ds.make_signal(-2, -2, 0), "偏弱")
        self.assertEqual(ds.make_signal(0, 0, 0), "中性")

    def test_value_scores_use_each_assets_own_history(self):
        history = list(range(1, 101))
        self.assertEqual(rules.valuation_percentile_score(history, 10), 2)
        self.assertEqual(
            rules.valuation_percentile_score(history, 90, higher_is_cheaper=True), 2
        )

    def test_composite_value_score_is_always_normalized(self):
        score, quality = rules.combine_scores({"pe": 2, "pb": 2, "erp": 2}, min_components=2)
        self.assertEqual(score, 2)
        self.assertEqual(quality, "完整")
        score, quality = rules.combine_scores({"pe": -2, "pb": None, "erp": 2}, min_components=2)
        self.assertEqual(score, 0)
        self.assertEqual(quality, "降级(2/3)")

    def test_gold_value_and_positive_momentum_rules(self):
        self.assertEqual(rules.gold_bias_score(-16), 2)
        self.assertEqual(rules.gold_bias_score(-10), 1)
        self.assertEqual(rules.gold_bias_score(10), -1)
        self.assertEqual(rules.gold_bias_score(16), -2)
        self.assertEqual(
            rules.select_positive_momentum({"A": 0.10, "B": -0.01, "C": 0.03}),
            ["A", "C"],
        )
        self.assertEqual(rules.select_positive_momentum({"A": 0.10, "B": -0.01}), ["A"])

    def test_multi_horizon_trend_requires_majority_and_positive_average(self):
        rising = [100 + i * 0.1 for i in range(240)]
        signal = rules.multi_horizon_trend(rising, 130)
        self.assertEqual(signal["positive_votes"], 3)
        self.assertTrue(signal["eligible"])

        mixed = [100.0] * 240
        mixed[-240] = 80.0
        mixed[-200] = 120.0
        mixed[-160] = 125.0
        signal = rules.multi_horizon_trend(mixed, 100.0)
        self.assertEqual(signal["positive_votes"], 1)
        self.assertFalse(signal["eligible"])

    def test_multi_horizon_selection_and_volatility(self):
        signals = {
            "A": {"eligible": True, "average": 0.12},
            "B": {"eligible": False, "average": 0.20},
            "C": {"eligible": True, "average": 0.08},
        }
        self.assertEqual(rules.select_multi_horizon_trend(signals), ["A", "C"])
        steady = [100 * (1.001 ** i) for i in range(60)]
        vol = rules.annualized_volatility(steady, steady[-1] * 1.001)
        self.assertIsNotNone(vol)
        self.assertLess(vol, 1e-10)

    def test_email_parser_accepts_multi_horizon_table(self):
        sample = """中长期趋势共识(160/200/240日；至少2个周期向上)
--------------------------------------------------------------------------
排名  资产      平均趋势      周期共识      60日波动
1    黄金       +18.0%      3/3向上       15.0%
--------------------------------------------------------------------------
红利低波 1.200 +0.20 +1 +2 +1 +4 偏强 买入
"""
        momentum, _, signals, _, _ = email_signal.parse_output(sample)
        self.assertEqual(momentum, [("1", "黄金", "+18.0%", "3/3向上", "15.0%")])
        self.assertEqual(signals[0][-2:], ("偏强", "买入"))
        html = email_signal.format_body(sample)
        self.assertIn("中长期趋势共识", html)
        self.assertIn("60日波动", html)

    def test_final_action_uses_dual_confirmation_and_slow_exit(self):
        self.assertEqual(rules.final_action(0, 2, 1, 0.12, 3)[0], "买入")
        self.assertEqual(
            rules.final_action(-2, 2, 1, 0.12, 3)[0], "持有",
            "趋势向上但估值过贵时不应新增买入结论",
        )
        self.assertEqual(rules.final_action(0, -1, 0, -0.08, 1)[0], "卖出")
        self.assertEqual(
            rules.final_action(0, 1, 0, -0.02, 1)[0], "持有",
            "长趋势刚转弱但短期仍稳时不应立刻卖出",
        )
        self.assertEqual(rules.final_action(None, 1, 1, 0.1, 3)[0], "数据不足")


class ValuationTests(unittest.TestCase):
    def test_growth_uses_median_and_clamp(self):
        rows = [
            {"PARENTNETPROFITTZ": 300},
            {"PARENTNETPROFITTZ": 10},
            {"PARENTNETPROFITTZ": 20},
        ]
        self.assertEqual(ff.smooth_g(rows), 20)

    def test_historical_pe_skips_prices_before_information_date(self):
        ttm = [("2024-04-30", 2.0), ("2024-08-30", 2.5), ("2024-10-30", 3.0)]
        prices = [("2023-01-01", 100.0)] * 200 + [("2024-05-01", 20.0)] * 200
        self.assertEqual(ff.hist_pe_median(ttm, prices), 10.0)

    def test_range_score_does_not_treat_missing_as_zero(self):
        self.assertIsNone(ff.valuation_range_score(10, None, None, None))
        self.assertEqual(ff.valuation_range_score(8, 9, 10, 11), 2)
        self.assertEqual(ff.valuation_range_score(12, 9, 10, 11), -2)

    def test_zero_growth_is_not_scored_as_negative_growth(self):
        score, verdict = ff.quality_score(15, 0, 40, 30, 0)
        self.assertEqual(score, 3)
        self.assertEqual(verdict, "中")

    def test_annual_report_eps_is_not_double_counted(self):
        annual = {
            "REPORT_DATE": "2025-12-31",
            "REPORT_DATE_NAME": "2025年报",
            "SECURITY_NAME_ABBR": "样例",
            "EPSJB": 9.61,
            "ROEJQ": 20,
        }
        out = ff.compute(annual, annual, {"EPSJB": 2.36}, 160, fair_pe_hist=20, g_override=10)
        self.assertEqual(out["eps_ttm"], 9.61)

    def test_high_historical_pe_keeps_ordered_range(self):
        latest = {
            "REPORT_DATE": "2026-03-31",
            "REPORT_DATE_NAME": "2026一季报",
            "SECURITY_NAME_ABBR": "样例",
            "EPSJB": 3,
            "ROEJQ": 20,
        }
        annual = {"EPSJB": 10, "ROEJQ": 20}
        out = ff.compute(latest, annual, {"EPSJB": 2}, 600, fair_pe_hist=60, g_override=10)
        self.assertEqual(out["fair_pe"], 60)
        self.assertLess(out["target_low"], out["target"])
        self.assertLess(out["target"], out["target_high"])


if __name__ == "__main__":
    unittest.main()
