// 真机预览 + ADB 状态。状态和帧分开处理，旧画面不会伪装成设备仍在线。
import { api } from './ws.js';

const STATUS_TEXT = {
  checking: '正在检查设备',
  ready: '设备已就绪',
  no_device: '未连接设备',
  unauthorized: '等待手机授权',
  offline: '设备离线',
  multiple_devices: '请选择一台设备',
  disconnected: '所选设备已断开',
  adb_missing: 'ADB 未安装',
  adb_error: 'ADB 异常',
};

const HELP = {
  checking: '正在检查 USB/ADB…',
  ready: '',
  no_device: '请用可传数据的 USB 线连接手机，并开启 USB 调试。',
  unauthorized: '请解锁手机，在“允许 USB 调试”弹窗中确认授权。',
  offline: '设备处于 offline。请拔插 USB；不会自动重放设备动作。',
  multiple_devices: '检测到多台手机，请点击顶栏设备状态并明确选择。',
  disconnected: '原设备已断开。重新连接同一台设备后再人工确认继续。',
  adb_missing: '未找到 ADB，请修复或重新安装 Windows Companion。',
  adb_error: 'ADB 检查失败，请查看 Companion 诊断。',
};

let lastFrameAt = 0;
let currentDevice = null;

function esc(s){ return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

export function onScreen(ev){
  document.querySelector('#screen').src = `data:${ev.mime};base64,${ev.data}`;
  lastFrameAt = Date.now();
  document.querySelector('#frameAge').textContent = '刚刚更新';
  if (currentDevice && currentDevice.status === 'ready'){
    document.querySelector('#deviceOverlay').classList.add('hidden');
  }
}

export function onDeviceStatus(device){
  currentDevice = device || {status: 'checking', devices: [], detail: {}};
  const state = currentDevice.status || 'checking';
  const status = document.querySelector('#deviceStatus');
  status.dataset.state = state;
  document.querySelector('#deviceStatusText').textContent = STATUS_TEXT[state] || state;

  const detail = currentDevice.detail || {};
  const model = detail.model || ((currentDevice.devices || []).find(d => d.serial === currentDevice.selected_serial) || {}).model || '';
  const geometry = detail.width && detail.height ? ` · ${detail.width}×${detail.height}` : '';
  document.querySelector('#deviceMeta').textContent = model ? `${model}${geometry}` : '请连接一台 Android 手机';

  const overlay = document.querySelector('#deviceOverlay');
  overlay.textContent = currentDevice.error || HELP[state] || '设备状态异常';
  overlay.classList.toggle('hidden', state === 'ready' && lastFrameAt > 0);
  document.querySelector('#deviceHelp').textContent = HELP[state] || '请选择一台已就绪的测试手机。';
  renderDeviceList();
}

function renderDeviceList(){
  const list = document.querySelector('#deviceList');
  if (!list || !currentDevice) return;
  const devices = currentDevice.devices || [];
  if (!devices.length){
    list.innerHTML = `<div class="device-choice empty-choice">${esc(HELP[currentDevice.status] || '没有发现设备')}</div>`;
    return;
  }
  list.innerHTML = '';
  devices.forEach(d => {
    const row = document.createElement('button');
    row.className = 'device-choice';
    row.disabled = d.state !== 'device';
    const label = (d.model || d.device || d.serial).replaceAll('_', ' ');
    row.innerHTML = `<span><b>${esc(label)}</b><small>${esc(d.serial)}</small></span><em>${esc(d.state)}</em>`;
    row.onclick = async () => {
      const resp = await api.selectDevice(d.serial);
      if (resp.ok){
        onDeviceStatus(resp.device);
        document.querySelector('#deviceModal').classList.add('hidden');
      } else {
        document.querySelector('#deviceHelp').textContent = resp.error || '设备切换失败';
      }
    };
    list.appendChild(row);
  });
}

document.querySelector('#deviceStatus').onclick = () => {
  renderDeviceList();
  document.querySelector('#deviceModal').classList.remove('hidden');
};
document.querySelector('#deviceClose').onclick = () => document.querySelector('#deviceModal').classList.add('hidden');

setInterval(() => {
  const age = lastFrameAt ? Math.round((Date.now() - lastFrameAt) / 1000) : null;
  const label = document.querySelector('#frameAge');
  if (age == null){ label.textContent = '等待首帧'; return; }
  label.textContent = age < 2 ? '刚刚更新' : `${age} 秒前`;
  if (age > 5 && currentDevice && currentDevice.status === 'ready'){
    const overlay = document.querySelector('#deviceOverlay');
    overlay.textContent = '画面已超过 5 秒未更新，请以设备状态为准。';
    overlay.classList.remove('hidden');
  }
}, 1000);
