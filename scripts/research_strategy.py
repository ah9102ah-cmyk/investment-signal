# -*- coding: utf-8 -*-
"""基金策略候选方案研究。

目的不是寻找历史最高收益，而是比较少量有明确含义的固定规则，观察它们在
2019-2022、2023-2024、2025至今三个阶段是否方向一致。
"""
from __future__ import annotations

import math
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtest
import daily_signal as ds
import signal_rules as rules


TRADING_DAYS = 244
COST_RATE = 0.001


def component_frame(df, value_score):
    """把每日价格转换为 V/T/M 与风险数据。"""
    frame = df.copy()
    frame["v"] = value_score.reindex(frame.index)
    frame["t"] = np.select(
        [
            (frame["close"] > frame["ma20"]) & (frame["ma20"] > frame["ma60"]),
            (frame["close"] > frame["ma20"]) & (frame["ma20"] <= frame["ma60"]),
            (frame["close"] <= frame["ma20"]) & (frame["ma20"] > frame["ma60"]),
        ],
        [2, 1, -1],
        default=-2,
    )
    frame["m"] = [
        ds.score_momentum_values(rsi, None if pd.isna(chg5) else chg5)
        for rsi, chg5 in zip(frame["rsi"], frame["chg5"])
    ]
    frame["ret"] = frame["close"].pct_change()
    frame["vol60"] = frame["ret"].rolling(60).std() * math.sqrt(TRADING_DAYS)
    valid = frame[["v", "ma60", "rsi", "chg5"]].notna().all(axis=1)
    if not valid.any():
        raise ValueError("没有可用的 V/T/M 共同历史")
    return frame.loc[valid[valid].index[0]:].copy()


def rule_action(rule, v, t, m):
    """返回 1=进入观察持仓，-1=退出，0=保持。"""
    total = v + t + m
    if rule == "current":
        if total >= 2 and t >= 0 and m >= -1:
            return 1
        if total <= -4 or (t <= -2 and v <= -1):
            return -1
        return 0

    if rule == "balanced":
        # 估值一轮与技术一轮等权：技术轮先把 T/M 平均，避免价格信息算两遍。
        balanced = v + (t + m) / 2
        if balanced >= 1 and t >= 0 and m >= -1:
            return 1
        if balanced <= -2 or (t <= -2 and v <= -1):
            return -1
        return 0

    if rule == "value_guard":
        # 沿用当前总分，但估值已经偏贵时不新进入。
        if total >= 2 and v >= 0 and t >= 0 and m >= -1:
            return 1
        if total <= -3 or t <= -2:
            return -1
        return 0

    if rule == "agreement":
        # 两轮明确同向才变化；中间状态维持，减少反复。
        if v >= 0 and t >= 1 and m >= 0:
            return 1
        if v <= 0 and t <= -1 and m <= 0:
            return -1
        return 0

    if rule == "trend_only":
        # 对照组：不看估值，判断 V 是否真的增加价值。
        if t >= 1 and m >= 0:
            return 1
        if t <= -1 and m <= 0:
            return -1
        return 0
    raise ValueError(f"未知规则: {rule}")


def weekly_vol_scale(frame, target_vol):
    """每周更新一次风险倍率，只降风险、不加杠杆。"""
    scale = (target_vol / frame["vol60"]).clip(upper=1.0)
    week = pd.Series(frame.index.to_period("W-FRI"), index=frame.index)
    week_end = week.ne(week.shift(-1))
    return scale.where(week_end).ffill().fillna(1.0)


def simulate_rule(frame, rule, target_vol=None, cost_rate=COST_RATE):
    holding = 0.0
    decisions = []
    for row in frame[["v", "t", "m"]].itertuples(index=False):
        action = rule_action(rule, row.v, row.t, row.m)
        if action == 1:
            holding = 1.0
        elif action == -1:
            holding = 0.0
        decisions.append(holding)
    desired = pd.Series(decisions, index=frame.index, dtype=float)
    if target_vol is not None:
        desired *= weekly_vol_scale(frame, target_vol)
    effective = desired.shift(1).fillna(0)
    turnover = desired.diff().abs().fillna(desired.abs()).shift(1).fillna(0)
    net_return = effective * frame["ret"].fillna(0) - turnover * cost_rate
    return pd.DataFrame({"ret": net_return, "pos": effective, "turnover": turnover})


