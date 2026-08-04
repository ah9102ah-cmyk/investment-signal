# -*- coding: utf-8 -*-
"""推送: fetch_fundamentals.py + fund.json + index.html + workflow
覆盖远程根和 web/ 两处(页面主源=根 data/fund.json, 降级链 raw 指向根)"""
import base64, json, os, urllib.request

TOKEN = None
for line in open(os.path.expanduser("~/.git-credentials"), encoding="utf-8"):
    if "@github.com" in line:
        TOKEN = line.strip().split("@")[0].split(":")[-1]
        break
assert TOKEN and TOKEN.startswith("gho_"), "no valid token"

REPO = "ah9102ah-cmyk/investment-signal"
PROXY = "http://127.0.0.1:7890"
TARGET_BRANCH = "dev"  # 不直接写 main
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# (本地绝对路径, 远程路径)
FILES = [
    (os.path.join(ROOT, "web", "index.html"), "index.html"),
    (os.path.join(ROOT, "scripts", "fetch_fundamentals.py"), "fetch_fundamentals.py"),
    (os.path.join(ROOT, "data", "fund.json"), "data/fund.json"),
    (os.path.join(ROOT, ".github", "workflows", "fundamentals.yml"), ".github/workflows/fundamentals.yml"),
    (os.path.join(ROOT, "scripts", "fetch_fundamentals.py"), "web/fetch_fundamentals.py"),
    (os.path.join(ROOT, "web", "data", "fund.json"), "web/data/fund.json"),
]

def api(path, method="GET", body=None):
    url = "https://api.github.com/repos/" + REPO + path
    if method == "GET":
        url += ("&" if "?" in url else "?") + "ref=" + TARGET_BRANCH
    req = urllib.request.Request(url, method=method)
    req.add_header("Authorization", "Bearer " + TOKEN)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "hermes")
    if body is not None:
        req.add_header("Content-Type", "application/json")
        data = json.dumps(body).encode("utf-8")
    else:
        data = None
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
    try:
        with opener.open(req, data=data, timeout=60) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))

for local, remote in FILES:
    with open(local, "rb") as f:
        content = base64.b64encode(f.read()).decode()
    sha = None
    st, info = api("/contents/" + remote)
    if st == 200:
        sha = info.get("sha")
    else:
        print("GET", remote, st, "(新建)")
    body = {"message": "同步本地验证后的基本面与估值修复", "content": content,
            "branch": TARGET_BRANCH}
    if sha:
        body["sha"] = sha
    st2, r = api("/contents/" + remote, "PUT", body)
    print("PUT", remote, "->", st2, r.get("commit", {}).get("sha", "")[:8] if isinstance(r, dict) else r)
