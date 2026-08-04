// 页面内纯逻辑测试：不访问网络，也不依赖浏览器。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '..', 'web', 'index.html'), 'utf8');
const match = html.match(/<script>([\s\S]*)<\/script>/);
assert.ok(match, '找不到页面 script 区块');
const source = match[1];

function section(from, to) {
  const start = source.indexOf(from);
  const end = source.indexOf(to, start);
  assert.ok(start >= 0 && end > start, `找不到代码区间: ${from}`);
  return source.slice(start, end);
}

const context = { DATA: {}, FUND: {} };
vm.createContext(context);
vm.runInContext(
  section('function rsi14', 'var CUR =') +
  section('function valuationRangeScore', 'function loadHKQuotes') +
  section('function indexOnOrBefore', 'function renderDay'),
  context
);

assert.equal(context.valuationRangeScore({ target_low: 90, target: 100, target_high: 110 }, 80), 2);
assert.equal(context.valuationRangeScore({ target_low: 90, target: 100, target_high: 110 }, 105), 0);
assert.equal(context.valuationRangeScore({ target_low: 90, target: 100, target_high: 110 }, 120), -2);

context.FUND.sh000300 = { v_score: 1 };
const indexAsset = { symbol: 'sh000300', g: 'fund' };
const priceRow = { v: null, close: 10 };
assert.equal(context.fundVScore(indexAsset, priceRow, false), null, '历史日期不能偷用当前V');
assert.equal(context.fundVScore(indexAsset, priceRow, true), 1);

assert.equal(context.indexOnOrBefore(['2026-07-30', '2026-08-03'], '2026-08-01'), 0);
assert.equal(context.indexOnOrBefore(['2026-07-30', '2026-08-03'], '2026-07-01'), -1);

context.DATA['样例'] = { close: Array.from({ length: 61 }, (_, i) => 100 * Math.pow(1.01, i)) };
const calc = context.calcAt('样例', 60);
assert.equal(calc.m, 2, '上涨动能应为正分');
assert.equal(calc.s, null, '缺少V时内部S也必须为空');
assert.equal(calc.risk, '涨得偏急');

context.DATA['长趋势样例'] = {
  close: Array.from({ length: 250 }, (_, i) => 100 * Math.pow(1.001, i))
};
const longTrend = context.calcAt('长趋势样例', 249);
assert.equal(longTrend.longVotes, 3, '三个中长期周期都应向上');
assert.equal(longTrend.longConsensus, true, '至少两个周期向上且平均为正时应形成共识');
assert.ok(longTrend.vol60 < 1e-10, '恒定日涨幅的波动率应接近0');
assert.equal(context.finalAction(0, longTrend).label, '买入');
assert.equal(context.finalAction(-2, longTrend).label, '持有', '估值过贵时不应显示买入');
assert.equal(
  context.actionForAsset({ g: 'fund' }, 0, longTrend, true).label,
  '买入',
  '观察指数有完整数据时也应输出统一三态结果'
);
const missingValuationAction = context.actionForAsset({ g: 'fund' }, null, longTrend, true);
assert.equal(missingValuationAction.label, '持有', '观察指数缺估值时应保守输出持有/继续观望');
assert.equal(missingValuationAction.quality, '估值缺失', '缺估值必须明确标注，不能静默按0分处理');
assert.equal(
  context.actionForAsset({ g: 'fund' }, null, longTrend, false).label,
  '数据不足',
  '历史回看缺估值时仍应显示数据不足'
);
assert.equal(
  context.finalAction(0, { t: -1, m: 0, longMom: -0.08, longVotes: 1 }).label,
  '卖出'
);

context.DATA['黄金'] = {
  close: Array.from({ length: 250 }, (_, i) => i === 249 ? 110 : 100)
};
const gold = context.calcAt('黄金', 249);
assert.equal(gold.v, -1, '黄金高于250日均线5%~15%时V应为-1');

context.FUND.hkHSTECH = { pe: 18 };
assert.equal(
  context.fundVScore({ symbol: 'hkHSTECH', hk: true, g: 'fund' }, priceRow, true),
  null,
  '没有自身历史分位时，基金指数不应套用港股个股绝对PE阈值'
);

console.log('web logic assertions: OK');
