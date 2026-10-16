const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { test } = require('node:test');
const ts = require('typescript');

const compiled = ts.transpileModule(
  readFileSync(`${__dirname}/../lib/venue-status.ts`, 'utf8'),
  { compilerOptions: { module: ts.ModuleKind.CommonJS } },
);
const loaded = { exports: {} };
new Function('exports', compiled.outputText)(loaded.exports);
const { mergeEngineStatus } = loaded.exports;

test('repeated control events preserve venue details and reference venues', () => {
  const current = {
    trading_enabled: true, uptime: 100, last_price_update: 123,
    venues: [
      ...['uni-base', 'uni-bsc'].map((name) => ({
        name, enabled: true, paused: false,
        position: { balances: { cngn: '0', usdc: '1200' }, lp_position: { in_range: false } },
        price: { quote: { mid: '0.000736' } },
        params: { rebalance_threshold_percent: '10' },
      })),
      { name: 'bybit', enabled: true, paused: false, price: { quote: { mid: '1358' } } },
    ],
  };
  const original = structuredClone(current);
  let result = current;
  for (let i = 0; i < 10; i++) {
    result = mergeEngineStatus(result, {
      trading_enabled: false, uptime: 101 + i,
      venues: ['uni-base', 'uni-bsc'].map((name) => ({ name, enabled: true, paused: true })),
    });
    assert.equal(result.venues.length, 3);
    for (let j = 0; j < 2; j++) {
      assert.equal(result.venues[j].paused, true);
      assert.deepEqual(result.venues[j].position, original.venues[j].position);
      assert.deepEqual(result.venues[j].price, original.venues[j].price);
      assert.deepEqual(result.venues[j].params, original.venues[j].params);
    }
    assert.deepEqual(result.venues[2], original.venues[2]);
  }
  assert.equal(result.trading_enabled, false);
  assert.equal(result.uptime, 110);
  assert.equal(result.last_price_update, 123);
  assert.deepEqual(current, original);
});

test('control events do not seed an incomplete status before the first API response', () => {
  assert.equal(mergeEngineStatus(undefined, {
    trading_enabled: true, uptime: 1, venues: [],
  }), undefined);
});
