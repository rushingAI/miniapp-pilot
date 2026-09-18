// 流式跟随底部（stick-to-bottom）。Claude Code 式：贴底时新内容自动跟随，
// 用户往上翻立刻脱离，用户自己滚回底部又自动恢复。
//
// ⚠️ 本模块存在的原因（别退回去）：跟随与否是**用户意图**，不能在 append 之后
// 从 DOM 几何反推。append 后 gap 恒等于「旧 gap + 新内容高度」，任何
// `if (gap < 阈值) 才滚` 的写法都会被一个超过阈值的大块永久锁死——整个会话
// 再也不跟随，且与用户有没有往上翻毫无关系。
// 所以：意图存成布尔值 stick，只由用户的滚动行为改写。

export const NEAR = 80;   // 离底 ≤ 这么多像素就算"贴底"

/** 一次 scroll 事件后的跟随意图。纯函数，见 tests/test_scroll.mjs。
 *  m = {scrollTop, lastTop, scrollHeight, clientHeight}，lastTop=上一次 scroll 时的 scrollTop。 */
export function nextStick(prev, m, near = NEAR){
  if (m.scrollHeight - m.scrollTop - m.clientHeight <= near) return true;   // 到底了(含用户手动滚回) → 跟随
  if (m.scrollTop < m.lastTop) return false;   // scrollTop 变小 = 用户往上翻。程序化滚动只会把它变大，故这个信号无歧义，
                                               // 不需要"忽略下一次事件"的标志位(浏览器会合并 scroll 事件、标志位会漏计泄漏)
  return prev;   // 变大但没到底：内容增长引发的迟到事件 → 维持原意图，绝不因几何反推而翻转
}

/** 绑定到滚动容器，返回 { toBottom, forceBottom }。
 *  toBottom()  = 只在跟随态下滚（流式内容用）
 *  forceBottom() = 无条件滚并恢复跟随（用户刚发消息用） */
export function follow(el, near = NEAR){
  let stick = true;
  let lastTop = el.scrollTop;
  el.addEventListener('scroll', () => {
    stick = nextStick(stick, {
      scrollTop: el.scrollTop, lastTop,
      scrollHeight: el.scrollHeight, clientHeight: el.clientHeight,
    }, near);
    lastTop = el.scrollTop;
  }, { passive: true });
  const forceBottom = () => { stick = true; el.scrollTop = el.scrollHeight; lastTop = el.scrollTop; };
  return { toBottom: () => { if (stick) forceBottom(); }, forceBottom, isStuck: () => stick };
}
