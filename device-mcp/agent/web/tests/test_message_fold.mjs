import assert from 'node:assert/strict';
import test from 'node:test';
import { compactLegacyToolResult, foldAgentOutput, foldChatDelta } from '../message_fold.js';

test('agent output only folds when it exceeds 200 characters', () => {
  const exact = '中'.repeat(200);
  const long = `${exact}文`;

  assert.deepEqual(foldAgentOutput(exact), {
    folded: false,
    preview: exact,
    text: exact,
  });
  assert.deepEqual(foldAgentOutput(long), {
    folded: true,
    preview: `${exact}…`,
    text: long,
  });
});

test('agent output character limit counts unicode code points', () => {
  const text = `${'🙂'.repeat(200)}尾`;
  const result = foldAgentOutput(text);

  assert.equal(result.folded, true);
  assert.equal(Array.from(result.preview.slice(0, -1)).length, 200);
});

test('long tool chat delta uses the same 200 character fold rule', () => {
  const text = JSON.stringify({notes: '规则'.repeat(120)});
  const result = foldChatDelta({role: 'tool', text});

  assert.equal(result.folded, true);
  assert.equal(result.text, text);
  assert.equal(Array.from(result.preview.slice(0, -1)).length, 200);
});

test('legacy tool image payload is omitted before it enters the DOM', () => {
  const legacy = [
    JSON.stringify({path: '/evidence/after.png', scale: 2}),
    JSON.stringify({type: 'image', data: 'A'.repeat(200_000), mimeType: 'image/jpeg'}),
  ].join('\n');

  const compact = compactLegacyToolResult(legacy);
  const result = foldChatDelta({role: 'tool', text: legacy});

  assert.match(compact, /binary omitted: 200000 chars/);
  assert.doesNotMatch(compact, /A{100}/);
  assert.equal(result.text, compact);
  assert.ok(result.text.length < 500);
});

test('assistant text and ordinary tool text remain unchanged', () => {
  const assistant = JSON.stringify({type: 'image', data: 'not-a-tool-result'});
  const ordinaryTool = JSON.stringify({status: 'ok', notes: '保留完整结构'});

  assert.equal(foldChatDelta({role: 'assistant', text: assistant}).text, assistant);
  assert.equal(foldChatDelta({role: 'tool', text: ordinaryTool}).text, ordinaryTool);
});
