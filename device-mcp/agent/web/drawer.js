// 任务步骤抽屉只展示套件/准备步骤进度；工具调用明细由主对话区展示，避免重复。
const body = () => document.querySelector('#drawerBody');
let suiteRunning = false;
const setupRows = {};
let suiteMeta = null;
let suiteStartedAt = 0;
let suiteTimer = null;

function open(){ document.querySelector('#app').classList.add('drawer-open'); }
const isHistoryReplay = () => document.documentElement.classList.contains('history-replay');

// 运行状态标：running/stopping/interrupted/stopped/done/idle
const STATUS = {
  running: ['运行中', 'running'], stopping: ['正在停止', 'interrupted'],
  interrupted: ['已中断，可继续', 'interrupted'],
  stopped: ['已停止', 'stopped'], done: ['已完成', 'done'], idle: ['待命', 'idle'],
};
export function setStatus(state, detail = ''){
  const el = document.querySelector('#drawerStatus');
  if (!el) return;
  const [txt, cls] = STATUS[state] || STATUS.idle;
  el.textContent = detail ? `${txt} · ${detail}` : txt;
  el.className = 'drawer-status ' + cls;
}
function row(html){
  const d = document.createElement('div');
  d.className = 'step';
  if (isHistoryReplay()) d.classList.add('history-item');
  d.innerHTML = html;
  body().appendChild(d);
  if (!isHistoryReplay()) body().scrollTop = 1e9;
  return d;
}
// 转义 & < > " ' —— 覆盖文本与属性两种上下文，杜绝 innerHTML 注入
function esc(s){ return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

// 每轮结束(chat_done)：套件没在跑就置"待命"
export function resetTurn(){ if (!suiteRunning) setStatus('idle'); }

export function resetDrawerState(){
  stopSuiteTimer();
  body().innerHTML = '';
  suiteRunning = false;
  for (const k in setupRows) delete setupRows[k];
  suiteMeta = null;
  suiteStartedAt = 0;
  setStatus('idle');
}

export function onDrawer(ev){
  if (ev.type === 'suite_start'){
    stopSuiteTimer();
    suiteRunning = true; open(); body().innerHTML = ''; setStatus('running');
    for (const k in setupRows) delete setupRows[k];
    const runId = ev.nested_run_id || ev.run_id || '';
    const restoredStart = Date.parse(ev.ts || '');
    suiteStartedAt = Number.isFinite(restoredStart) ? restoredStart : Date.now();
    suiteMeta = row(`<span class="suite-meta">Run ID: <span class="suite-run-id">${esc(runId || '—')}</span>` +
      `<button class="copy-run-id" type="button" title="复制 Run ID" aria-label="复制 Run ID" data-run-id="${esc(runId)}">⧉</button></span>` +
      `<span class="suite-total"></span>`);
    suiteMeta.classList.add('suite-header');
    const copyButton = suiteMeta.querySelector('.copy-run-id');
    copyButton.disabled = !runId;
    copyButton.onclick = () => copyRunId(copyButton);
    updateSuiteTimer();
    suiteTimer = setInterval(updateSuiteTimer, 1000);
    (ev.cases || []).forEach(c => {
      const r = row(`<b>#${esc(c.seq)}</b><span>${esc(c.operation)}</span><span class="v running" data-v="${esc(c.seq)}">待运行</span>`);
      r.dataset.seq = c.seq;
    });
  } else if (ev.type === 'suite_resumed'){
    suiteRunning = true; open(); setStatus('running');
    if (!suiteTimer){
      updateSuiteTimer();
      suiteTimer = setInterval(updateSuiteTimer, 1000);
    }
  } else if (ev.type === 'turn_interrupted' || ev.type === 'suite_interrupted'){
    // 只表示当前 Agent turn 已停止；Suite/current_step 仍可继续。
    // suite_interrupted 是旧 Run 的回放兼容名，新事件统一为 turn_interrupted。
    suiteRunning = true;
    setStatus('interrupted');
    stopSuiteTimer();
  } else if (ev.type === 'turn_stopped'){
    suiteRunning = Boolean(suiteRunning || ev.suite_id || ev.current_step);
    open(); setStatus('stopped', ev.msg || '可继续'); stopSuiteTimer();
  } else if (ev.type === 'case_start'){
    const v = body().querySelector(`[data-v="${ev.seq}"]`);
    if (v){ v.className = 'v running'; v.textContent = '运行中'; }
  } else if (ev.type === 'case_result'){
    const v = body().querySelector(`[data-v="${ev.seq}"]`);
    if (v){ v.className = 'v ' + ev.verdict; v.textContent = `${ev.verdict} · ${formatDuration(ev.duration_s)}`; }
  } else if (ev.type === 'setup_step'){
    const r = row(`<b>准备${Number(ev.index) + 1}</b><span>${esc(ev.operation)}</span><span class="v running">运行中</span>`);
    setupRows[ev.index] = r;
  } else if (ev.type === 'setup_result'){
    const r = setupRows[ev.index];
    const v = r && r.querySelector('.v');
    if (v){ v.className = 'v ' + ev.verdict; v.textContent = `${ev.verdict} · ${formatDuration(ev.duration_s)}`; }
  } else if (ev.type === 'suite_done'){
    suiteRunning = false;
    const completed = ev.status === 'completed' || !ev.status;
    const verdict = ev.verdict || (ev.summary || {}).verdict || '';
    const verdictText = {pass: '通过', fail: '失败', blocked: '阻塞', needs_review: '待复核'}[verdict] || verdict;
    setStatus(completed ? 'done' : 'stopped', completed ? verdictText : '');
    stopSuiteTimer();
    const total = suiteMeta && suiteMeta.querySelector('.suite-total');
    if (total) total.textContent = `总时间：${formatDuration((ev.summary || {}).total_duration_s)}`;
  }
}

function updateSuiteTimer(){
  const total = suiteMeta && suiteMeta.querySelector('.suite-total');
  if (total) total.textContent = `总时间：${formatDuration((Date.now() - suiteStartedAt) / 1000)}`;
}

function stopSuiteTimer(){
  if (suiteTimer !== null) clearInterval(suiteTimer);
  suiteTimer = null;
}

async function copyRunId(button){
  const runId = button.dataset.runId || '';
  if (!runId) return;
  try {
    await navigator.clipboard.writeText(runId);
  } catch (e) {
    const input = document.createElement('textarea');
    input.value = runId; input.style.position = 'fixed'; input.style.opacity = '0';
    document.body.appendChild(input); input.select(); document.execCommand('copy'); input.remove();
  }
  button.textContent = '✓';
  button.title = '已复制';
  setTimeout(() => { button.textContent = '⧉'; button.title = '复制 Run ID'; }, 1200);
}

function formatDuration(value){
  if (value === null || value === undefined || value === '') return '—';
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return '—';
  const seconds = Math.max(0, Math.round(parsed));
  if (seconds < 60) return `${seconds}秒`;
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return rest ? `${minutes}分${rest}秒` : `${minutes}分`;
}

document.querySelector('#drawerClose').onclick = () => document.querySelector('#app').classList.remove('drawer-open');
// 步骤抽屉可重新唤起：收起后用顶栏"☰ 步骤"重开(不必重跑任务)
document.querySelector('#drawerToggle').onclick = () => document.querySelector('#app').classList.toggle('drawer-open');
