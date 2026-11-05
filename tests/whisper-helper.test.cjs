const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const vm = require('node:vm');
const source = readFileSync(require.resolve('../extension/whisper-helper.js'), 'utf8');
function controller(fetch, native) {
  const context = vm.createContext({ fetch, AbortSignal, URL, chrome: { runtime: {
    sendNativeMessage: native, lastError: null
  } } });
  vm.runInContext(source, context);
  return context.BocWhisperHelper;
}
const ready = () => Response.json({ service: 'boc-whisper-helper', version: '1.1', auto_setup: true });

test('running helper is reused without a native launch', async () => {
  const api = controller(async () => ready(), () => assert.fail('must reuse running helper'));
  assert.equal((await api.ensure('http://127.0.0.1:8788')).auto_setup, true);
});
test('offline default helper is woken through the fixed native command', async () => {
  let count = 0;
  const api = controller(async () => { if (++count === 1) throw Error('offline'); return ready(); }, (host, message, callback) => {
    assert.equal(host, 'com.boc.desk');
    assert.equal(message.service, 'whisper');
    callback({ ok: true, running: true });
  });
  assert.equal((await api.ensure('http://localhost:8788')).auto_setup, true);
  assert.equal(count, 2);
});
test('custom remote addresses never cause local native launches', async () => {
  const api = controller(async () => { throw Error('offline'); }, () => assert.fail('must not launch'));
  for (const url of ['http://example.test:8788', 'http://127.0.0.1:9999', 'http://127.0.0.1:8788/proxy']) {
    await assert.rejects(api.ensure(url), /无法连接指定/);
  }
});
test('setup sends the explicit model and surfaces old-helper errors', async () => {
  let posted;
  const api = controller(async (url, options) => {
    if (url.endsWith('/health')) return ready();
    posted = JSON.parse(options.body);
    return Response.json({ ok: true, setup: { status: 'running' } });
  }, () => assert.fail());
  assert.equal((await api.setup('http://127.0.0.1:8788', { start: true, model: 'custom.bin' })).setup.status, 'running');
  assert.equal(posted.model, 'custom.bin');
  const old = controller(async () => Response.json({ version: '1.0' }), () => assert.fail());
  await assert.rejects(old.setup('http://127.0.0.1:8788'), /更新 helper/);
});
test('missing native installation produces actionable instructions', async () => {
  const api = controller(async () => { throw Error('offline'); }, (host, message, callback) => callback({ ok: false, error: 'launcher failed' }));
  await assert.rejects(api.ensure('http://127.0.0.1:8788'), /launcher failed/);
});
