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
const indexAsset = { symbol: 'sh000300' };
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

console.log('web logic assertions: OK');
