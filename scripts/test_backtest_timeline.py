# -*- coding: utf-8 -*-
"""回测执行口径合成时间线测试(P1-1 更正, 2026-08)。

验证:
- 信号日不产生策略收益
- 成交日前不产生策略收益
- 开盘成交: 使用真实开盘价(执行日收益 = 收盘/开盘-1)
- 收盘成交: 执行日按旧持仓计收益, 新持仓从下一日开始
- 成交成本只在真实换仓日扣除
- 不同执行延迟(1/2/3/5)结果正确
- 未来数据变化不影响历史信号
- common_v1 与 V2 使用相同成交假设
- 年化换手率计算正确
- 回测起始 NAV 归一化为 1
"""
import os, sys, unittest
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtest_v2 as bt


def synthetic_df(days=30, open_gap=0.995, day_gain=0.01, start=100.0):
    """构造确定性行情: 每日开盘 = 前收 × open_gap, 收盘 = 开盘 × (1+day_gain)。

    => ret_oc(开盘->收盘) = day_gain 恒定; ret_cc = open_gap*(1+day_gain)-1 恒定。"""
    idx = pd.bdate_range("2026-01-01", periods=days)
    close = np.zeros(days)
    openp = np.zeros(days)
    close[0] = start
    openp[0] = start
    for i in range(1, days):
        openp[i] = close[i - 1] * open_gap
        close[i] = openp[i] * (1 + day_gain)
    df = pd.DataFrame({"open": openp, "close": close}, index=idx)
    df["ret"] = df["close"].pct_change().fillna(0)
    df["ret_oc"] = df["close"] / df["open"] - 1
    df["pe"] = 15.0
    df["rsi"] = 50.0
    df["chg5"] = df["close"] / df["close"].shift(5) - 1
    df["ma20"] = df["close"].rolling(20).mean()
    df["ma60"] = df["close"].rolling(60).mean()
    return df


COST = 0.001
GAP = 0.995
GAIN = 0.01
RET_CC = GAP * (1 + GAIN) - 1     # ≈0.00495
RET_OC = GAIN                     # 0.01


