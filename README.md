# 投资信号项目 (investment-signal)

个人投资参考网页 + 每日观察信号/持仓邮件系统。静态页部署在 GitHub Pages,数据由云端 Actions 每日抓取 + 页面端实时行情组成。

## 目录结构

```
investment-signal/
├── web/
│   ├── index.html              # 主页面(基金/股票 tab、三维度信号、基本面&12个月估值区间、资产明细)
│   ├── strategy.html           # 策略说明页
│   └── data/fund.json          # 基本面数据(每日云端重算, 页面主数据源)
├── scripts/
│   ├── fetch_fundamentals.py   # ★ 数据管道主脚本: 东财F10(财务) + 腾讯(行情/未复权K线) + akshare(港股财务/券商预期/指数估值) -> fund.json
│   ├── stocks.json             # 股票清单(代码->名称), A股6位代码 + 港股5位代码
│   ├── daily_signal.py         # 投资信号计算(动量轮动 + 三维度), 供邮件
│   ├── datahub.py              # 信号数据源(csindex/新浪/乐咕)
│   ├── email_signal.py         # 信号邮件(云端 SMTP 模式 + 本地 agently 模式)
│   ├── holdings_email.py       # 持仓观察邮件(云端 SMTP 模式, 爱基金持仓 -> 双轮判断)
│   ├── aggregate_holdings.py   # 爱基金持仓聚合
│   ├── backtest.py             # 策略回测
│   └── push_*.py               # 用 GitHub Contents API 推送文件到线上仓库(需 ~/.git-credentials 的 PAT + 代理)
└── .github/workflows/fundamentals.yml   # 每日 09:00(北京)抓基本面 -> 提交 fund.json
```

## 数据流

```
[东财 F10 财务] ──┐
[腾讯 实时行情] ──┼─> scripts/fetch_fundamentals.py ─┬> data/fund.json ─────> GitHub Pages 托管
[akshare 港股财务] ┤      (每日 09:00 云端 Actions 跑)                        (页面 fetch)
[akshare 券商预期] ┘                                  └> web/data/fund.json ─> 本地预览/降级副本
[新浪/腾讯 K线] ───────────> 页面浏览器端直接拉取(实时, 盘中更新)
[腾讯 qt.gtimg.cn] ────────> 页面浏览器端拉港股实时 PE/PB
```

## 线上部署

- 页面: https://ah9102ah-cmyk.github.io/investment-signal/
- 仓库: https://github.com/ah9102ah-cmyk/investment-signal (私有)
- **页面主源 = 仓库根 `index.html` + `data/fund.json`**(不是 web/ 子目录);仓库 `web/` 目录是降级副本
- 每日 09:00 北京时间: Actions `update-fundamentals` 跑 `fetch_fundamentals.py` → 提交 `data/fund.json` → Pages 自动重建
- 推送文件到线上: `py scripts/push_stocks.py`(index/fetch/fund.json/stocks)或 `py scripts/push_sfix.py`(仅 index.html 根+web 副本), 走 Contents API, 需要 `~/.git-credentials` 里 PAT 和代理 127.0.0.1:7890

## 常用命令(本地)

```bash
py scripts/fetch_fundamentals.py     # 重算 fund.json(写 data/ 和 web/data/ 两份)
py scripts/test_logic.py             # 核心评分、历史PE和估值区间测试
node scripts/test_web_logic.js       # 页面端动能、估值与历史日期逻辑测试
cd web && py -3 -m http.server 8765  # 本地预览 http://127.0.0.1:8765
```

## 相关仓库(每日邮件, 独立)

- `ah9102ah-cmyk/signal-email` — 投资信号邮件(每日 12:00 + 14:50 北京, SMTP)
- `ah9102ah-cmyk/holdings-email` — 持仓观察邮件(每日 12:00 北京, SMTP)
- Secrets: `QQ_SMTP_CODE`(两仓共用)、`AIJIJIN_CREDS`(仅 holdings)

## 重要约定

- **推送必须经用户同意**。本地可能有未推送改动,先 `git status`/对照线上,验证后再推。
- 不要在任何文件里写死凭证(PAT/授权码/爱基金密钥)。推送脚本从 `~/.git-credentials` 读 PAT。
