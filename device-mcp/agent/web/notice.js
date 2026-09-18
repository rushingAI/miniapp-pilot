// 对话内的非致命提醒：关闭只移除当前节点，不影响任务或后续新提醒。
export function appendDismissibleNotice(doc, container, message){
  const notice = doc.createElement('div');
  notice.className = 'dismissible-notice';

  const text = doc.createElement('span');
  text.textContent = '⚠ ' + message;

  const close = doc.createElement('button');
  close.className = 'notice-close';
  close.textContent = '×';
  close.setAttribute('type', 'button');
  close.setAttribute('aria-label', '关闭提醒');
  close.setAttribute('title', '关闭提醒');
  close.onclick = () => container.remove();

  notice.appendChild(text);
  notice.appendChild(close);
  container.appendChild(notice);
  return notice;
}
