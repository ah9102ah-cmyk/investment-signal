# AGENTS.md — Codex 开发规则

这是"投资信号"项目的开发上下文。开发前必读。完整交接细节见 `HANDOFF.md`,项目结构见 `README.md`。

## 项目一句话

静态投资参考网页(基金+股票自选,三维度信号、五维评分、12个月估值区间、实时行情),数据 = 每日 09:00 云端 Actions 抓取 + 浏览器端实时拉取。

## 铁律(违反会惹恼用户)

1. **不要主动推送到 GitHub**。用户明确要求:改动先在本地验证,用户发话后才推送。推送 = `py scripts/push_stocks.py` / `py scripts/push_sfix.py`(Contents API,需 ~/.git-credentials PAT + 代理 127.0.0.1:7890)。
2. **不要替用户下交易指令**。页面/邮件只给数据、评分、判断依据,结论用"偏强/中性/偏弱/需留意"这类观察语气,不给买卖指令。
3. **判断必须"技术面 + 基本面估值"双轮**,不能只看单指标。
4. **文案要大白话**。页面和邮件用"最近在上涨/估值偏便宜/涨得偏急"这种说法,不用"空头排列/RSI 背离"术语;红涨绿跌。
5. **凭证绝不入代码**。PAT/SMTP授权码/爱基金密钥只存在于 ~/.git-credentials、GitHub Secrets、~/.aijijin/credentials.json,别写进文件或提交。

## 核心文件

| 文件 | 作用 | 改它之前注意 |
|---|---|---|
| `web/index.html` | 主页面(ASSETS 列表、V/T/M/S 计算、股票评价面板、实时行情拉取) | ASSETS 加股票要同步 `scripts/stocks.json` |
| `scripts/fetch_fundamentals.py` | 数据管道: 财务/行情 → 五维评分/12个月估值区间 → fund.json | 改了要本地重跑生成 fund.json |
| `scripts/stocks.json` | 股票清单 | 加 A股用 6 位代码,港股用 5 位(hk 前缀在 index.html ASSETS) |
| `data/fund.json`、`web/data/fund.json` | 线上主数据与本地页面副本(每次跑脚本后同时更新) | 两份内容必须一致 |

## 修改后的验证流程

1. `py scripts/fetch_fundamentals.py`(重算 fund.json,看输出有没有异常/降级)
2. `cd web && py -3 -m http.server 8765` 本地起服务
3. 浏览器打开 `http://127.0.0.1:8765` 检查: 控制台无 JS 报错、股票 tab 每只评分/估值区间/评价正常、V 缺失时 S 显示“—”、其余 S = V+T+M
4. 全部通过后**报告用户,等用户说推送**才推

## 当前状态(2026-08-03 交接时)

- 本地 `web/index.html` + `scripts/fetch_fundamentals.py` + `web/data/fund.json` **含一批未推送改动**(详见 HANDOFF.md 第三节),线上 GitHub 还是旧版。
- 下一步: 验证这批改动 → 用户确认后推送 → 确认每日 09:00 云端自动更新正常。