def simulate_final_action(frame, rule, cost_rate=COST_RATE):
    """把多周期趋势与V/T/M合成可回测的进入/退出状态。"""
    work = frame.copy()
    moves = [work["close"] / work["close"].shift(horizon) - 1
             for horizon in rules.LONG_TREND_HORIZONS]
    long_average = sum(moves) / len(moves)
    positive_votes = sum(move.gt(0).astype(int) for move in moves)
    long_positive = (positive_votes >= 2) & (long_average > 0)
    long_negative = (positive_votes <= 1) & (long_average < 0)

    holding = 0.0
    desired, actions = [], []
    for i, row in enumerate(work[["v", "t", "m"]].itertuples(index=False)):
        total = row.v + row.t + row.m
        positive = bool(long_positive.iloc[i])
        negative = bool(long_negative.iloc[i])
        old_weak = total <= -4 or (row.t <= -2 and row.v <= -1)

        if rule == "strict":
            enter = positive and total >= 2 and row.t >= 0 and row.m >= -1
            exit_now = old_weak or (negative and row.t <= -1)
        elif rule == "guard":
            action, _ = rules.final_action(
                int(row.v), int(row.t), int(row.m),
                float(long_average.iloc[i]) if not pd.isna(long_average.iloc[i]) else None,
                int(positive_votes.iloc[i]),
            )
            enter = action == "买入"
            exit_now = action == "卖出"
        elif rule == "slow":
            enter = positive and row.v >= -1 and row.t >= 0
            exit_now = old_weak or (negative and row.t <= -1 and row.m <= 0)
        elif rule == "trend_value":
            enter = positive and row.v >= -1
            exit_now = negative or (row.t <= -2 and row.v <= -1)
        else:
            raise ValueError(f"未知最终动作规则: {rule}")

        action = 0
        if not holding and enter:
            holding = 1.0
            action = 1
        elif holding and exit_now:
            holding = 0.0
            action = -1
        desired.append(holding)
        actions.append(action)

    desired = pd.Series(desired, index=work.index, dtype=float)
    effective = desired.shift(1).fillna(0)
    turnover = desired.diff().abs().fillna(desired.abs()).shift(1).fillna(0)
    net_return = effective * work["ret"].fillna(0) - turnover * cost_rate
    return pd.DataFrame({
        "ret": net_return,
        "pos": effective,
        "turnover": turnover,
        "action": pd.Series(actions, index=work.index),
        "long_average": long_average,
        "long_votes": positive_votes,
    })


def rotation_strategy(prices, horizon=40, inverse_vol=False, cost_rate=COST_RATE):
    returns = prices.pct_change()
    momentum = prices / prices.shift(horizon) - 1
    vol60 = returns.rolling(60).std() * math.sqrt(TRADING_DAYS)
    desired = pd.DataFrame(0.0, index=prices.index, columns=prices.columns)
    current = pd.Series(0.0, index=prices.columns)
    weeks = pd.Series(prices.index.to_period("W-FRI"), index=prices.index)
    for i, day in enumerate(prices.index):
        if i >= max(horizon, 60) and (i == len(prices) - 1 or weeks.iloc[i] != weeks.iloc[i + 1]):
            positive = momentum.loc[day][momentum.loc[day] > 0].nlargest(2)
            current[:] = 0.0
            if len(positive):
                if inverse_vol:
                    inv = 1 / vol60.loc[day, positive.index].replace(0, np.nan)
                    weights = inv / inv.sum()
                else:
                    weights = pd.Series(1 / len(positive), index=positive.index)
                current.loc[weights.index] = weights
        desired.loc[day] = current
    effective = desired.shift(1).fillna(0)
    turnover = desired.diff().abs().sum(axis=1).fillna(desired.abs().sum(axis=1)).shift(1).fillna(0)
    net_return = (effective * returns.fillna(0)).sum(axis=1) - turnover * cost_rate
    return pd.DataFrame({"ret": net_return, "pos": effective.sum(axis=1), "turnover": turnover})


