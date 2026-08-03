# 交接文档 (HANDOFF)

> 交接时间: 2026-08-03 | 由 Hermes Agent 交接给 Codex CLI
> 交接人注意: 本文件是唯一完整的状态快照, 动手前先读 AGENTS.md 铁律。

## 一、项目全貌

三个独立但相关的自动化, 全部跑在 GitHub(用户电脑关机也不影响):

| 系统 | 仓库 | 触发 | 内容 |
|---|---|---|---|
| 投资参考网页 | ah9102ah-cmyk/investment-signal | 每日09:00抓数据 + 页面实时行情 | 基金/股票 tab, 三维度信号, 五维评分+目标价, 实时评价 |
| 投资信号邮件 | ah9102ah-cmyk/signal-email | 每日 12:00 / 14:50(北京) | 动量轮动+三维度信号邮件 |
| 持仓建议邮件 | ah9102ah-cmyk/holdings-email | 每日 12:00(北京) | 爱基金持仓(28笔,约108万)+技术面/估值双轮建议 |

本包 = 网页系统源码(其余两个邮件系统源码也在 scripts/ 里, 是本地规范版)。

## 二、已上线(线上正常)部分

- 网页: 基金 tab 全功能、股票 tab 10 只股票(贵州茅台、腾讯控股、汇川技术、超声电子、生益科技、北方稀土、思源电气、北方华创、泡泡玛特、美团)
- 每日 09:00 云端自动更新 fund.json(基本面) —— 已修复双写路径, 真正生效
- 邮件: 两封都走云端 SMTP, 用户已确认收到
- V 列首屏自动显示(S 总分 = V+T+M 已修复)

## 三、★ 本地已改、尚未推送(核心交接点)

线上是旧版, 本地 `web/index.html`、`scripts/fetch_fundamentals.py`、`web/data/fund.json` 比线上新。以下改动**全部只在本包/本地, 线上没有**:

1. **股票实时化**: 打开页面即实时 — 股票 V 分/目标价空间用实时价在浏览器端重算; 港股 PE/PB 打开时实时拉腾讯 qt.gtimg.cn(CORS 已验证开放)
2. **V 列自动刷新**: fund.json 加载完成后自动重渲染(之前要手动点一下才出 V)
3. **股票评价面板**: 股票 tab 每只股票可展开(▸箭头), 显示大白话实时评价: 涨跌趋势 / 上涨劲头(RSI+40日涨幅) / 贵还是便宜(目标价空间+PE回本年限) / 一句话综合
4. **港股财务补全**: 东财港股财务(akshare stock_financial_hk_analysis_indicator_em)→ 五维评分/目标价/ROE/营收/负债/毛利, 与 A 股同一套 compute()
5. **新目标价模型**(重要, 取代旧模型):
   - 增速: 单季同比 → 最近3期平均, 限幅 [-30%, +50%](避免单季噪音, 如汇川 2026Q1 -23% 误伤)
   - 合理PE: 拍脑袋分档表 → **该股自身近2年历史日PE中位数**(区间 8~40), 用 F10 的 TTM每股收益序列 + 腾讯800日K线计算
   - 护栏: 目标价空间 >100% 或 < -90% 不显示(高增速失真)
   - 港股: **券商一致预期目标价**(akshare stock_hk_profit_forecast_et, 全部券商目标价中位数)优先于模型
6. **8 只新股**已加入 stocks.json + ASSETS
7. **页面文案**: 评价左对齐、注脚说明改为"目标价 = 该股自身历史PE中位数 × 预期每股收益"
8. **模型护栏**: 负 EPS 的股票 PE 显示 — 不显示负数(美团等亏损股)

**推送方式**: 用户同意后 `py scripts/push_stocks.py`(推 index/fetch_fundamentals/fund.json/stocks.json 到根+web)+ `py scripts/push_sfix.py`(index.html 根+web)。之后手动触发 fundamentals workflow(dispatch)验证云端能重算, 或等次日 09:00 自动跑。

## 四、目标价模型(当前版, 公式)

