// 对话回合的纯状态规则；DOM 渲染只消费这里的决定，便于锁定多轮回归。
export function assistantBubbleAfter(current, eventType){
  return ['user_message', 'chat_done', 'error'].includes(eventType) ? null : current;
}

export function durableMessageKey(event){
  return event.client_message_id || event.event_id || '';
}

export async function sendStopRequest(control, timeoutMs=3000){
  const abort = new AbortController();
  let timeoutId;
  const timeout = new Promise(resolve => {
    timeoutId = setTimeout(() => {
      abort.abort();
      resolve({accepted: false, error: '停止请求超时，未确认后端是否已接收'});
    }, timeoutMs);
  });
  const request = Promise.resolve()
    .then(() => control('stop', {}, {signal: abort.signal}))
    .then(response => response && response.ok
      ? {accepted: true, error: ''}
      : {accepted: false, error: (response && response.error) || '停止请求失败'})
    .catch(() => ({
      accepted: false,
      error: '无法连接本机 Companion，停止请求未送达',
    }));
  try {
    return await Promise.race([request, timeout]);
  } finally {
    clearTimeout(timeoutId);
  }
}