def buy_and_hold(prices):
    """期初等额买入后自然漂移，不假装每天免费再平衡。"""
    nav = prices.div(prices.iloc[0]).mean(axis=1)
    return pd.DataFrame({
        "ret": nav.pct_change().fillna(0),
        "pos": 1.0,
        "turnover": 0.0,
    })


def inverse_vol_hold(prices, cost_rate=COST_RATE):
    """始终持有全部资产、每周按近 60 日逆波动配权，作为权重效应对照。"""
    returns = prices.pct_change()
    vol60 = returns.rolling(60).std() * math.sqrt(TRADING_DAYS)
    desired = pd.DataFrame(0.0, index=prices.index, columns=prices.columns)
    current = pd.Series(0.0, index=prices.columns)
    weeks = pd.Series(prices.index.to_period("W-FRI"), index=prices.index)
    for i, day in enumerate(prices.index):
        if i >= 60 and (i == len(prices) - 1 or weeks.iloc[i] != weeks.iloc[i + 1]):
            inv = 1 / vol60.loc[day].replace(0, np.nan)
            current = inv / inv.sum()
        desired.loc[day] = current
    effective = desired.shift(1).fillna(0)
    turnover = desired.diff().abs().sum(axis=1).fillna(desired.abs().sum(axis=1)).shift(1).fillna(0)
    net_return = (effective * returns.fillna(0)).sum(axis=1) - turnover * cost_rate
    return pd.DataFrame({"ret": net_return, "pos": effective.sum(axis=1), "turnover": turnover})


def multi_horizon_rotation(
    prices,
    horizons=rules.LONG_TREND_HORIZONS,
    inverse_vol=True,
    rebalance="weekly",
    rebalance_offset=0,
    execution_delay=1,
    cost_rate=COST_RATE,
):
    """用多个中长期周期形成共识，避免结果依赖某一个恰好好看的参数。"""
    returns = prices.pct_change()
    signals = [prices / prices.shift(horizon) - 1 for horizon in horizons]
    score = sum(signals) / len(signals)
    positive_votes = sum(signal.gt(0).astype(int) for signal in signals)
    required_votes = len(horizons) // 2 + 1
    vol60 = returns.rolling(60).std() * math.sqrt(TRADING_DAYS)
    desired = pd.DataFrame(0.0, index=prices.index, columns=prices.columns)
    current = pd.Series(0.0, index=prices.columns)
    if rebalance == "weekly":
        periods = pd.Series(prices.index.to_period("W-FRI"), index=prices.index)
    elif rebalance == "monthly":
        periods = pd.Series(prices.index.to_period("M"), index=prices.index)
    elif rebalance == "every5":
        periods = None
    else:
        raise ValueError(f"未知调仓频率: {rebalance}")
    warmup = max(max(horizons), 60)

    for i, day in enumerate(prices.index):
        if rebalance == "every5":
            is_rebalance = i >= warmup and (i - warmup - rebalance_offset) % 5 == 0
        else:
            is_rebalance = i >= warmup and (
                i == len(prices) - 1 or periods.iloc[i] != periods.iloc[i + 1]
            )
        if is_rebalance:
            eligible = score.loc[day][positive_votes.loc[day] >= required_votes]
            positive = eligible[eligible > 0].nlargest(2)
            current[:] = 0.0
            if len(positive):
                if inverse_vol:
                    inv = 1 / vol60.loc[day, positive.index].replace(0, np.nan)
                    weights = inv / inv.sum()
                else:
                    weights = pd.Series(1 / len(positive), index=positive.index)
                current.loc[weights.index] = weights
        desired.loc[day] = current

    effective = desired.shift(execution_delay).fillna(0)
    turnover = (
        desired.diff().abs().sum(axis=1).fillna(desired.abs().sum(axis=1))
        .shift(execution_delay).fillna(0)
    )
    net_return = (effective * returns.fillna(0)).sum(axis=1) - turnover * cost_rate
    return pd.DataFrame({"ret": net_return, "pos": effective.sum(axis=1), "turnover": turnover})


