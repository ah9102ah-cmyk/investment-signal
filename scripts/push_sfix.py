# -*- coding: utf-8 -*-
"""推送修复后的 index.html (根 + web 副本)"""
import base64, json, os, urllib.request

TOKEN = None
for line in open(os.path.expanduser("~/.git-credentials"), encoding="utf-8"):
    if "@github.com" in line:
        TOKEN = line.strip().split("https://")[-1].split(":")[1].split("@")[0]
        break
assert TOKEN and TOKEN.startswith("gho_"), "no valid token"

REPO = "ah9102ah-cmyk/investment-signal"
PROXY = "http://127.0.0.1:7890"
TARGET_BRANCH = "dev"  # 不直接写 main
LOCAL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "index.html")
REMOTES = ["index.html", "web/index.html"]

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

with open(LOCAL, "rb") as f:
    content = base64.b64encode(f.read()).decode()

for remote in REMOTES:
    sha = None
    st, info = api("/contents/" + remote)
    if st == 200:
        sha = info.get("sha")
    body = {"message": "同步本地验证后的页面修复", "content": content,
            "branch": TARGET_BRANCH}
    if sha:
        body["sha"] = sha
    st2, r = api("/contents/" + remote, "PUT", body)
    print("PUT", remote, "->", st2, (r.get("commit", {}).get("sha", "")[:8] if isinstance(r, dict) else r))
