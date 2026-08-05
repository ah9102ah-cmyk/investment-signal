# -*- coding: utf-8 -*-
"""每周影子观察(包装脚本): 顺序执行 shadow_log.py + snapshot_cons.py。

供 Hermes cron / 手动调用; 内部用 py -3 确保 akshare 环境。
输出周报文本到 stdout(非空), 数据落盘: data/shadow_log.json + data/cons_snapshots/<date>.json。
"""
import os, subprocess, sys, datetime as dt

# 仓库绝对路径(本脚本可能被 cron 从 hermes/scripts 调用, 不能依赖脚本自身位置)
REPO = r"C:\Users\thfwj\Documents\Codex\2026-08-03\chatgpt-codex-1-codex-github-ah9102ah\investment-signal"
SCRIPTS = os.path.join(REPO, "scripts")


def run(name, script):
    print(f"===== {name}  {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} =====")
    try:
        r = subprocess.run(["py", "-3", os.path.join(SCRIPTS, script)], cwd=SCRIPTS,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=600)
    except subprocess.TimeoutExpired:
        print(f"[ERROR] {script} 超时(600s)")
        return 1
    print(r.stdout)
    if r.returncode != 0 and r.stderr:
        print("[stderr]", r.stderr[-600:])
    return r.returncode


if __name__ == "__main__":
    rc1 = run("影子观察对比", "shadow_log.py")
    rc2 = run("成分宽度快照", "snapshot_cons.py")
    print("== 每周影子观察完成 ==")
    sys.exit(0 if rc1 == 0 and rc2 == 0 else 1)