def blend_results(left, right, right_weight=0.5):
    """把两套规则当作两个固定资金袖套；不抵消换手，成本估计更保守。"""
    index = left.index.intersection(right.index)
    left_weight = 1 - right_weight
    return pd.DataFrame({
        "ret": left.loc[index, "ret"] * left_weight + right.loc[index, "ret"] * right_weight,
        "pos": left.loc[index, "pos"] * left_weight + right.loc[index, "pos"] * right_weight,
        "turnover": (
            left.loc[index, "turnover"] * left_weight
            + right.loc[index, "turnover"] * right_weight
        ),
    })


def metrics(result, start=None, end=None):
    sample = result.loc[start:end].copy()
    if len(sample) < 60:
        return None
    ret = sample["ret"].fillna(0)
    nav = (1 + ret).cumprod()
    years = len(ret) / TRADING_DAYS
    annual = nav.iloc[-1] ** (1 / years) - 1
    drawdown = (nav / nav.cummax() - 1).min()
    volatility = ret.std() * math.sqrt(TRADING_DAYS)
    sharpe = ret.mean() / ret.std() * math.sqrt(TRADING_DAYS) if ret.std() > 0 else float("nan")
    return {
        "ann": annual,
        "mdd": drawdown,
        "vol": volatility,
        "sharpe": sharpe,
        "invested": sample["pos"].mean(),
        "turnover": sample["turnover"].sum(),
    }


def block_bootstrap_excess(strategy, benchmark, start, block=20, draws=2000, seed=20260804):
    """对日收益差做成对区块抽样；只衡量抽样不确定性，不消除选模偏差。"""
    paired = pd.concat(
        [strategy.loc[start:, "ret"], benchmark.loc[start:, "ret"]], axis=1, join="inner"
    ).dropna()
    diff = paired.iloc[:, 0].to_numpy() - paired.iloc[:, 1].to_numpy()
    n = len(diff)
    rng = np.random.default_rng(seed)
    annual_excess = np.empty(draws)
    for draw in range(draws):
        starts = rng.integers(0, n, size=math.ceil(n / block))
        sample = np.concatenate([
            np.take(diff, np.arange(item, item + block) % n) for item in starts
        ])[:n]
        annual_excess[draw] = sample.mean() * TRADING_DAYS
    return {
        "p_positive": float((annual_excess > 0).mean()),
        "q05": float(np.quantile(annual_excess, 0.05)),
        "q50": float(np.quantile(annual_excess, 0.50)),
        "q95": float(np.quantile(annual_excess, 0.95)),
    }


def portfolio_result(asset_results):
    returns = pd.concat({name: result["ret"] for name, result in asset_results.items()}, axis=1, join="inner")
    positions = pd.concat({name: result["pos"] for name, result in asset_results.items()}, axis=1, join="inner")
    turnover = pd.concat({name: result["turnover"] for name, result in asset_results.items()}, axis=1, join="inner")
    return pd.DataFrame({
        "ret": returns.mean(axis=1),
        "pos": positions.mean(axis=1),
        "turnover": turnover.mean(axis=1),
    })


def fmt(value, kind="pct"):
    if value is None or pd.isna(value):
        return "—"
    if kind == "num":
        return f"{value:.2f}"
    return f"{value * 100:.1f}%"