class ExecutionTimelineTests(unittest.TestCase):
    def setUp(self):
        self.df = synthetic_df(30)
        self.actions = {self.df.index[5]: "买入", self.df.index[15]: "卖出"}

    def test_signal_day_produces_no_return(self):
        # 信号日在第5天, 第6天收盘成交: 第5天收益必须为0
        res = bt.simulate(self.df, self.actions, cost=COST, delay=1, exec_mode="close")
        self.assertAlmostEqual(res["strat_ret"].iloc[5], 0.0, places=12)

    def test_close_mode_no_return_before_execution(self):
        res = bt.simulate(self.df, self.actions, cost=COST, delay=1, exec_mode="close")
        # 第0~5天: 空仓无收益
        self.assertTrue(np.allclose(res["strat_ret"].iloc[:6], 0.0, atol=1e-12))
        # 第6天: 收盘成交, 当日无收益, 只扣成本
        self.assertAlmostEqual(res["strat_ret"].iloc[6], -COST, places=12)
        # 第7天起: 按收盘-收盘计收益
        self.assertAlmostEqual(res["strat_ret"].iloc[7], RET_CC, places=12)
        self.assertAlmostEqual(res["strat_ret"].iloc[15], RET_CC, places=12)

    def test_close_mode_sell_earns_last_full_day(self):
        res = bt.simulate(self.df, self.actions, cost=COST, delay=1, exec_mode="close")
        # 第16天收盘卖出: 当天仍按持仓计收益(持有到收盘), 另扣成本
        self.assertAlmostEqual(res["strat_ret"].iloc[16], RET_CC - COST, places=12)
        # 第17天起空仓
        self.assertAlmostEqual(res["strat_ret"].iloc[17], 0.0, places=12)

    def test_open_mode_uses_real_open(self):
        res = bt.simulate(self.df, self.actions, cost=COST, delay=1, exec_mode="open")
        # 第6天开盘买入: 当日收益 = 收盘/开盘-1 = GAIN(真实开盘价)
        self.assertAlmostEqual(res["strat_ret"].iloc[6], RET_OC - COST, places=12)
        # 第7天起: 未换仓 -> 收盘-收盘
        self.assertAlmostEqual(res["strat_ret"].iloc[7], RET_CC, places=12)
        # 第16天开盘卖出: 当日收益0(开盘即卖出), 只扣成本
        self.assertAlmostEqual(res["strat_ret"].iloc[16], -COST, places=12)
        # 对照: 若错误地用信号日收盘成交(旧bug), 第6天收益会是 RET_CC
        self.assertNotAlmostEqual(res["strat_ret"].iloc[6], RET_CC, places=6)

    def test_cost_only_on_real_change_days(self):
        res = bt.simulate(self.df, self.actions, cost=COST, delay=1, exec_mode="close")
        cost_days = res.index[res["cost_day"] > 0].tolist()
        self.assertEqual(cost_days, [self.df.index[6], self.df.index[16]])

    def test_delays_shift_correctly(self):
        for delay in (1, 2, 3, 5):
            res = bt.simulate(self.df, self.actions, cost=COST, delay=delay, exec_mode="close")
            buy_exec = 5 + delay
            sell_exec = 15 + delay
            self.assertAlmostEqual(res["strat_ret"].iloc[buy_exec], -COST, places=12,
                                   msg=f"delay={delay} 买入执行日应只扣成本")
            self.assertAlmostEqual(res["strat_ret"].iloc[buy_exec + 1], RET_CC, places=12,
                                   msg=f"delay={delay} 买入后次日才开始计收益")
            self.assertAlmostEqual(res["strat_ret"].iloc[sell_exec], RET_CC - COST, places=12,
                                   msg=f"delay={delay} 卖出执行日按旧持仓计收益")
            self.assertAlmostEqual(res["strat_ret"].iloc[sell_exec + 1], 0.0, places=12,
                                   msg=f"delay={delay} 卖出后次日空仓")

    def test_start_nav_normalized(self):
        res = bt.simulate(self.df, self.actions, cost=COST, delay=1, exec_mode="close")
        self.assertAlmostEqual(res["strat_nav"].iloc[0], 1.0, places=12)

    def test_common_v1_and_v2_same_execution(self):
        # 同一个 simulate() 被 common_v1 与 V2 共用: 在相同动作下执行语义必须一致
        df = synthetic_df(300)
        for name in ("中证500", "红利低波"):
            if name == "红利低波":
                df2 = df.copy()
                df2["pe"] = 10.0
            else:
                df2 = df.copy()
            acts_v1 = bt.common_v1_actions(df2, name)
            acts_v2 = bt.v2_shadow_actions(df2, name, "balanced")
            r1 = bt.simulate(df2, acts_v1, cost=COST, delay=2, exec_mode="open")
            r2 = bt.simulate(df2, acts_v2, cost=COST, delay=2, exec_mode="open")
            # 两套策略结果都满足: 任何收益只出现在持仓生效后(执行日+1 之后的开盘口径)
            for r in (r1, r2):
                buy_days = [i for i, d in enumerate(df2.index) if acts_v1.get(d) == "买入" or acts_v2.get(d) == "买入"]
                first_pos = int(np.argmax(r["pos"].to_numpy() > 0)) if (r["pos"] > 0).any() else None
                if first_pos is not None and buy_days:
                    # 持仓首次生效日必须 >= 首个买入信号日 + 延迟(开盘口径, 执行日当日即可生效)
                    self.assertGreaterEqual(first_pos, min(buy_days) + 2)

    def test_annualized_turnover(self):
        df = synthetic_df(300)
        actions = {df.index[50]: "买入", df.index[150]: "卖出"}
        res = bt.simulate(df, actions, cost=COST, delay=1, exec_mode="close")
        years = len(df) / bt.TRADING_DAYS
        # 换手 = 2 次(买入+卖出), 每单位换仓幅度1
        self.assertAlmostEqual(res["turnover"].sum(), 2.0, places=9)
        stat = bt.metrics(res)
        self.assertAlmostEqual(stat["turnover_annual"], 2.0 / years, places=9)


class FutureDataPerturbationTests(unittest.TestCase):
    def test_future_data_does_not_change_past_signal(self):
        df = synthetic_df(300)
        df["pe"] = [10 + (i % 50) for i in range(300)]
        df["us10y"] = 3.5
        day = df.index[250]
        sig1 = bt.v2_signal_at(df, "中证500", "balanced", day)
        # 篡改评估日之后的所有数据
        df.loc[df.index[251]:, "close"] = 99999.0
        df.loc[df.index[251]:, "pe"] = 1.0
        df.loc[df.index[251]:, "open"] = 1.0
        sig2 = bt.v2_signal_at(df, "中证500", "balanced", day)
        self.assertEqual(sig1["action"], sig2["action"])
        for k in ("structural_score", "technical_score", "composite_score",
                  "valuation_score", "trend_score", "momentum_score"):
            self.assertEqual(sig1[k], sig2[k], f"{k} 不应受未来数据影响")

    def test_future_price_does_not_change_common_v1(self):
        df = synthetic_df(300)
        df["pe"] = [10 + (i % 50) for i in range(300)]
        day = df.index[240]
        a1 = bt.common_v1_actions(df, "中证500").get(day)
        df.loc[df.index[241]:, "close"] = 99999.0
        a2 = bt.common_v1_actions(df, "中证500").get(day)
        self.assertEqual(a1, a2)


if __name__ == "__main__":
    unittest.main()
