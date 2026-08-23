# -*- coding: utf-8 -*-
"""2026-08 行业相对化三档评分单测: 差值法/比值法/周期边界/港股档位/降级。"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fetch_fundamentals import (rel_pos, rel_growth, rel_debt,
                                industry_relative_score, cyclical_position,
                                hk_absolute_score)

ok = 0
def check(cond, msg):
    global ok
    assert cond, msg
    ok += 1

# ---- rel_growth 差值法(核心修复: 负中位数方向不反转) ----
check(rel_growth(10.5, -14.2) == 2, "真实茅台案例: 领先行业24.7pp应+2")
check(rel_growth(-8.5, -14.2) == 1, "跌得比行业少5~10pp应+1")
check(rel_growth(-20.0, -14.2) == -1, "跌得比行业多5~10pp应-1")
check(rel_growth(40.0, 28.0) == 2, "领先10pp以上应+2(40-28=12)")
check(rel_growth(34.0, 28.0) == 1, "领先5~10pp应+1")
check(rel_growth(None, 10) is None, "缺数据返回None")
check(rel_growth(10, None) is None, "行业中位缺失返回None")

# ---- rel_pos 比值法(恒正指标) ----
check(rel_pos(21.5, 14.3) == 2, "ROE高出50%以上+2")
check(rel_pos(18.0, 14.3) == 1, "高出25~50% +1(18/14.3=+26%)")
check(rel_pos(15.0, 14.3) == 0, "±25%内为0")
check(rel_pos(10.5, 14.3) == -1, "低25~50% -1(-27%)")
check(rel_pos(6.0, 14.3) == -2, "低50%以上-2")
check(rel_pos(30, None) is None, "中位None->None")

# ---- rel_debt ---- (med=29.3: 阈值 24.9 / 27.84 / 33.70)
check(rel_debt(24.0, 29.3) == 2, "<85%中位 -> +2(24<24.9)")
check(rel_debt(27.5, 29.3) == 1, "85%~95%中位 -> +1(27.5<27.84)")
check(rel_debt(30.0, 29.3) == 0, "±15%内 -> 0")
check(rel_debt(35.0, 29.3) == -1, ">115%中位 -> -1(35>33.7)")

# ---- industry_relative_score 综合 ----
s, v, det = industry_relative_score(32.7, 10.5, 14.1, 91.5,
                                    {"roe": 14.3, "rev_g": -14.2, "debt": 29.3, "gm": 77.8})
# 茅台实测: rel_roe=2, rel_rev=2(差值), rel_debt=2, rel_gm=0(毛利91 vs 中位78 = +17% -> 0)
check(det["rel_roe"] == 2 and det["rel_rev"] == 2 and det["rel_debt"] == 2 and det["rel_gm"] == 0,
      f"茅台四项明细不符: {det}")
check(s == 6 and v == "好", f"茅台总分应为6好, 实得{s}{v}")

s2, v2, d2 = industry_relative_score(None, None, None, None, {"roe": 10, "rev_g": 5, "debt": 50, "gm": 30})
check(s2 == 0 and v2 == "中", "全缺数据降级为0分中评")

# ---- cyclical_position 周期定位 ----
pct, hint, label = cyclical_position([(2020, 8.3), (2021, 37.3), (2022, 34.0), (2023, 11.4), (2024, 4.6), (2025, 9.6)])
check(pct == 15 and hint == 1, f"北方稀土实测应为15%/偏底(+1), 实得{pct}/{hint}")
check("偏底" in label, f"标签应含偏底: {label}")
pct2, hint2, _ = cyclical_position([(2021, 4.0), (2022, 10.0), (2023, 36.0), (2024, 37.0)])
check(hint2 == -1 and pct2 >= 80, f"顶部场景应偏顶(-1), 实得{hint2}/{pct2}")
pct3, hint3, _ = cyclical_position([(2021, 10.0), (2022, 12.0), (2023, 11.0), (2024, 11.5)])
check(hint3 == 0, f"中段应为0, 实得{hint3}")
_, hint4, label4 = cyclical_position([(2024, 9.0)])
check(hint4 is None and "不足" in label4, "少于3期应拒绝并说明历史不足")
pct5, hint5, _ = cyclical_position([(y, float(y)) for y in range(2019, 2025)])  # 单调升
check(pct5 >= 80, f"单调上升序列当前应在高位: {pct5}")

# ---- hk_absolute_score 港股档位 ----
check(hk_absolute_score(11, None) == 2 and hk_absolute_score(14, None) == 1 and
      hk_absolute_score(18, None) == 0 and hk_absolute_score(30, None) == -1, "PE四档")
check(hk_absolute_score(None, 2.5) == 2 and hk_absolute_score(None, 9) == -1, "PB兜底档")
check(hk_absolute_score(None, None) is None, "双缺None")

print(f"行业相对化评分单测: {ok} 项全部通过")
