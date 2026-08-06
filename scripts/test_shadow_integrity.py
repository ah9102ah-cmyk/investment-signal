# -*- coding: utf-8 -*-
"""v4 数据完整性针对性测试(2026-08-06)。

覆盖用户验收清单:
1. 信号日后的数据绝不能进入计算(快照/美元/字段 as_of=signal_date 严格截止)
2. 未来数据必须返回 invalid(future 状态, 不得伪装新鲜)
3. 到期历史记录能够自动回填收益(遍历全部记录, 只追加不覆盖历史信号)
4. 研究候选能够落盘并在下次运行后保留(完整 dict, 同数据同规则)
5. 美元历史包含多日期时能正常解析(保留日期并截断到信号日)
6. 阶段回测只累计该阶段选中的日收益
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtest_v2 as bt
import category_v2 as v2
import shadow_log as sl
import v2_config as cfg
import v2_daily as vd


def synthetic_df(days=300, start="2025-01-01"):
    """确定性行情(与 test_backtest_timeline 同构): 每日开盘=前收*0.995, 收盘=开盘*1.01。"""
    idx = pd.bdate_range(start, periods=days)
    close = np.zeros(days)
    openp = np.zeros(days)
    close[0] = 100.0
    openp[0] = 100.0
    for i in range(1, days):
        openp[i] = close[i - 1] * 0.995
        close[i] = openp[i] * 1.01
    df = pd.DataFrame({"open": openp, "close": close}, index=idx)
    df["ret"] = df["close"].pct_change().fillna(0)
    df["ret_oc"] = df["close"] / df["open"] - 1
    df["pe"] = 15.0
    df["pb"] = 1.5
    df["erp"] = 4.0
    df["rsi"] = 50.0
    df["chg5"] = df["close"] / df["close"].shift(5) - 1
    df["ma20"] = df["close"].rolling(20).mean()
    df["ma60"] = df["close"].rolling(60).mean()
    df["us10y"] = 3.5
    return df


class AsOfCutoffTests(unittest.TestCase):
    """1. 信号日后的数据绝不能进入计算。"""

    def setUp(self):
        self.df = synthetic_df()
        self.day = self.df.index[250]          # 信号日(合成数据范围内的交易日)
        self.day_str = str(self.day.date())
        self.tmp = tempfile.mkdtemp(prefix="snap_asof_")
        self.snap_dir = os.path.join(self.tmp, "cons_snapshots")
        os.makedirs(self.snap_dir, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_snapshot(self, date_str, ratio=0.8):
        path = os.path.join(self.snap_dir, f"{date_str}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"indexes": {"中证500": {"above_ma250_ratio": ratio, "valid": 100, "cons": 100}}}, f)
        return path

    def test_future_snapshot_never_enters_signal(self):
        # 只有晚于信号日的快照 -> 不得进入评分(宽度缺失)
        future = (self.day + pd.Timedelta(days=10)).date().isoformat()
        self._write_snapshot(future, ratio=0.8)
        with mock.patch.object(vd, "SNAP_DIR", self.snap_dir):
            sig = vd.build_signal_for("中证500", self.df, self.day, "balanced", None, None, "2026-08-06T10:00:00")
        self.assertIsNone(sig["breadth_score"])
        self.assertNotIn("breadth", sig["data_sources"])
        self.assertTrue(any("宽度缺失" in d for d in sig["degraded_fields"]),
                        sig["degraded_fields"])

    def test_asof_snapshot_enters_signal(self):
        # 信号日当天/之前的快照 -> 正常进入评分
        self._write_snapshot(self.day_str, ratio=0.8)
        with mock.patch.object(vd, "SNAP_DIR", self.snap_dir):
            sig = vd.build_signal_for("中证500", self.df, self.day, "balanced", None, None, "2026-08-06T10:00:00")
        self.assertEqual(sig["breadth_score"], 2)
        self.assertEqual(sig["data_sources"]["breadth"]["data_date"], self.day_str)
        self.assertNotEqual(sig["data_sources"]["breadth"]["status"], "future")

    def test_future_dollar_never_enters_gold_macro_score(self):
        # 美元历史含信号日之后的未来日期(值 999) -> 截断到信号日, 未来值不入黄金宏观评分
        base_dates = pd.bdate_range(self.day - pd.Timedelta(days=70), self.day)   # 约50个交易日
        values = 100.0 + np.arange(len(base_dates)) * 0.1
        hist_clean = pd.Series(values, index=base_dates)
        future_dates = pd.date_range(self.day + pd.Timedelta(days=1), periods=5, freq="D")
        hist_future = pd.concat([hist_clean, pd.Series([999.0] * 5, index=future_dates)])

        sig_clean = vd.build_signal_for("黄金", self.df, self.day, "balanced", None, hist_clean, "t")
        sig_future = vd.build_signal_for("黄金", self.df, self.day, "balanced", None, hist_future, "t")
        # 未来值绝不能改变评分: 两次 macro 一致, 且 data_date 是信号日本身
        self.assertEqual(sig_clean["macro_score"], sig_future["macro_score"])
        self.assertEqual(sig_future["data_sources"]["dollar"]["data_date"], self.day_str)
        self.assertEqual(sig_future["data_sources"]["dollar"]["status"], "ok")
        values, last_date = vd.dollar_asof(hist_future, self.day)
        self.assertNotIn(999.0, values)
        self.assertEqual(last_date, self.day_str)

    def test_future_valuation_date_reported_not_fresh(self):
        # 未来日期的估值字段 -> staleness 为负、状态 future, 不得标"完整"
        close = list(np.linspace(100, 200, 300))
        future = (self.day + pd.Timedelta(days=5)).date().isoformat()
        sig = v2.compute_signal(
            "中证500", close=close, spot=close[-1],
            pe_history=[10.0] * 200, pe_now=10,
            signal_date=self.day_str,
            data_dates={"price": self.day_str, "valuation": future})
        src = sig["data_sources"]["valuation"]
        self.assertLess(src["staleness_days"], 0)
        self.assertEqual(src["status"], "future")
        self.assertNotEqual(sig["data_quality"], "完整")
        self.assertTrue(any("晚于信号日" in d for d in sig["degraded_fields"]))
        self.assertNotEqual(sig["action"], "买入")


class FutureInvalidTests(unittest.TestCase):
    """2. 未来数据必须返回 invalid(禁止 max(0) 伪装新鲜)。"""

    def test_staleness_days_keeps_negative_for_future(self):
        days = v2.staleness_days({"price": "2026-08-10", "valuation": "2026-08-01"},
                                 "2026-08-08")
        self.assertEqual(days["price"], -2)      # 未来 -> 负值, 不是 max(0) 的 0
        self.assertEqual(days["valuation"], 7)
        self.assertEqual(v2._status_for("price", -2), "future")
        self.assertEqual(v2._status_for("price", 3), "ok")     # 3天 < price stale 阈值5
        self.assertEqual(v2._status_for("price", 7), "stale")

    def test_future_breadth_no_buy(self):
        close = list(np.linspace(100, 200, 300))
        sig = v2.compute_signal(
            "中证500", close=close, spot=close[-1],
            pe_history=[10.0] * 200, pe_now=10, breadth_ratio=0.8,
            signal_date="2026-07-31",
            data_dates={"breadth": "2026-08-05", "price": "2026-07-31"})
        self.assertEqual(sig["data_sources"]["breadth"]["status"], "future")
        self.assertNotEqual(sig["action"], "买入")


class BackfillTests(unittest.TestCase):
    """3. 到期历史记录能够自动回填收益(遍历全部记录, 只追加不覆盖)。"""

    def _synthetic_log(self, day_idx=100, shadow="买入"):
        df = synthetic_df()
        name = "中证500"
        day = df.index[day_idx]
        entry = {
            "strategy_version": "category_v2_shadow_v4",
            "candidate": "no_candidate",
            "signal_date": str(day.date()),
            "price_date": str(day.date()),
            "formal": "持有",
            "shadow": shadow,
            "valuation_score": 1.0,
        }
        log = {
            "meta": {"observation_id": "obs-test", "strategy_version": "category_v2_shadow_v4",
                     "observation_start": "2025-01-01", "candidate_map": {}},
            "2025-06-15": {"date": "2025-06-15", "entries": {name: entry}},
        }
        return log, df, name, day

    def test_backfill_adds_fwd_fields_and_preserves_signal(self):
        log, df, name, day = self._synthetic_log(day_idx=100)
        # 回填前: 无 fwd 字段
        entry = log["2025-06-15"]["entries"][name]
        self.assertNotIn("fwd_1w", entry)
        # 手动预期: 用同一动作序列模拟
        acts = {day: entry["shadow"]}
        res = bt.simulate(df, acts, cost=0.001, delay=1, exec_mode="close")
        i0 = df.index.get_loc(day)
        expected = res["strat_nav"].iloc[i0 + 5] / res["strat_nav"].iloc[i0] - 1

        filled, added = sl.backfill_log(log, {name: df})
        self.assertEqual(filled, 1)
        self.assertGreaterEqual(added, 3)   # fwd_1w/4w/12w
        self.assertIn("fwd_1w", entry)
        self.assertAlmostEqual(entry["fwd_1w"], expected, places=10)
        self.assertIn("bh_1w", entry)
        self.assertIn("common_v1_1w", entry)
        # 历史信号字段绝不被覆盖/重算
        self.assertEqual(entry["formal"], "持有")
        self.assertEqual(entry["shadow"], "买入")
        self.assertEqual(entry["valuation_score"], 1.0)
        self.assertEqual(entry["signal_date"], str(day.date()))

    def test_backfill_idempotent(self):
        log, df, name, _ = self._synthetic_log(day_idx=100)
        sl.backfill_log(log, {name: df})
        snapshot = json.loads(json.dumps(log))
        filled2, added2 = sl.backfill_log(log, {name: df})
        self.assertEqual(filled2, 0)
        self.assertEqual(added2, 0)
        self.assertEqual(log, snapshot)

    def test_archived_records_never_backfilled(self):
        # 归档记录不属于当前观察期 -> 不回填、不参与动作序列
        log, df, name, day = self._synthetic_log(day_idx=100)
        log["archive/obs-old/2025-06-15"] = log.pop("2025-06-15")
        filled, _ = sl.backfill_log(log, {name: df})
        self.assertEqual(filled, 0)

    def test_expired_1w_only_fills_what_has_elapsed(self):
        # 只有 5 个交易日到期: fwd_1w 有, 4w/12w 未到期 -> 不填
        df = synthetic_df(days=106)
        name = "中证500"
        day = df.index[100]
        entry = {"strategy_version": "category_v2_shadow_v4", "candidate": "no_candidate",
                 "signal_date": str(day.date()), "formal": "持有", "shadow": "买入"}
        log = {"meta": {"observation_id": "obs-test", "strategy_version": "category_v2_shadow_v4",
                        "observation_start": "2025-01-01", "candidate_map": {}},
               "2025-06-15": {"date": "2025-06-15", "entries": {name: entry}}}
        sl.backfill_log(log, {name: df})
        self.assertIn("fwd_1w", entry)
        self.assertNotIn("fwd_4w", entry)
        self.assertNotIn("fwd_12w", entry)


class CandidatePersistenceTests(unittest.TestCase):
    """4. 研究候选能够落盘并在下次运行后保留(完整 dict, 同数据同规则)。"""

    def test_candidates_research_full_dicts_saved_and_reloaded(self):
        df = synthetic_df()
        name = "沪深300"     # no_candidate 资产
        day = df.index[150]
        dollar_now, dollar_hist = None, None
        fetched_at = "2026-08-06T10:00:00"
        # 与正式影子信号同一 build_signal_for(同数据/同陈旧度/同类规则)
        research = {c: vd.build_signal_for(name, df, day, c, dollar_now, dollar_hist, fetched_at)
                    for c in ("value", "balanced", "trend")}
        for c, sig in research.items():
            self.assertIsInstance(sig, dict)
            self.assertIn("action", sig)
            self.assertIn("valuation_score", sig)
            self.assertIn("data_sources", sig)
            self.assertIn("degraded_fields", sig)
            self.assertIn("composite_score", sig)

        # 落盘 round-trip: 写入临时 shadow_log, 重载后保留
        tmp = tempfile.mkdtemp(prefix="shadow_persist_")
        try:
            with mock.patch.object(sl, "LOG_FILE", os.path.join(tmp, "shadow_log.json")):
                entry = {"strategy_version": "category_v2_shadow_v4", "candidate": "no_candidate",
                         "signal_date": str(day.date()), "formal": "卖出", "shadow": "持有",
                         "candidates_research": research}
                log = {"meta": {"observation_id": "obs-test", "strategy_version": "category_v2_shadow_v4",
                                "observation_start": "2025-01-01", "candidate_map": {}},
                       "2025-06-15": {"date": "2025-06-15", "entries": {name: entry}}}
                sl.save_log(log)
                reloaded = sl.load_log()
                saved = reloaded["2025-06-15"]["entries"][name]["candidates_research"]
                self.assertEqual(sorted(saved.keys()), ["balanced", "trend", "value"])
                self.assertEqual(saved["value"]["asset_name"], "沪深300")
                self.assertEqual(saved["value"]["strategy_version"], "category_v2_shadow_v4")
                self.assertIsNotNone(saved["value"]["structural_score"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_no_candidate_research_uses_same_data_dates(self):
        # 候选与正式影子信号使用同一时点数据: data_sources 的 price/valuation 日期一致
        df = synthetic_df()
        name = "沪深300"
        day = df.index[150]
        formal = vd.build_signal_for(name, df, day, "no_candidate", None, None, "t")
        cand = vd.build_signal_for(name, df, day, "value", None, None, "t")
        for field in ("price", "valuation"):
            self.assertEqual(formal["data_sources"][field]["data_date"],
                             cand["data_sources"][field]["data_date"],
                             f"{field} 日期不一致: 候选策略数据时点与正式不同")


class DollarAsOfTests(unittest.TestCase):
    """5. 美元历史包含多日期时能正常解析(保留日期并截断到信号日)。"""

    def test_dollar_asof_parses_multi_date_series(self):
        hist = pd.Series([100.0, 101.5, 99.0, 102.0, 103.0, 104.5],
                         index=pd.to_datetime(["2026-06-01", "2026-06-15", "2026-07-01",
                                               "2026-07-15", "2026-07-31", "2026-08-04"]))
        values, last_date = vd.dollar_asof(hist, "2026-07-31")
        self.assertEqual(values, [100.0, 101.5, 99.0, 102.0, 103.0])
        self.assertEqual(last_date, "2026-07-31")
        self.assertNotIn(104.5, values)   # 08-04 未来值排除

    def test_dollar_asof_no_data_before_signal(self):
        hist = pd.Series([100.0], index=pd.to_datetime(["2026-08-05"]))
        values, last_date = vd.dollar_asof(hist, "2026-07-31")
        self.assertIsNone(values)
        self.assertIsNone(last_date)

    def test_dollar_asof_empty(self):
        self.assertEqual(vd.dollar_asof(None, "2026-07-31"), (None, None))
        self.assertEqual(vd.dollar_asof(pd.Series(dtype=float), "2026-07-31"), (None, None))


class PhaseBacktestTests(unittest.TestCase):
    """6. 阶段回测只累计该阶段选中的日收益。"""

    def test_phase_metrics_accumulates_only_selected_days(self):
        df = synthetic_df(days=160)
        # 两段 bull(30..69, 90..129)夹着非 bull 日(70..89): 共 80 个 bull 日(>= min_days 60)
        phase = pd.Series("range", index=df.index)
        phase.iloc[30:70] = "bull"
        phase.iloc[90:130] = "bull"
        # 策略: 确定性动作序列, 让收益非零
        actions = {df.index[30]: "买入", df.index[69]: "卖出", df.index[90]: "买入"}
        result = bt.simulate(df, actions, cost=0.001, delay=1, exec_mode="close")
        stat = bt.phase_metrics(result, phase, "bull")
        self.assertIsNotNone(stat)
        self.assertEqual(stat["days"], 80)
        # 阶段总收益 = 只累计选中日收益的连乘
        sel_ret = result.loc[phase[phase == "bull"].index, "strat_ret"].fillna(0)
        expected_total = float(np.prod(1 + sel_ret.to_numpy()) - 1)
        years = 80 / bt.TRADING_DAYS
        expected_ann = (1 + expected_total) ** (1 / years) - 1
        self.assertAlmostEqual(stat["ann"], expected_ann, places=9)
        # 与旧口径(绝对 nav 归一化)不同: 段间 70..89 非 bull 日收益不应计入
        old_style = result["strat_nav"].iloc[129] / result["strat_nav"].iloc[30] - 1
        self.assertNotAlmostEqual(stat["ann"], old_style, places=3)

    def test_phase_metrics_bh_only_selected_days(self):
        df = synthetic_df(days=160)
        phase = pd.Series("range", index=df.index)
        phase.iloc[10:70] = "bear"    # 60 个 bear 日
        result = bt.simulate(df, {df.index[10]: "买入"}, cost=0.001, delay=1, exec_mode="close")
        stat = bt.phase_metrics(result, phase, "bear")
        self.assertEqual(stat["days"], 60)
        bh_sel_ret = result["bh_nav"].pct_change().fillna(0).loc[phase[phase == "bear"].index]
        expected_bh = float(np.prod(1 + bh_sel_ret.to_numpy()) - 1)
        years = 60 / bt.TRADING_DAYS
        self.assertAlmostEqual(stat["bh_ann"], (1 + expected_bh) ** (1 / years) - 1, places=9)

    def test_phase_metrics_too_short_returns_none(self):
        df = synthetic_df(days=160)
        phase = pd.Series("range", index=df.index)
        phase.iloc[10:70] = "bull"    # 60 日 -> 达标
        result = bt.simulate(df, {}, cost=0.001, delay=1, exec_mode="close")
        self.assertIsNotNone(bt.phase_metrics(result, phase, "bull"))
        short = pd.Series("range", index=df.index)
        short.iloc[10:40] = "bull"    # 30 日 < 60 -> 样本不足
        self.assertIsNone(bt.phase_metrics(result, short, "bull", min_days=60))


class ObservationArchiveTests(unittest.TestCase):
    """观察期归档: 旧批次记录不得混入新观察期。"""

    def test_archive_moves_old_observation_records(self):
        log = {
            "meta": {"observation_id": "obs-2026-08-06", "strategy_version": "category_v2_shadow_v4",
                     "observation_start": "2026-08-06",
                     "superseded_observation": "obs-2026-08-05",
                     "candidate_map": {}},
            "2026-08-05": {"date": "2026-08-05", "entries": {"沪深300": {"candidate": "no_candidate"}}},
            "legacy/2026-08-05": {"date": "2026-08-05", "entries": {}},
        }
        moved = sl.archive_superseded(log)
        self.assertEqual(moved, 1)
        self.assertNotIn("2026-08-05", log)
        self.assertIn("archive/obs-2026-08-05/2026-08-05", log)
        self.assertIn("legacy/2026-08-05", log)   # 旧日志保留
        self.assertEqual(list(sl.active_record_keys(log, log["meta"])), [])

    def test_active_record_keys_skip_archive(self):
        log = {
            "meta": {"observation_id": "obs-x", "strategy_version": "category_v2_shadow_v4",
                     "observation_start": "2026-01-01", "candidate_map": {}},
            "2026-01-10": {"entries": {}},
            "2026-01-17": {"entries": {}},
            "archive/obs-old/2025-12-31": {"entries": {}},
            "legacy/2025-12-24": {"entries": {}},
        }
        self.assertEqual(list(sl.active_record_keys(log, log["meta"])),
                         ["2026-01-10", "2026-01-17"])


class FutureEqualsMissingTests(unittest.TestCase):
    """v4.1: 未来字段在计算所有分项之前等同缺失, 各项分数与该字段缺失时完全一致。"""

    def test_hs300_future_pb_erp_equals_missing(self):
        close = list(np.linspace(100, 200, 300))
        base = v2.compute_signal("沪深300", close=close, spot=close[-1],
                                 pe_history=[10.0] * 200, pe_now=10,
                                 signal_date="2026-07-31",
                                 data_dates={"price": "2026-07-31"})
        fut = v2.compute_signal("沪深300", close=close, spot=close[-1],
                                pe_history=[10.0] * 200, pe_now=10,
                                pb_history=[1.5] * 200, pb_now=3.0,
                                erp_history=[5.0] * 200, erp_now=0.5,
                                signal_date="2026-07-31",
                                data_dates={"price": "2026-07-31",
                                            "pb": "2026-08-05", "erp": "2026-08-05"})
        for k in ("valuation_score", "structural_score", "composite_score",
                  "action", "data_quality"):
            self.assertEqual(base[k], fut[k], f"未来 PB/ERP 影响了 {k}")
        # 沪深300 估值需至少两项 -> 未来 PB/ERP 等同缺失后只剩 PE -> 估值分 None
        self.assertIsNone(fut["valuation_score"])
        self.assertIn("pb", fut["data_sources"])
        self.assertEqual(fut["data_sources"]["pb"]["status"], "future")

    def test_gold_future_macro_equals_missing(self):
        close = list(np.linspace(100, 200, 300))
        base = v2.compute_signal("黄金", close=close, spot=close[-1],
                                 signal_date="2026-07-31",
                                 data_dates={"price": "2026-07-31"})
        fut = v2.compute_signal("黄金", close=close, spot=close[-1],
                                us10y_history=[3.5] * 60,
                                dollar_history=[100.0] * 60, dollar_now=102.0,
                                signal_date="2026-07-31",
                                data_dates={"price": "2026-07-31",
                                            "us10y": "2026-08-05", "dollar": "2026-08-05"})
        for k in ("macro_score", "structural_score", "composite_score", "action"):
            self.assertEqual(base[k], fut[k], f"未来美债/美元影响了 {k}")
        self.assertIsNone(fut["macro_score"])

    def test_breadth_future_equals_missing(self):
        close = list(np.linspace(100, 200, 300))
        base = v2.compute_signal("中证500", close=close, spot=close[-1],
                                 pe_history=[10.0] * 200, pe_now=10,
                                 signal_date="2026-07-31",
                                 data_dates={"price": "2026-07-31"})
        fut = v2.compute_signal("中证500", close=close, spot=close[-1],
                                pe_history=[10.0] * 200, pe_now=10, breadth_ratio=0.8,
                                signal_date="2026-07-31",
                                data_dates={"price": "2026-07-31", "breadth": "2026-08-05"})
        for k in ("breadth_score", "structural_score", "composite_score", "available_weight_ratio"):
            self.assertEqual(base[k], fut[k], f"未来宽度影响了 {k}")
        self.assertIsNone(fut["breadth_score"])
        self.assertIn("宽度缺失", fut["degraded_fields"])

    def test_industry_earnings_future_equals_missing(self):
        close = list(np.linspace(100, 200, 300))
        base = v2.compute_signal("中证白酒", close=close, spot=close[-1],
                                 pe_history=[10.0] * 200, pe_now=10,
                                 signal_date="2026-07-31",
                                 data_dates={"price": "2026-07-31"})
        fut = v2.compute_signal("中证白酒", close=close, spot=close[-1],
                                pe_history=[10.0] * 200, pe_now=10, earnings_cycle=0.9,
                                signal_date="2026-07-31",
                                data_dates={"price": "2026-07-31", "earnings": "2026-08-05"})
        for k in ("earnings_score", "structural_score", "composite_score"):
            self.assertEqual(base[k], fut[k], f"未来盈利影响了 {k}")
        self.assertIsNone(fut["earnings_score"])


class StalenessAliasTests(unittest.TestCase):
    """v4.1: pb/erp→valuation、us10y/dollar→macro 共用阈值, 15 天宏观必须 severe。"""

    def test_macro_15_days_severe(self):
        # macro 阈值 stale=10/severe=15
        self.assertEqual(v2._status_for("us10y", 15), "severe")
        self.assertEqual(v2._status_for("dollar", 15), "severe")
        self.assertEqual(v2._status_for("us10y", 12), "stale")
        self.assertEqual(v2._status_for("dollar", 10), "stale")

    def test_valuation_alias_thresholds(self):
        # valuation 阈值 stale=10/severe=20: pb/erp 共用, 不落默认阈值(10/20)
        self.assertEqual(v2._status_for("pb", 19), "stale")
        self.assertEqual(v2._status_for("pb", 20), "severe")
        self.assertEqual(v2._status_for("erp", 25), "severe")
        self.assertEqual(v2._status_for("erp", 5), "ok")

    def test_macro_15_days_no_buy_gold(self):
        close = list(np.linspace(100, 200, 300))
        sig = v2.compute_signal("黄金", close=close, spot=close[-1],
                                us10y_history=[3.5] * 60,
                                signal_date="2026-07-31",
                                data_dates={"us10y": "2026-07-16", "price": "2026-07-31"})
        self.assertEqual(sig["data_sources"]["us10y"]["staleness_days"], 15)
        self.assertEqual(sig["data_sources"]["us10y"]["status"], "severe")
        self.assertNotEqual(sig["action"], "买入")


class PbErpDateConsistencyTests(unittest.TestCase):
    """v4.1: PB/ERP 显示日期与陈旧天数一致(build_data_meta 分别接收真实日期)。"""

    def test_pb_erp_data_date_matches_staleness(self):
        df = synthetic_df()
        day = df.index[250]
        # 让 PB/ERP 最后 3 个交易日缺失 -> 最近有效日期 = 信号日前 3 个交易日
        df.loc[df.index[248]:, "pb"] = np.nan
        df.loc[df.index[248]:, "erp"] = np.nan
        sig = vd.build_signal_for("沪深300", df, day, "balanced", None, None, "t")
        for field in ("pb", "erp"):
            src = sig["data_sources"][field]
            src_date = pd.Timestamp(src["data_date"]).date()
            expect_days = (day.date() - src_date).days
            self.assertEqual(src["staleness_days"], expect_days,
                             f"{field} 的 data_date 与 staleness_days 不一致")
            self.assertGreater(src["staleness_days"], 0, f"{field} 应早于信号日")
            self.assertEqual(src["status"], v2._status_for(field, src["staleness_days"]))


class CrossFieldFutureTests(unittest.TestCase):
    """v4.2: 分字段精确清空 — 未来字段只清自身, 其他有效分项必须保留。

    交叉验证: 有效PE+ERP/未来PB == 仅PB缺失; 有效PE+PB/未来ERP == 仅ERP缺失;
    有效PB+ERP/未来PE == 仅PE缺失; 有效美债/未来美元 == 仅美元缺失。"""

    PE = [10.0] * 200
    PB = [1.5] * 200
    ERP = [5.0] * 200

    def _hs300(self, future_field=None, pe=None, pb=None, erp=None):
        close = list(np.linspace(100, 200, 300))
        data_dates = {"price": "2026-07-31"}
        if future_field:
            data_dates[future_field] = "2026-08-05"
        return v2.compute_signal(
            "沪深300", close=close, spot=close[-1],
            pe_history=pe, pe_now=pe[-1] if pe else None,
            pb_history=pb, pb_now=pb[-1] if pb else None,
            erp_history=erp, erp_now=erp[-1] if erp else None,
            signal_date="2026-07-31", data_dates=data_dates)

    def test_valid_pe_erp_future_pb_equals_pb_missing(self):
        base = self._hs300(pe=self.PE, pb=None, erp=self.ERP)          # 仅PB缺失
        fut = self._hs300("pb", pe=self.PE, pb=self.PB, erp=self.ERP)  # 未来PB
        for k in ("valuation_score", "structural_score", "composite_score"):
            self.assertEqual(base[k], fut[k], f"未来PB影响了有效分项 {k}")
        self.assertIsNotNone(fut["valuation_score"])   # PE+ERP 有效 -> 估值分保留
        self.assertEqual(fut["data_sources"]["pb"]["status"], "future")
        self.assertTrue(any("未来数据已按缺失处理: pb" in d for d in fut["degraded_fields"]),
                        fut["degraded_fields"])

    def test_valid_pe_pb_future_erp_equals_erp_missing(self):
        base = self._hs300(pe=self.PE, pb=self.PB, erp=None)           # 仅ERP缺失
        fut = self._hs300("erp", pe=self.PE, pb=self.PB, erp=self.ERP) # 未来ERP
        for k in ("valuation_score", "structural_score", "composite_score"):
            self.assertEqual(base[k], fut[k], f"未来ERP影响了有效分项 {k}")
        self.assertIsNotNone(fut["valuation_score"])
        self.assertEqual(fut["data_sources"]["erp"]["status"], "future")

    def test_valid_pb_erp_future_pe_equals_pe_missing(self):
        base = self._hs300(pe=None, pb=self.PB, erp=self.ERP)          # 仅PE缺失
        fut = self._hs300("valuation", pe=self.PE, pb=self.PB, erp=self.ERP)  # 未来PE
        for k in ("valuation_score", "structural_score", "composite_score"):
            self.assertEqual(base[k], fut[k], f"未来PE影响了有效分项 {k}")
        self.assertIsNotNone(fut["valuation_score"])   # PB+ERP 有效 -> 估值分保留
        self.assertEqual(fut["data_sources"]["valuation"]["status"], "future")
        self.assertTrue(any("未来数据已按缺失处理: valuation" in d for d in fut["degraded_fields"]),
                        fut["degraded_fields"])

    def test_valid_us10y_future_dollar_equals_dollar_missing(self):
        close = list(np.linspace(100, 200, 300))
        base = v2.compute_signal("黄金", close=close, spot=close[-1],
                                 us10y_history=[3.5] * 60, dollar_history=None,
                                 signal_date="2026-07-31",
                                 data_dates={"price": "2026-07-31"})      # 仅美元缺失
        fut = v2.compute_signal("黄金", close=close, spot=close[-1],
                                us10y_history=[3.5] * 60, dollar_history=[100.0] * 60,
                                signal_date="2026-07-31",
                                data_dates={"price": "2026-07-31", "dollar": "2026-08-05"})
        for k in ("macro_score", "structural_score", "composite_score"):
            self.assertEqual(base[k], fut[k], f"未来美元影响了有效分项 {k}")
        self.assertIsNotNone(fut["macro_score"])       # 有效美债 -> 宏观分保留(利率方向)
        self.assertEqual(fut["data_sources"]["dollar"]["status"], "future")
        self.assertTrue(any("未来数据已按缺失处理: dollar" in d for d in fut["degraded_fields"]))

    def test_other_valid_components_kept_with_future_warning(self):
        # 除未来字段本身告警外, 其他有效分项必须保留: 与"仅PB缺失"基线完全一致
        fut = self._hs300("pb", pe=self.PE, pb=self.PB, erp=self.ERP)
        clean = self._hs300(pe=self.PE, pb=None, erp=self.ERP)
        self.assertEqual(fut["valuation_score"], clean["valuation_score"])
        self.assertEqual(fut["structural_score"], clean["structural_score"])
        self.assertEqual(fut["composite_score"], clean["composite_score"])
        self.assertIsNotNone(fut["valuation_score"])
        # 未来告警存在, 但 PE/ERP 有效分项没有被误清
        self.assertTrue(any("未来数据已按缺失处理: pb" in d for d in fut["degraded_fields"]))
        self.assertTrue(any("pb数据日期晚于信号日" in d for d in fut["degraded_fields"]))


if __name__ == "__main__":
    unittest.main()
