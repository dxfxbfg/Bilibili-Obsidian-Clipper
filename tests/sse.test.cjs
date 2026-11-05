const { test } = require('node:test');
const assert = require('node:assert/strict');
require('../extension/ai/sse.js');
const { parseOpenAISSE } = globalThis.BocAIStream;
const encode = (text) => new TextEncoder().encode(text);
const event = (text) => `data: ${JSON.stringify({ choices: [{ delta: { content: text } }] })}\n\n`;

function response(chunks) {
  return new Response(new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(chunk);
      controller.close();
    }
  }));
}
async function collect(res) {
  let text = '';
  for await (const token of parseOpenAISSE(res)) text += token;
  return text;
}

test('handles every byte boundary, Unicode, CRLF and end marker', async () => {
  const bytes = encode((event('你好') + event('🌏') + 'data: [DONE]\n\n' + event('ignored')).replaceAll('\n', '\r\n'));
  const res = response([...bytes].map((byte) => new Uint8Array([byte])));
  assert.equal(await collect(res), '你好🌏');
  assert.equal(res.body.locked, false);
});
test('flushes final unterminated event', async () => {
  assert.equal(await collect(response([encode(event('last').trimEnd())])), 'last');
});
test('supports multiline data, comments and CR-only lines', async () => {
  const text = ': heartbeat\rdata: {"choices":\rdata: [{"delta":{"content":"ok"}}]}\r\r';
  assert.equal(await collect(response([encode(text)])), 'ok');
});
test('ignores malformed and metadata events', async () => {
  assert.equal(await collect(response([encode('data: broken\n\ndata: {"usage":{}}\n\n' + event('ok'))])), 'ok');
});
test('surfaces provider errors and releases the lock', async () => {
  const res = response([encode('data: {"error":{"message":"quota exceeded"}}\n\n')]);
  await assert.rejects(collect(res), /quota exceeded/);
  assert.equal(res.body.locked, false);
});
test('cancels the underlying stream when the consumer stops', async () => {
  let cancelled = false;
  const res = new Response(new ReadableStream({
    start(controller) { controller.enqueue(encode(event('first'))); },
    cancel() { cancelled = true; }
  }));
  for await (const token of parseOpenAISSE(res)) { assert.equal(token, 'first'); break; }
  assert.equal(cancelled, true);
  assert.equal(res.body.locked, false);
});
test('propagates stream failures and missing response bodies', async () => {
  const res = new Response(new ReadableStream({ start(c) { c.error(new Error('disconnected')); } }));
  await assert.rejects(collect(res), /disconnected/);
  assert.equal(res.body.locked, false);
  await assert.rejects(collect({}), /数据流/);
});
