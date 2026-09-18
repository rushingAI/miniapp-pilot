// 对话渲染：流式气泡、动作行、上传、下载卡、need_human 浮层。
import { api } from './ws.js';
import { setStatus } from './drawer.js';   // 停止时同步抽屉状态标(drawer 不 import 本模块，无循环)
import { follow } from './scroll.js';
import { assistantBubbleAfter, durableMessageKey, sendStopRequest } from './turns.js';
import { appendDismissibleNotice } from './notice.js';
import { composerAction } from './composer_state.js';
import { foldAgentOutput, foldChatDelta } from './message_fold.js';

const chat = () => document.querySelector('#chat');
let cur = null;   // 当前 assistant 气泡
const toolRows = {};   // tool_event: id → DOM 行(原地更新 running→ok/fail,Claude Code 式)
const userRows = {};   // client_message_id → 用户消息节点；乐观显示与 durable 回放去重
let turnState = 'idle';
let activeSuite = {};

const STATUS_ICON = { running: '⏳', ok: '✓', fail: '✗', interrupted: '■' };
function esc(s){ return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

// 一次工具调用一行：▸ 名 参数 [图标] 详情；有 cmd 则可折叠看底层命令。按 id 复用同一行(心跳原地刷新)。
function renderToolEvent(ev){
  if (!cur) cur = bubble('assistant');
  let row = toolRows[ev.id];
  if (!row){
    row = document.createElement('div');
    row.className = 'tool-event';
    row.innerHTML = '<div class="te-head"></div><pre class="te-cmd hidden"></pre>';
    row.querySelector('.te-head').onclick = () => row.querySelector('.te-cmd').classList.toggle('hidden');
    cur.appendChild(row);
    toolRows[ev.id] = row;
    row._d = {};   // 保留 name/args/cmd,后续 ok/fail 更新可能只带 status+detail
  }
  const d = row._d;
  if (ev.name) d.name = ev.name;
  if (ev.args) d.args = ev.args;
  if (ev.cmd) d.cmd = ev.cmd;
  row.dataset.status = ev.status;
  const icon = STATUS_ICON[ev.status] || '·';
  const seqTag = ev.seq ? `<b>#${esc(ev.seq)}</b> ` : '';
  const detail = ev.detail ? ` — <span class="te-detail">${esc(ev.detail)}</span>` : '';
  row.querySelector('.te-head').innerHTML =
    `${seqTag}<span class="te-icon">${icon}</span> <b>${esc(d.name)}</b> <span class="te-args">${esc(d.args || '')}</span>${detail}`;
  const cmdEl = row.querySelector('.te-cmd');
  if (d.cmd){ cmdEl.textContent = d.cmd; } else { cmdEl.classList.add('hidden'); }
  toBottom();
}

// 流式跟随底部。状态机在 scroll.js（纯函数可单测）：贴底→跟随，往上翻→脱离，滚回底部→自动恢复。
// ⚠️ 别改回"append 后测 gap 再决定滚不滚"——那样一个高过阈值的块就会永久锁死跟随，见 scroll.js 注释。
const isHistoryReplay = () => document.documentElement.classList.contains('history-replay');
const scrollFollower = follow(chat());
const toBottom = () => { if (!isHistoryReplay()) scrollFollower.toBottom(); };
const forceBottom = () => { if (!isHistoryReplay()) scrollFollower.forceBottom(); };

function bubble(role){
  const empty = chat().querySelector('.chat-empty');
  if (empty) empty.remove();
  const d = document.createElement('div');
  d.className = 'msg ' + role;
  if (isHistoryReplay()) d.classList.add('history-item');
  chat().appendChild(d);
  toBottom();
  return d;
}
function appendAgentOutput(container, text, {className='', prefix='', output=null}={}){
  output = output || foldAgentOutput(text);
  if (!output.folded){
    const row = document.createElement('div');
    if (className) row.className = className;
    row.textContent = prefix + output.text;
    container.appendChild(row);
    return row;
  }
  const details = document.createElement('details');
  details.className = `agent-output-fold ${className}`.trim();
  const summary = document.createElement('summary');
  const preview = document.createElement('span');
  preview.className = 'agent-output-preview';
  preview.textContent = prefix + output.preview;
  const toggle = document.createElement('span');
  toggle.className = 'agent-output-toggle';
  summary.appendChild(preview);
  summary.appendChild(toggle);
  const full = document.createElement('div');
  full.className = 'agent-output-full';
  full.textContent = prefix + output.text;
  details.appendChild(summary);
  details.appendChild(full);
  container.appendChild(details);
  return details;
}
function userMsg(t, file, messageId=''){
  if (messageId && userRows[messageId]) return userRows[messageId];
  cur = assistantBubbleAfter(cur, 'user_message'); // 后续 Agent 回复必须创建新气泡，不能续写上一轮
  for (const k in toolRows) delete toolRows[k];
  const d = bubble('user');
  if (messageId){ d.dataset.messageId = messageId; userRows[messageId] = d; }
  if (t){ const p = document.createElement('div'); p.textContent = t; d.appendChild(p); }
  if (file){ const a = document.createElement('div'); a.className = 'attach'; a.textContent = '📎 ' + file.filename; d.appendChild(a); }
  if (!isHistoryReplay()){
    forceBottom();   // 用户刚发消息:无条件滚到底,必须看到自己发的(不受"贴底才跟随"限制)
    requestAnimationFrame(forceBottom); // 等本轮 DOM 高度落定后再锚一次，治第二轮消息未随回复上移
  }
  return d;
}

export function resetChatState(){
  chat().innerHTML = '<div class="chat-empty"><div class="empty-mark" aria-hidden="true"></div><p class="empty-title">用自然语言测试你的小程序</p><p class="empty-sub">连接 Android 设备并配置模型后开始操作，或上传 Excel 用例执行回归测试。</p></div>';
  cur = null;
  for (const k in toolRows) delete toolRows[k];
  for (const k in userRows) delete userRows[k];
}

export function restoreTurnState(busy, state='idle', suite={}){
  turnState = busy ? 'running' : (state || 'idle');
  activeSuite = suite || {};
  updateComposer();
  if (busy) showTyping(); else hideTyping();
}

let pendingFile = null;   // 已上传、待随下一条消息发出的文件
let pendingSuiteChoice = null;
function renderChip(){
  const box = document.querySelector('#attachments');
  box.textContent = '';
  if (!pendingFile){ box.classList.add('hidden'); return; }
  box.classList.remove('hidden');
  const chip = document.createElement('span'); chip.className = 'chip';
  const name = document.createElement('span'); name.textContent = '📎 ' + pendingFile.filename;
  const x = document.createElement('span'); x.className = 'x'; x.textContent = '✕';
  x.onclick = () => { pendingFile = null; renderChip(); };
  chip.appendChild(name); chip.appendChild(x); box.appendChild(chip);
}
// 方案C：运行中把 ↑ 发送键就地切成 ⏹ 停止键(同一个键,按 dataset.mode 切换语义)。
// 这样发送后中断入口始终可见,不再依赖右侧抽屉是否展开(原 bug:普通对话/单步时抽屉不开→⏸⏹ 藏起来点不到)。
function setSendMode(mode, disabled=false){   // 'send'=↑发送 | 'stop'=⏹停止
  const b = document.querySelector('#sendBtn');
  b.dataset.mode = mode;
  b.textContent = mode === 'stop' ? '⏹' : '↑';
  b.title = mode === 'stop' ? '停止当前执行' : (turnState === 'running' ? '追加发言' : '发送');
  b.disabled = Boolean(disabled);
}
function updateComposer(){
  const input = document.querySelector('#msgInput');
  const action = composerAction(turnState, input.value);
  setSendMode(action.mode, action.disabled);
  input.placeholder = turnState === 'running'
    ? '输入内容后按钮变为发送；清空内容后按钮变为停止'
    : '发消息，或上传文件后加说明再发…';
}
function lock(){ turnState = 'running'; updateComposer(); }
function unlock(state='idle'){ turnState = state; updateComposer(); }
function showTyping(){ document.querySelector('#typing').classList.remove('hidden'); }
function hideTyping(){ document.querySelector('#typing').classList.add('hidden'); }
// 停止当前轮：只 interrupt Agent turn；开放 Suite/current_step 保留，下一条消息可继续。抽屉里的 ⏹ 与此共用同一后端。
export async function stopTurn(){
  setStatus('stopping');
  const result = await sendStopRequest(api.control);
  if (!result.accepted){
    setStatus('running', '停止请求未确认');
    const d = bubble('assistant');
    d.style.color = 'var(--amber)';
    d.textContent = '⚠ ' + result.error;
    toBottom();
  }
  // 保持停止态，直到后端真正发出 chat_done；避免提前换机撞 409。
  return result.accepted;
}

export function onChat(ev){
  if (ev.type === 'suite_start' || ev.type === 'suite_resumed'){
    activeSuite = {
      ...(activeSuite || {}), suite_id: ev.suite_id || activeSuite.suite_id,
      current_step: ev.current_step || activeSuite.current_step || null,
    };
  } else if (ev.type === 'setup_step' || ev.type === 'case_start'){
    activeSuite = {...(activeSuite || {}), current_step: {
      step_id: ev.step_id || (ev.type === 'case_start' ? `case-${ev.seq}` : `setup-${ev.index}`),
      operation: ev.operation || '',
    }};
  } else if (ev.type === 'suite_done' || ev.type === 'suite_interrupted'){
    activeSuite = {};
  }
  if (ev.type === 'user_message' || (ev.type === 'turn_input' && ev.status === 'accepted')){
    userMsg(ev.text || '', ev.file || null, durableMessageKey(ev));
    if (ev.type === 'turn_input'){
      const row = userRows[durableMessageKey(ev)];
      if (row) row.dataset.inputStatus = 'submitted';
    }
  } else if (ev.type === 'chat_delta'){
    if (!cur) cur = bubble('assistant');
    appendAgentOutput(cur, ev.text, {output: foldChatDelta(ev)});
    toBottom();
  } else if (ev.type === 'chat_done'){
    cur = assistantBubbleAfter(cur, 'chat_done'); hideTyping();
    unlock(turnState === 'stopped' ? 'stopped' : 'idle');
    for (const k in toolRows) delete toolRows[k];   // 新一轮工具行不复用旧 id
  } else if (ev.type === 'thinking'){
    if (!cur) cur = bubble('assistant');
    let det = cur.querySelector('details.thinking');
    if (!det){
      det = document.createElement('details'); det.className = 'thinking';
      const s = document.createElement('summary'); s.textContent = '💭 思考过程'; det.appendChild(s);
      const bd = document.createElement('div'); bd.className = 'think-body'; det.appendChild(bd);
      cur.appendChild(det);
    }
    const p = document.createElement('div'); p.textContent = ev.text;
    det.querySelector('.think-body').appendChild(p);
    toBottom();
  } else if (ev.type === 'tool_call'){
    if (!cur) cur = bubble('assistant');
    const a = document.createElement('div');
    a.className = 'actions';
    a.textContent = `▸ ${ev.name} ${ev.summary || ''}`.trim();
    cur.appendChild(a);
    toBottom();
  } else if (ev.type === 'tool_event'){   // Claude Code 式工具行:按 id 原地更新 running→ok/fail
    renderToolEvent(ev);
  } else if (ev.type === 'agent_text'){   // 套件内逐条用例的实时思考/动作叙述
    if (!cur) cur = bubble('assistant');
    appendAgentOutput(cur, ev.text, {
      className: 'agent-text', prefix: ev.seq ? `用例${ev.seq}· ` : '',
    });
    toBottom();
  } else if (ev.type === 'download_ready'){
    const d = bubble('assistant');
    const c = document.createElement('div');
    c.className = 'dl-card';
    c.textContent = '⬇ 下载文件 ' + ev.filename;
    c.onclick = () => location = '/download?run_id=' + encodeURIComponent(ev.nested_run_id || ev.run_id || '')
      + (ev.filename ? '&name=' + encodeURIComponent(ev.filename) : '');
    d.appendChild(c);
    toBottom();   // bubble() 那次是空气泡高度，卡片填完再滚一次才真到底
  } else if (ev.type === 'need_human'){
    showHuman(ev.question);
  } else if (ev.type === 'human_resolved'){
    hideHuman();
  } else if (ev.type === 'tool_permission'){
    showPermission(ev);
  } else if (ev.type === 'permission_resolved'){
    hidePermission();
  } else if (ev.type === 'session_divider' || ev.type === 'architecture_changed'){
    cur = assistantBubbleAfter(cur, 'chat_done');
    const d = bubble('assistant');
    d.classList.add('session-divider');
    d.textContent = ev.text || '新会话开始';
    cur = null;
  } else if (ev.type === 'turn_stopped'){
    turnState = 'stopped';
    if (ev.suite_id || ev.current_step) activeSuite = {
      ...(activeSuite || {}), suite_id: ev.suite_id || activeSuite.suite_id,
      current_step: ev.current_step || activeSuite.current_step,
    };
    const d = bubble('assistant');
    d.classList.add('warning-message');
    appendDismissibleNotice(document, d, ev.msg || '当前执行已停止，现场已保留。');
    hideTyping(); updateComposer(); toBottom();
  } else if (ev.type === 'warn' || ev.type === 'silence_warning'){
    // 非致命提醒：本轮还在跑。刻意不动 cur/hideTyping/unlock——
    // 解锁发送键会让用户以为回合结束，再发就撞 /message 的 409 并发护栏。
    const d = bubble('assistant');
    d.classList.add('warning-message');
    appendDismissibleNotice(document, d, ev.msg);
    toBottom();
  } else if (ev.type === 'error'){
    const d = bubble('assistant');
    d.style.color = 'var(--fail)';
    d.textContent = '错误: ' + ev.msg;
    cur = assistantBubbleAfter(cur, 'error'); hideTyping(); unlock();
  }
}

function showPermission(ev){
  const modal = document.querySelector('#permissionModal');
  modal.dataset.requestId = ev.request_id || '';
  document.querySelector('#permissionTitle').textContent = ev.title || '允许执行这次 Bash？';
  document.querySelector('#permissionDescription').textContent = ev.description || '此批准仅对当前工具调用生效。';
  modal.classList.remove('hidden');
}
function hidePermission(){ document.querySelector('#permissionModal').classList.add('hidden'); }
document.querySelector('#permissionAllow').onclick = async () => { await api.respondPermission(true); hidePermission(); };
document.querySelector('#permissionDeny').onclick = async () => { await api.respondPermission(false); hidePermission(); };

function showSuiteChoice(activeSuite){
  const modal = document.querySelector('#suiteChoiceModal');
  const step = (activeSuite || {}).current_step || {};
  const label = step.step_id ? `${step.step_id}${step.operation ? `：${step.operation}` : ''}` : '当前未完成步骤';
  document.querySelector('#suiteChoiceDescription').textContent = `旧测试仍可恢复（${label}）。请选择这条消息的用途。`;
  modal.classList.remove('hidden');
  hideTyping(); unlock();
}
function hideSuiteChoice(){ document.querySelector('#suiteChoiceModal').classList.add('hidden'); }

async function submitMessage(request, turnMode=''){
  if (!request.optimistic){
    request.optimistic = userMsg(request.text, request.file, request.messageId);
  }
  const resp = await api.send(request.text, request.file, request.effort, request.messageId, turnMode);
  if (resp && !resp.ok){
    let payload = {};
    try { payload = await resp.json(); } catch (e) {}
    if (payload.code === 'active_suite_choice_required'){
      pendingSuiteChoice = {...request, optimistic: null};
      activeSuite = payload.active_suite || activeSuite;
      showSuiteChoice(payload.active_suite || {});
      return;
    }
    const msg = payload.error || '发送失败';
    if (request.wasBusy){
      request.optimistic.dataset.inputStatus = 'rejected';
      const d = bubble('assistant'); d.style.color = 'var(--amber)'; d.textContent = '⚠ ' + msg;
      lock(); showTyping(); forceBottom();
    } else {
      onChat({ type: 'error', msg });
    }
    return;
  }
  pendingSuiteChoice = null;
}

async function doSend(){
  const i = document.querySelector('#msgInput');
  const t = i.value.trim();
  if (!t && !pendingFile) return;
  const file = pendingFile;
  const wasBusy = turnState === 'running';
  const messageId = (globalThis.crypto && crypto.randomUUID) ? crypto.randomUUID() : `m-${Date.now()}-${Math.random()}`;
  const effort = (document.querySelector('#effortSel') || {}).value || ''; // Kimi 推理模式(none/low/high/max)
  const request = {text: t, file, effort, messageId, optimistic: null, wasBusy};
  if (!wasBusy && activeSuite && activeSuite.suite_id){
    pendingSuiteChoice = request;
    showSuiteChoice(activeSuite);
    return;
  }
  request.optimistic = userMsg(t, file, messageId);
  i.value = ''; pendingFile = null; renderChip(); lock(); showTyping(); updateComposer();
  await submitMessage(request);
}
document.querySelectorAll('#suiteChoiceModal [data-turn-mode]').forEach(button => {
  button.onclick = async () => {
    const request = pendingSuiteChoice;
    if (!request) return;
    request.optimistic = userMsg(request.text, request.file, request.messageId);
    document.querySelector('#msgInput').value = '';
    pendingFile = null; renderChip();
    hideSuiteChoice(); lock(); showTyping();
    await submitMessage(request, button.dataset.turnMode || '');
  };
});
document.querySelector('#sendBtn').onclick = () => {
  // 按当前语义分发：running 且输入为空时停止；其余状态发送/追加。
  if (document.querySelector('#sendBtn').dataset.mode === 'stop') stopTurn();
  else doSend();
};
document.querySelector('#msgInput').addEventListener('keydown', e => {
  // 运行(停止)态时 Enter 不触发停止——避免打字准备下一条时误停；中断只走点击 ⏹
  // 输入法用 Enter 确认候选词时不能发送；keyCode 229 兼容部分浏览器的 IME 事件。
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && e.keyCode !== 229){
    e.preventDefault(); doSend();
  }
});
document.querySelector('#msgInput').addEventListener('input', updateComposer);
document.querySelector('#fileInput').onchange = async e => {
  const f = e.target.files[0]; if (!f) return;
  const r = await api.upload(f);
  if (r.error){ const d = bubble('assistant'); d.style.color = 'var(--fail)'; d.textContent = '上传失败: ' + r.error; e.target.value = ''; return; }
  pendingFile = { filename: r.filename, sheets: r.sheets || [] };  // 挂起，等下一条消息一起发
  renderChip();
  e.target.value = '';
};

