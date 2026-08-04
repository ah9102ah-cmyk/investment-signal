# -*- coding: utf-8 -*-
import unittest

import daily_signal as ds
import fetch_fundamentals as ff


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
