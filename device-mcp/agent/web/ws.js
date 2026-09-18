// WS 连接 + 事件分发。对外暴露 api(HTTP 动作)。
import { onChat, resetChatState, restoreTurnState } from './chat.js';
import { onDrawer, resetTurn, resetDrawerState } from './drawer.js';
import { onScreen, onDeviceStatus } from './device.js';
import { onProviderStatus, onSettingsDeviceStatus } from './settings.js';

const J = (u, body, options={}) => fetch(u, {
  method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body), ...options,
});
export const api = {
  send: (t, file, effort, clientMessageId, turnMode='') => J('/message', {text: t, ...(file ? {file} : {}), ...(effort ? {effort} : {}), ...(clientMessageId ? {client_message_id: clientMessageId} : {}), ...(turnMode ? {turn_mode: turnMode} : {})}),
  respond: a => J('/respond', {answer: a}),
  respondPermission: allow => J('/permission/respond', {allow}),
  control: (a, extra={}, options={}) => J('/control', {action: a, ...extra}, options)
    .then(async r => ({ok: r.ok, ...(await r.json())})),
  upload: f => { const fd = new FormData(); fd.append('file', f); return fetch('/upload', {method: 'POST', body: fd}).then(r => r.json()); },
  uploadTencentSheet: (url, token) => J('/upload/tencent-sheet', {url, token})
    .then(async r => ({ok: r.ok, ...(await r.json())})),
  status: () => fetch('/api/status').then(r => r.json()),
  snapshot: (after=0) => fetch(`/api/snapshot?after=${encodeURIComponent(after || 0)}`).then(r => r.json()),
  setProviderKey: (key, baseUrl='', model='', remember=false) => J('/api/provider-key', {
    api_key: key,
    anthropic_base_url: baseUrl,
    model,
    remember,
  }).then(async r => ({ok: r.ok, ...(await r.json())})),
  settingsPlugins: () => fetch('/api/settings/plugins').then(r => r.json()),
  importPluginConfig: (pluginId, f) => {
    const fd = new FormData(); fd.append('file', f);
    return fetch(`/api/settings/plugins/${encodeURIComponent(pluginId)}/import`, {method: 'POST', body: fd})
      .then(async r => ({ok: r.ok, ...(await r.json())}));
  },
  syncPluginConfig: (pluginId, url, token, rememberToken=false) => J(
    `/api/settings/plugins/${encodeURIComponent(pluginId)}/sync`, {
      url, token, remember_token: rememberToken,
    }
  ).then(async r => ({ok: r.ok, ...(await r.json())})),
  refreshPluginConfig: pluginId => J(
    `/api/settings/plugins/${encodeURIComponent(pluginId)}/refresh`, {}
  ).then(async r => ({ok: r.ok, ...(await r.json())})),
  selectDevice: serial => J('/api/device/select', {serial}).then(async r => ({ok: r.ok, ...(await r.json())})),
};

let ws;
let retryMs = 500;
let heartbeat;
let lastEventSeq = 0;
const seenEvents = new Set();
let hasOpenedWebSocket = false;

function onAppVersion(version){
  const el = document.querySelector('#appVersion');
  const value = String(version || '').trim();
  el.textContent = value ? `v${value}` : 'v—';
  el.title = value ? `当前本地服务版本：${value}` : '无法读取 Companion 版本';
}

function setCompanionConnection(state){
  const status = document.querySelector('#companionStatus');
  const label = document.querySelector('#companionStatusText');
  const activity = document.querySelector('#typingText');
  if (status) status.dataset.state = state;
  if (label) label.textContent = state === 'online' ? '本地服务在线'
    : (state === 'offline' ? '本地服务已断开' : '本地服务连接中');
  if (activity) activity.textContent = state === 'offline' ? '连接已断开，执行状态未知'
    : (state === 'checking' ? '正在连接本地服务' : '思考中');
}

