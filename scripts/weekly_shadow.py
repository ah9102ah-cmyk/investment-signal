# -*- coding: utf-8 -*-
"""每周影子观察(包装脚本, 2026-08 更正版)。

P1-5 更正执行顺序:
  1. snapshot_cons.py(先生成宽度快照)
  2. 检查快照成功、日期正确、有效样本比例达标
  3. shadow_log.py(影子对比, 宽度数据来自刚生成的快照)
  4. 写周报
快照失败时: 影子策略可降级运行, 但必须记录"宽度数据失败/陈旧", 不得静默使用旧数据标记完整。

P2-4: 仓库根目录从 __file__ 计算, 不写死绝对路径(供 GitHub Actions 与本地 cron 共用)。

用法: py scripts/weekly_shadow.py   (或 hermes cron 直接调本脚本)
"""
import json
import os
import subprocess
import sys
import datetime as dt

# 仓库根目录: 本脚本位于 <repo>/scripts/, 上一级即仓库根
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
SNAP_DIR = os.path.join(REPO, "data", "cons_snapshots")


def run(name, script, timeout=1800):
    print(f"===== {name}  {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} =====")
    try:
        r = subprocess.run([sys.executable, os.path.join(SCRIPTS, script)], cwd=SCRIPTS,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"[ERROR] {script} 超时({timeout}s)")
        return 1
    print(r.stdout)
    if r.returncode != 0 and r.stderr:
        print("[stderr]", r.stderr[-800:])
    return r.returncode


def check_snapshot():
    """检查最新快照: 日期是否今天/最近、各指数有效样本比例是否达标。

    返回 (ok, 描述文本)。不达标时影子策略可降级运行, 但记录失败/陈旧。"""
    if not os.path.isdir(SNAP_DIR):
        return False, "宽度快照目录不存在: 快照失败"
    files = sorted(f for f in os.listdir(SNAP_DIR) if f.endswith(".json"))
    if not files:
        return False, "宽度快照为空: 快照失败"
    latest = files[-1]
    date_str = latest[:-5]
    days_old = (dt.date.today() - dt.date.fromisoformat(date_str)).days
    try:
        snap = json.load(open(os.path.join(SNAP_DIR, latest), encoding="utf-8"))
    except Exception as e:
        return False, f"宽度快照读取失败({latest}): {e}"
    bad = []
    for iname, info in snap.get("indexes", {}).items():
        if isinstance(info, dict) and "error" in info:
            bad.append(f"{iname}={info['error']}")
        elif isinstance(info, dict) and info.get("valid", 0) < max(10, info.get("cons", 50) * 0.5):
            bad.append(f"{iname}有效样本{info.get('valid')}/{info.get('cons')}")
    ok = days_old <= 7 and not bad
    note = f"最新快照 {date_str}({days_old}天前)" + (f"; 异常: {'; '.join(bad)}" if bad else "; 各指数有效比例达标")
    return ok, note


if __name__ == "__main__":
    rc1 = run("成分宽度快照", "snapshot_cons.py")
    snap_ok, snap_note = check_snapshot()
    print(f"[snapshot] {snap_note}")
    rc2 = run("影子观察对比", "shadow_log.py")
    if not snap_ok:
        # 影子策略降级运行, 但必须把快照失败/陈旧写进周报记录(shadow_log 会按陈旧度降级)
        print(f"[WARN] 宽度数据失败/陈旧({snap_note}): 影子策略降级运行, 已按陈旧度标记")
    print("== 每周影子观察完成 ==")
    sys.exit(0 if rc1 == 0 and rc2 == 0 else 1)