def main():
    assets = backtest.build_assets()
    frames = {name: component_frame(df, value) for name, (df, value) in assets.items()}
    common_index = None
    for frame in frames.values():
        common_index = frame.index if common_index is None else common_index.intersection(frame.index)
    frames = {name: frame.loc[common_index] for name, frame in frames.items()}

    candidates = {}
    for rule in ("current", "balanced", "value_guard", "agreement", "trend_only"):
        candidates[rule] = portfolio_result({
            name: simulate_rule(frame, rule) for name, frame in frames.items()
        })
    for target in (0.12, 0.15, 0.18):
        candidates[f"current_vol{int(target * 100)}"] = portfolio_result({
            name: simulate_rule(frame, "current", target_vol=target)
            for name, frame in frames.items()
        })
    for action_rule in ("strict", "guard", "slow", "trend_value"):
        candidates[f"action_{action_rule}"] = portfolio_result({
            name: simulate_final_action(frame, action_rule)
            for name, frame in frames.items()
        })

    prices = pd.concat({name: frame["close"] for name, frame in frames.items()}, axis=1, join="inner")
    # 最长候选需要 240 日历史；所有横向比较从同一起点开始，避免现金热身期美化结果。
    fair_start = common_index[240]
    candidates["equal_hold"] = buy_and_hold(prices.loc[fair_start:])
    candidates["invvol_hold"] = inverse_vol_hold(prices)
    for horizon in (40, 80, 120, 200):
        candidates[f"mom{horizon}"] = rotation_strategy(prices, horizon=horizon, inverse_vol=False)
        candidates[f"mom{horizon}_iv"] = rotation_strategy(prices, horizon=horizon, inverse_vol=True)
    candidates["mom_multi_iv"] = multi_horizon_rotation(prices)
    candidates["mom_multi_monthly_iv"] = multi_horizon_rotation(prices, rebalance="monthly")
    candidates["blend50"] = blend_results(
        candidates["current_vol12"], candidates["mom_multi_iv"], right_weight=0.5
    )
    candidates["blend50_monthly"] = blend_results(
        candidates["current_vol12"], candidates["mom_multi_monthly_iv"], right_weight=0.5
    )

    periods = {
        "公平样本": (fair_start, None),
        "训练热身后-2022": (fair_start, "2022-12-31"),
        "验证2023-24": ("2023-01-01", "2024-12-31"),
        "后段2025+": ("2025-01-01", None),
    }
    print()
    print(f"共同样本: {common_index.min().date()} ~ {common_index.max().date()} ({len(common_index)}日)")
    print(f"公平比较起点: {fair_start.date()}（最长 240 日规则完成热身）")
    print("说明: 第一轮候选事先固定；多周期与组合是敏感度检查后的稳健性研究，2025+不再算严格盲测。")
    for period, (start, end) in periods.items():
        print()
        print("=" * 96)
        print(period)
        print("=" * 96)
        print(f"{'方案':<18}{'年化':>9}{'回撤':>9}{'波动':>9}{'夏普':>9}{'平均投入':>10}{'单边换手':>10}")
        rows = []
        for name, result in candidates.items():
            stat = metrics(result, start, end)
            if stat is not None:
                rows.append((name, stat))
        rows.sort(key=lambda item: item[1]["sharpe"], reverse=True)
        for name, stat in rows:
            print(f"{name:<18}{fmt(stat['ann']):>9}{fmt(stat['mdd']):>9}{fmt(stat['vol']):>9}"
                  f"{fmt(stat['sharpe'], 'num'):>9}{fmt(stat['invested']):>10}{fmt(stat['turnover'], 'num'):>10}")

    print()
    print("=" * 96)
    print("最终动作候选（每个指数独立；进入与退出使用不同门槛）")
    print("=" * 96)
    print(f"{'规则':<18}{'全段年化':>12}{'全段回撤':>12}{'训练年化':>12}"
          f"{'验证年化':>12}{'后段年化':>12}{'平均投入':>12}{'单边换手':>12}")
    for rule in ("strict", "guard", "slow", "trend_value"):
        result = candidates[f"action_{rule}"]
        full = metrics(result, fair_start, None)
        train = metrics(result, fair_start, "2022-12-31")
        valid = metrics(result, "2023-01-01", "2024-12-31")
        later = metrics(result, "2025-01-01", None)
        print(f"{rule:<18}{fmt(full['ann']):>12}{fmt(full['mdd']):>12}"
              f"{fmt(train['ann']):>12}{fmt(valid['ann']):>12}{fmt(later['ann']):>12}"
              f"{fmt(full['invested']):>12}{fmt(full['turnover'], 'num'):>12}")

    print()
    print("=" * 96)
    print("动量周期敏感度(逆波动权重；不按测试段挑参数)")
    print("=" * 96)
    print(f"{'周期':<8}{'训练年化':>10}{'训练夏普':>10}{'验证年化':>10}{'验证夏普':>10}"
          f"{'测试年化':>10}{'测试夏普':>10}{'全段回撤':>10}")
    for horizon in (120, 160, 180, 200, 220, 240):
        result = rotation_strategy(prices, horizon=horizon, inverse_vol=True)
        train = metrics(result, fair_start, "2022-12-31")
        valid = metrics(result, "2023-01-01", "2024-12-31")
        test = metrics(result, "2025-01-01", None)
        full = metrics(result, fair_start, None)
        print(f"{horizon:<8}{fmt(train['ann']):>10}{fmt(train['sharpe'], 'num'):>10}"
              f"{fmt(valid['ann']):>10}{fmt(valid['sharpe'], 'num'):>10}"
              f"{fmt(test['ann']):>10}{fmt(test['sharpe'], 'num'):>10}{fmt(full['mdd']):>10}")

    print()
    print("=" * 96)
    print("成本敏感度(200日正动量+逆波动权重)")
    print("=" * 96)
    print(f"{'单边成本':<12}{'全段年化':>12}{'全段回撤':>12}{'全段夏普':>12}{'测试年化':>12}{'测试夏普':>12}")
    for cost in (0.001, 0.002, 0.003, 0.005):
        result = rotation_strategy(prices, horizon=200, inverse_vol=True, cost_rate=cost)
        full = metrics(result, fair_start, None)
        test = metrics(result, "2025-01-01", None)
        print(f"{cost * 100:<11.1f}%{fmt(full['ann']):>12}{fmt(full['mdd']):>12}"
              f"{fmt(full['sharpe'], 'num'):>12}{fmt(test['ann']):>12}{fmt(test['sharpe'], 'num'):>12}")

    print()
    print("=" * 96)
    print("多周期共识与组合比例敏感度（动量袖套权重）")
    print("=" * 96)
    print(f"{'方案':<16}{'全段年化':>12}{'全段回撤':>12}{'全段夏普':>12}"
          f"{'训练年化':>12}{'验证年化':>12}{'测试年化':>12}")
    multi = candidates["mom_multi_iv"]
    multi_monthly = candidates["mom_multi_monthly_iv"]
    for label, result in (
        ("多周期", multi),
        ("多周期按月", multi_monthly),
        ("组合25%", blend_results(candidates["current_vol12"], multi, 0.25)),
        ("组合50%", candidates["blend50"]),
        ("组合75%", blend_results(candidates["current_vol12"], multi, 0.75)),
        ("组合50%按月", candidates["blend50_monthly"]),
    ):
        full = metrics(result, fair_start, None)
        train = metrics(result, fair_start, "2022-12-31")
        valid = metrics(result, "2023-01-01", "2024-12-31")
        test = metrics(result, "2025-01-01", None)
        print(f"{label:<16}{fmt(full['ann']):>12}{fmt(full['mdd']):>12}"
              f"{fmt(full['sharpe'], 'num'):>12}{fmt(train['ann']):>12}"
              f"{fmt(valid['ann']):>12}{fmt(test['ann']):>12}")

    print()
    print("=" * 96)
    print("多周期共识成本敏感度（组合使用同样成本，未净额抵消换手）")
    print("=" * 96)
    print(f"{'单边成本':<12}{'多周期年化':>14}{'多周期回撤':>14}"
          f"{'组合年化':>14}{'组合回撤':>14}{'组合测试年化':>16}")
    for cost in (0.001, 0.002, 0.003, 0.005):
        defensive = portfolio_result({
            name: simulate_rule(frame, "current", target_vol=0.12, cost_rate=cost)
            for name, frame in frames.items()
        })
        multi_cost = multi_horizon_rotation(prices, cost_rate=cost)
        blend = blend_results(defensive, multi_cost, 0.5)
        multi_full = metrics(multi_cost, fair_start, None)
        blend_full = metrics(blend, fair_start, None)
        blend_test = metrics(blend, "2025-01-01", None)
        print(f"{cost * 100:<11.1f}%{fmt(multi_full['ann']):>14}{fmt(multi_full['mdd']):>14}"
              f"{fmt(blend_full['ann']):>14}{fmt(blend_full['mdd']):>14}"
              f"{fmt(blend_test['ann']):>16}")

    print()
    print("=" * 96)
    print("多周期时点稳健性（公平样本；五日错位不是选参，只检查是否依赖某一天）")
    print("=" * 96)
    print(f"{'检查':<16}{'年化':>12}{'回撤':>12}{'夏普':>12}{'单边换手':>14}")
    timing_checks = [
        (f"延迟{delay}日", multi_horizon_rotation(prices, execution_delay=delay))
        for delay in (1, 2, 3, 5)
    ]
    timing_checks.extend(
        (f"五日错位{offset}", multi_horizon_rotation(
            prices, rebalance="every5", rebalance_offset=offset
        ))
        for offset in range(5)
    )
    for label, result in timing_checks:
        stat = metrics(result, fair_start, None)
        print(f"{label:<16}{fmt(stat['ann']):>12}{fmt(stat['mdd']):>12}"
              f"{fmt(stat['sharpe'], 'num'):>12}{fmt(stat['turnover'], 'num'):>14}")

    print()
    print("=" * 96)
    print("资产依赖检查（每次只保留两类资产；比较同一对子里的趋势与始终逆波动持有）")
    print("=" * 96)
    print(f"{'资产对子':<20}{'趋势年化':>12}{'趋势回撤':>12}{'持有年化':>12}{'持有回撤':>12}")
    pairs = (("红利低波", "沪深300"), ("红利低波", "黄金"), ("沪深300", "黄金"))
    for pair in pairs:
        pair_prices = prices.loc[:, list(pair)]
        trend = metrics(multi_horizon_rotation(pair_prices), fair_start, None)
        control = metrics(inverse_vol_hold(pair_prices), fair_start, None)
        label = "+".join(pair)
        print(f"{label:<20}{fmt(trend['ann']):>12}{fmt(trend['mdd']):>12}"
              f"{fmt(control['ann']):>12}{fmt(control['mdd']):>12}")

    print()
    print("=" * 96)
    print("相对逆波动持有的区块抽样（20日区块、2000次；不包含选模偏差）")
    print("=" * 96)
    print(f"{'方案':<18}{'跑赢概率':>12}{'年化差5%位':>14}{'年化差中位':>14}{'年化差95%位':>14}")
    for label, result in (
        ("多周期", candidates["mom_multi_iv"]),
        ("防守/多周期各半", candidates["blend50"]),
        ("旧双轮+波动12", candidates["current_vol12"]),
    ):
        boot = block_bootstrap_excess(result, candidates["invvol_hold"], fair_start)
        print(f"{label:<18}{fmt(boot['p_positive']):>12}{fmt(boot['q05']):>14}"
              f"{fmt(boot['q50']):>14}{fmt(boot['q95']):>14}")

    print()
    print("=" * 96)
    print("两年分段稳定性")
    print("=" * 96)
    selected = {
        "持有": candidates["equal_hold"],
        "始终持有+逆波动": candidates["invvol_hold"],
        "旧双轮": candidates["current"],
        "旧双轮+波动12": candidates["current_vol12"],
        "动量200+逆波动": rotation_strategy(prices, horizon=200, inverse_vol=True),
        "多周期+逆波动": candidates["mom_multi_iv"],
        "多周期按月+逆波动": candidates["mom_multi_monthly_iv"],
        "防守/多周期各半": candidates["blend50"],
        "防守/按月多周期各半": candidates["blend50_monthly"],
    }
    windows = {
        "热身后-2020": (fair_start, "2020-12-31"),
        "2021-22": ("2021-01-01", "2022-12-31"),
        "2023-24": ("2023-01-01", "2024-12-31"),
        "2025+": ("2025-01-01", None),
    }
    print(f"{'方案':<20}" + "".join(f"{name + '年化':>14}" for name in windows))
    for name, result in selected.items():
        stats = [metrics(result, start, end) for start, end in windows.values()]
        print(f"{name:<20}" + "".join(f"{fmt(stat['ann']):>14}" for stat in stats))


if __name__ == "__main__":
    main()
