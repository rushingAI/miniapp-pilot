import { test } from 'node:test';
import assert from 'node:assert';
import { assistantBubbleAfter, durableMessageKey, sendStopRequest } from '../turns.js';

test('第二轮用户 Prompt 必须关闭上一轮 assistant 气泡', () => {
  const previous = { id: 'assistant-turn-1' };
  assert.equal(assistantBubbleAfter(previous, 'user_message'), null);
  assert.equal(assistantBubbleAfter(previous, 'chat_done'), null);
  assert.equal(assistantBubbleAfter(previous, 'chat_delta'), previous);
});

test('刷新回放优先用稳定的浏览器消息 ID 去重', () => {
  assert.equal(durableMessageKey({client_message_id: 'm1', event_id: 'e1'}), 'm1');
  assert.equal(durableMessageKey({event_id: 'e1'}), 'e1');
});

test('停止请求连接失败时返回未送达结果而不抛出异常', async () => {
  const result = await sendStopRequest(() => Promise.reject(new TypeError('Failed to fetch')));

  assert.deepEqual(result, {
    accepted: false,
    error: '无法连接本机 Companion，停止请求未送达',
  });
});

test('停止请求被后端拒绝时保留后端错误', async () => {
  const result = await sendStopRequest(async () => ({ok: false, error: '当前没有可停止的执行'}));

  assert.deepEqual(result, {
    accepted: false,
    error: '当前没有可停止的执行',
  });
});

test('停止请求在有限时间内未确认时返回超时', async () => {
  const result = await sendStopRequest(() => new Promise(() => {}), 5);

  assert.deepEqual(result, {
    accepted: false,
    error: '停止请求超时，未确认后端是否已接收',
  });
});