const tencentSheetModal = document.querySelector('#tencentSheetModal');
const attachmentMenu = document.querySelector('#attachmentMenu');
const attachmentButton = document.querySelector('#attachmentBtn');
function closeAttachmentMenu(){
  attachmentMenu.classList.add('hidden');
  attachmentButton.setAttribute('aria-expanded', 'false');
}
attachmentButton.onclick = e => {
  e.stopPropagation();
  const willOpen = attachmentMenu.classList.contains('hidden');
  attachmentMenu.classList.toggle('hidden', !willOpen);
  attachmentButton.setAttribute('aria-expanded', String(willOpen));
};
document.querySelector('#localFileOption').onclick = () => {
  closeAttachmentMenu();
  document.querySelector('#fileInput').click();
};
document.addEventListener('click', e => {
  if (!document.querySelector('#attachmentPicker').contains(e.target)) closeAttachmentMenu();
});
function closeTencentSheetModal(){
  tencentSheetModal.classList.add('hidden');
  document.querySelector('#tencentSheetToken').value = '';
  document.querySelector('#tencentSheetError').classList.add('hidden');
}
document.querySelector('#tencentSheetBtn').onclick = () => {
  closeAttachmentMenu();
  tencentSheetModal.classList.remove('hidden');
  document.querySelector('#tencentSheetUrl').focus();
};
document.querySelector('#tencentSheetCancel').onclick = closeTencentSheetModal;
document.querySelector('#tencentSheetSubmit').onclick = async () => {
  const button = document.querySelector('#tencentSheetSubmit');
  const error = document.querySelector('#tencentSheetError');
  const url = document.querySelector('#tencentSheetUrl').value.trim();
  const token = document.querySelector('#tencentSheetToken').value.trim();
  error.classList.add('hidden');
  if (!url || !token){
    error.textContent = '请输入腾讯表格链接和 MCP Token';
    error.classList.remove('hidden');
    return;
  }
  button.disabled = true; button.textContent = '正在读取…';
  try {
    const result = await api.uploadTencentSheet(url, token);
    document.querySelector('#tencentSheetToken').value = '';
    if (!result.ok){
      error.textContent = result.error || '腾讯表格读取失败';
      error.classList.remove('hidden');
      return;
    }
    pendingFile = {filename: result.filename, sheets: result.sheets || [], source: result.source || null};
    renderChip();
    closeTencentSheetModal();
  } catch (e) {
    document.querySelector('#tencentSheetToken').value = '';
    error.textContent = '无法连接本机 Companion';
    error.classList.remove('hidden');
  } finally {
    button.disabled = false; button.textContent = '读取并附加';
  }
};

function showHuman(q){
  document.querySelector('#humanQ').textContent = q;
  document.querySelector('#humanModal').classList.remove('hidden');
  document.querySelector('#humanInput').focus();
}
function hideHuman(){
  document.querySelector('#humanModal').classList.add('hidden');
  document.querySelector('#humanInput').value = '';
}
document.querySelector('#humanSubmit').onclick = async () => {
  const v = document.querySelector('#humanInput').value;
  await api.respond(v);
  hideHuman();
};