function connect(after=lastEventSeq){
  setCompanionConnection('checking');
  const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${scheme}://${location.host}/ws?after=${encodeURIComponent(after || 0)}`);
  ws.onopen = async () => {
    const wasReconnect = hasOpenedWebSocket;
    hasOpenedWebSocket = true;
    setCompanionConnection('online');
    retryMs = 500;
    clearInterval(heartbeat);
    heartbeat = setInterval(() => { try { ws.send('p'); } catch (e) {} }, 25000);
    if (wasReconnect){
      try {
        await restoreSnapshot(lastEventSeq);
      } catch (_error) {
        setCompanionConnection('offline');
      }
    }
  };
  ws.onmessage = onMessage;
  ws.onclose = () => {
    clearInterval(heartbeat);
    setCompanionConnection('offline');
    setTimeout(connect, retryMs);
    retryMs = Math.min(retryMs * 2, 10000);
  };
}
function onMessage(ev){
  const m = JSON.parse(ev.data);
  dispatch(m);
}
function dispatch(m){
  // Historical replay compatibility only; new Runs never emit provider pause events.
  if (m.type === 'provider_unhealthy' || m.type === 'provider_paused') {
    m = {...m, type: 'turn_stopped', status: 'stopped', reason: m.reason || 'provider_unavailable'};
  }
  if (m.type === 'provider_resumed') return;
  if (m.event_id){
    if (seenEvents.has(m.event_id)) return;
    seenEvents.add(m.event_id);
  }
  if (m.event_seq) lastEventSeq = Math.max(lastEventSeq, Number(m.event_seq) || 0);
  if (m.type === 'screen') { onScreen(m); return; }
  if (m.type === 'device_status') { onDeviceStatus(m.device); onSettingsDeviceStatus(m.device); return; }
  if (m.type === 'interactive_status') {
    restoreTurnState(Boolean(m.busy), m.turn_state, m.active_suite || {});
    return;
  }
  if (['suite_start', 'suite_resumed', 'turn_interrupted', 'suite_interrupted', 'turn_stopped', 'setup_step', 'setup_result', 'case_start', 'case_result', 'suite_done'].includes(m.type)) onDrawer(m);
  if (m.type === 'chat_done') resetTurn();
  onChat(m);   // chat 处理 chat_delta/chat_done/tool_call/download_ready/need_human/error
}

async function restoreSnapshot(after=0){
  const snap = await api.snapshot(after);
  replayHistory(snap.events || []);
  lastEventSeq = Math.max(lastEventSeq, Number(snap.last_event_seq) || 0);
  restoreTurnState(Boolean(snap.busy), snap.turn_state, snap.active_suite || {});
  onDeviceStatus(snap.device);
  onSettingsDeviceStatus(snap.device);
  return snap;
}

function replayHistory(events){
  const root = document.documentElement;
  root.classList.add('history-replay');
  try {
    events.forEach(dispatch);
  } finally {
    root.classList.remove('history-replay');
    const chat = document.querySelector('#chat');
    const drawer = document.querySelector('#drawerBody');
    if (chat) chat.scrollTop = chat.scrollHeight;
    if (drawer) drawer.scrollTop = drawer.scrollHeight;
  }
}

async function bootstrap(){
  resetChatState();
  resetDrawerState();
  try {
    await restoreSnapshot(0);
  } catch (e) {
    restoreTurnState(false);
  }
  try {
    const s = await api.status();
    onAppVersion(s.app_version);
    onDeviceStatus(s.device);
    onSettingsDeviceStatus(s.device);
    onProviderStatus(s.provider);
    restoreTurnState(Boolean((s.interactive || {}).busy), (s.interactive || {}).turn_state,
      (s.interactive || {}).active_suite || {});
  } catch (e) {
    onDeviceStatus({status: 'adb_error', error: '无法读取 Companion 状态'});
  }
  setCompanionConnection('checking');
  connect(lastEventSeq);
}
bootstrap();