```
目标价 = TTM每股收益 × (1 + 平滑增速) × 合理PE

TTM每股收益  = 最新累计EPS + 上年年报EPS - 上年同期累计EPS  (F10 EPSJB 序列)
平滑增速     = 最近3期净利润同比(PARENTNETPROFITTZ)均值, 限幅 [-30, +50]
合理PE       = 该股近2年(800个交易日)日PE中位数, 区间 [8, 40]
              (日PE = 腾讯前复权收盘价 / 当时已知TTM EPS, 剔除 >100 异常)
港股         = 券商一致预期目标价中位数优先(stock_hk_profit_forecast_et)
护栏         = 空间 >100% 或 < -90% 时不显示目标价
```

五维评分(0~10): ROE(>=15:2/>=10:1/>=5:0/负:-2) + 营收增速(>=20:2/>=10:1/>=0:0/负:-2) + 负债率(<40:2/<60:1/<75:0/更高:-1) + 目标价空间(>=20:2/>=0:1/>-20:0/更差:-2) + 毛利率(>=60:1/>=30:0/更低:-1)。好>=5, 中>=2, 差<2。

三维度: V 估值分(指数按PE/股息率、股票按目标价空间实时、黄金按年线乖离、港股按实时PE或PB) + T 趋势分(均线) + M 动能分(RSI) , S = V+T+M。

## 五、数据源清单

| 用途 | 接口 | 备注 |
|---|---|---|
| A股财务 | emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis/ZYZBAjaxNew?type=0&code=SZ300124 | 141字段, ~9期; 港股该接口不可用 |
| A股/港股 K线 | web.ifzq.gtimg.cn/appstock/app/fqkline/get | 前复权, CORS开放, 页面浏览器端也用它 |
| 港股实时 | qt.gtimg.cn/q=hk00700 | PE=f[39] PB=f[43] 市值=f[44], GBK, CORS开放 |
| 港股财务 | akshare stock_financial_hk_analysis_indicator_em(symbol='00700') | 东财, ROE_AVG/OPERATE_INCOME_YOY/HOLDER_PROFIT_YOY/DEBT_ASSET_RATIO/GROSS_PROFIT_RATIO/BASIC_EPS |
| 港股券商预期 | akshare stock_hk_profit_forecast_et(symbol='03690') | 目标价列取中位数; 源站 etnet.com.hk 本地可能 SSL 被墙(降级到模型目标价) |
| 指数估值 | akshare stock_zh_index_value_csindex | 中证官网, 日频 |
| 信号数据 | datahub.py: csindex/新浪期货/新浪国债/乐咕PB | 乐咕海外IP可能失败, 自动降级 PB=0 |
| 爱基金持仓 | aijijin_sdk(本地)/AIJIJIN_CREDS(云端) | 凭证 ~/.aijijin/credentials.json |

## 六、已知问题 / 边界

- **泡泡玛特**: 增速300%+, 任何简单模型算不出合理目标价 → 用券商一致预期(中位数 211 左右)
- **美团**: TTM 亏损(EPS 负) → 无模型目标价、PE 显示 —, 用券商预期 + PB 估值
- **腾讯券商预期**: 本地 etnet SSL 被墙抓不到, 用模型目标价(≈780, 与券商共识接近); 云端网络不同可能抓到
- **恒生科技指数**: 无 PE/PB 数据源, V 永远 —
- **乐咕个股PE**: akshare 1.18.81 已无该接口, 历史PE中位数由 F10+腾讯K线本地计算
- **CDN 缓存**: GitHub Pages 更新后可能滞后 10~60 分钟, 验证用 raw.githubusercontent.com
- **fund.json 双写**: 脚本同时写 scripts/data/fund.json 与 web/data/fund.json(线上 Actions 提交的是根 data/fund.json, 页面主源也是它)

## 七、用户偏好(沟通红线)

- 中文交流; 大白话; 红涨绿跌; 每项分析一小段, 不铺长篇框架
- 给数据自己判断, 不要指令式买卖信号; 警惕追涨杀跌类策略
- 持仓判断必须"技术面+基本面估值"双轮
- 自相矛盾零容忍(同一指数不能两套说法)
- 改动先本地验证, 推送要用户发话

## 八、下一步建议

1. 验证本包未推送改动(本地起服务逐项核对) → 用户确认 → 推送 → 触发 workflow 验证云端
2. 把"合理PE依据"(历史中位数、区间)显示到页面评价里, 增加透明度(用户已表达想看依据)
3. 监控每日 09:00 云端自动更新是否正常(乐咕/东财港股接口海外可达性)
4. 如需: 给 A股也接券商一致预期目标价(当前 akshare 版本无 A股接口, 可找东财 datacenter 盈利预测)
