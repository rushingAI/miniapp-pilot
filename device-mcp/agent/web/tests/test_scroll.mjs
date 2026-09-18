// 流式跟随底部的状态机单测。纯函数、无 DOM，node --test 直接跑：
//   cd device-mcp && node --test agent/web/tests/*.mjs
// （目录名不能直接传给 node --test，会被当模块解析；必须给 *.mjs 让 shell 展开）
import { test } from 'node:test';
import assert from 'node:assert';
import { nextStick, NEAR } from '../scroll.js';

// 造一次 scroll 事件时刻的几何量测
const m = (scrollTop, lastTop, scrollHeight, clientHeight = 600) =>
  ({ scrollTop, lastTop, scrollHeight, clientHeight });

test('贴底时追加一大块内容，迟到的 scroll 事件不得解除跟随', () => {
  // 用户本来贴底(scrollTop=400, scrollHeight=1000, clientHeight=600 → gap=0)。
  // 追加了一个 500px 高的块 → scrollHeight=1500，scrollTop 还没被我们改。
  // 旧实现在这一刻用 gap(=500) > 120 反推出"用户在看历史"→ 从此永久锁死。
  assert.equal(nextStick(true, m(400, 400, 1500)), true);
});

test('用户往上翻(scrollTop 变小且离底远) → 脱离跟随', () => {
  assert.equal(nextStick(true, m(100, 900, 1500)), false);
});

test('用户手动滚回底部 → 自动恢复跟随', () => {
  assert.equal(nextStick(false, m(900, 100, 1500)), true);
});

test('贴底附近小幅上滑(仍在阈值内) → 不算脱离', () => {
  assert.equal(nextStick(true, m(900 - (NEAR - 10), 900, 1500)), true);
});

test('已脱离时内容继续增长(scrollTop 不变) → 保持脱离，不被拽回', () => {
  assert.equal(nextStick(false, m(100, 100, 3000)), false);
});

test('已脱离时用户继续往上翻 → 仍脱离', () => {
  assert.equal(nextStick(false, m(50, 100, 3000)), false);
});
