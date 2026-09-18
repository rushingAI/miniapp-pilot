// 通用设置中心：基础设置由核心提供，可选能力由后端插件描述动态渲染。
import { api } from './ws.js';

let activePlugin = null;
let pluginConfigFile = null;
let providerState = {configured: false, storage: 'memory', model: 'k3[1m]'};

const settingsModal = document.querySelector('#settingsModal');
const pluginModal = document.querySelector('#pluginConfigModal');
const pluginInput = document.querySelector('#pluginConfigInput');
const pluginName = document.querySelector('#pluginConfigName');
const pluginResult = document.querySelector('#pluginConfigResult');
const pluginSubmit = document.querySelector('#pluginConfigSubmit');
const pluginLinkImport = document.querySelector('#pluginLinkImport');
const pluginLinkUrl = document.querySelector('#pluginLinkUrl');
const pluginLinkToken = document.querySelector('#pluginLinkToken');
const pluginLinkSubmit = document.querySelector('#pluginLinkSubmit');
const pluginRememberToken = document.querySelector('#pluginRememberToken');
const pluginLinkSource = document.querySelector('#pluginLinkSource');
const pluginLinkSourceUrl = document.querySelector('#pluginLinkSourceUrl');
const pluginLinkSyncedAt = document.querySelector('#pluginLinkSyncedAt');
const pluginLinkRefresh = document.querySelector('#pluginLinkRefresh');

export function onProviderStatus(provider){
  const p = provider || {configured: false};
  providerState = {...providerState, ...p};
  const selectedModel = p.model || 'k3[1m]';
  const modelLabel = selectedModel === 'k3[1m]' ? 'Kimi K3' : selectedModel;
  document.querySelector('#settingsBtn').dataset.state = p.configured ? 'ready' : 'missing';
  document.querySelector('#settingsProviderSummary').textContent = p.configured
    ? `${modelLabel} · ${p.fingerprint || '已连接'}`
    : '尚未连接模型服务';
  document.querySelector('#providerBadge').textContent = modelLabel;
}

export function onSettingsDeviceStatus(device){
  const d = device || {};
  const detail = d.detail || {};
  const selected = (d.devices || []).find(item => item.serial === d.selected_serial) || {};
  const model = detail.model || selected.model || '';
  document.querySelector('#settingsDeviceSummary').textContent = d.status === 'ready'
    ? `${model || 'Android 设备'} · 已连接`
    : '尚未连接可用设备';
}

function closeKey(){
  document.querySelector('#keyModal').classList.add('hidden');
  document.querySelector('#keyInput').value = '';
  document.querySelector('#baseUrlInput').value = '';
  document.querySelector('#modelInput').value = '';
  document.querySelector('#rememberProvider').checked = false;
  document.querySelector('#keyError').classList.add('hidden');
}

function openKey(){
  settingsModal.classList.add('hidden');
  const configured = Boolean(providerState.configured);
  const keyInput = document.querySelector('#keyInput');
  keyInput.placeholder = configured ? '已连接；留空沿用当前 Key' : '粘贴 API Key';
  document.querySelector('#baseUrlInput').placeholder = configured
    ? '留空沿用当前地址' : 'https://api.kimi.com/coding/';
  document.querySelector('#modelInput').placeholder = providerState.model || 'k3[1m]';
  document.querySelector('#rememberProvider').checked = providerState.storage === 'system_credential';
  document.querySelector('#keyModal').classList.remove('hidden');
  keyInput.focus();
}

function pluginCard(plugin){
  const card = document.createElement('div');
  card.className = 'settings-card';
  const icon = document.createElement('span');
  icon.className = 'settings-card-icon';
  icon.textContent = '＋';
  const copy = document.createElement('span');
  copy.className = 'settings-card-copy';
  const name = document.createElement('strong');
  name.textContent = plugin.name || plugin.id;
  const summary = document.createElement('small');
  summary.textContent = plugin.summary || plugin.description || '可选能力';
  copy.append(name, summary);
  const button = document.createElement('button');
  button.className = 'secondary-button';
  button.textContent = '配置';
  button.disabled = !plugin.settings || plugin.settings.type !== 'file_import';
  button.onclick = () => openPlugin(plugin);
  card.append(icon, copy, button);
  return card;
}

async function loadPlugins(){
  const list = document.querySelector('#pluginSettingsList');
  const error = document.querySelector('#settingsError');
  error.classList.add('hidden');
  try {
    const response = await api.settingsPlugins();
    const plugins = response.plugins || [];
    list.innerHTML = '';
    if (!plugins.length){
      const empty = document.createElement('div');
      empty.className = 'settings-empty';
      empty.textContent = '暂无已安装的可配置能力';
      list.appendChild(empty);
      return;
    }
    plugins.forEach(plugin => list.appendChild(pluginCard(plugin)));
  } catch (e){
    error.textContent = '无法读取已安装能力，请稍后再试';
    error.classList.remove('hidden');
  }
}

function resetPluginFile(){
  pluginConfigFile = null;
  pluginInput.value = '';
  pluginName.textContent = (activePlugin && activePlugin.settings.input_label) || '选择配置文件';
  pluginSubmit.disabled = true;
  pluginSubmit.textContent = (activePlugin && activePlugin.settings.action_label) || '应用配置';
  pluginResult.textContent = '';
  pluginResult.className = 'config-result hidden';
  pluginLinkToken.value = '';
}

function syncTimeLabel(value){
  if (!value) return '尚未同步';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? `最后同步：${value}` : `最后同步：${date.toLocaleString()}`;
}

function renderOnlineSource(link){
  const source = link || {};
  const hasSource = Boolean(source.last_url);
  pluginLinkSource.classList.toggle('hidden', !hasSource);
  pluginRememberToken.checked = Boolean(source.token_saved);
  pluginLinkToken.placeholder = source.token_saved ? '已安全保存；留空沿用' : '输入 MCP Token';
  if (!hasSource) return;
  pluginLinkSourceUrl.textContent = source.last_url;
  pluginLinkSourceUrl.href = source.last_url;
  pluginLinkSyncedAt.textContent = syncTimeLabel(source.last_synced_at);
  pluginLinkRefresh.textContent = source.refresh_label || '刷新最新内容';
  pluginLinkRefresh.disabled = !source.can_refresh;
  pluginLinkRefresh.title = source.can_refresh ? '' : '请先输入 Token 并勾选“记住此电脑”';
}

function applyOnlineResult(resp){
  const link = (activePlugin.settings && activePlugin.settings.link_import) || {};
  const source = resp.source || {};
  if (source.url) link.last_url = source.url;
  if (source.synced_at) link.last_synced_at = source.synced_at;
  if (typeof resp.token_saved === 'boolean') link.token_saved = resp.token_saved;
  link.can_refresh = Boolean(link.last_url && link.token_saved);
  renderOnlineSource(link);
}

function openPlugin(plugin){
  activePlugin = plugin;
  const config = plugin.settings || {};
  settingsModal.classList.add('hidden');
  document.querySelector('#pluginConfigBadge').textContent = plugin.name || '可选能力';
  document.querySelector('#pluginConfigTitle').textContent = config.title || '能力配置';
  document.querySelector('#pluginConfigCopy').textContent = config.copy || plugin.description || '';
  document.querySelector('#pluginConfigHelp').textContent = config.help || '';
  pluginInput.accept = config.accept || '';
  const link = config.link_import || null;
  pluginLinkImport.classList.toggle('hidden', !link);
  if (link){
    document.querySelector('#pluginLinkTitle').textContent = link.title || '从在线表格读取';
    document.querySelector('#pluginLinkUrlLabel').textContent = link.url_label || '在线表格链接';
    document.querySelector('#pluginLinkTokenLabel').textContent = link.token_label || '访问 Token';
    document.querySelector('#pluginLinkTokenHelp').textContent = link.token_help || '';
    pluginLinkUrl.placeholder = link.url_placeholder || '';
    pluginLinkUrl.value = link.last_url || '';
    pluginLinkSubmit.textContent = link.action_label || '读取并应用';
    renderOnlineSource(link);
  }
  resetPluginFile();
  pluginModal.classList.remove('hidden');
}

document.querySelector('#settingsBtn').onclick = async () => {
  settingsModal.classList.remove('hidden');
  await loadPlugins();
};
document.querySelector('#settingsClose').onclick = () => settingsModal.classList.add('hidden');
document.querySelector('#providerSettingsOpen').onclick = openKey;
document.querySelector('#deviceSettingsOpen').onclick = () => {
  settingsModal.classList.add('hidden');
  document.querySelector('#deviceStatus').click();
};
document.querySelector('#keyCancel').onclick = closeKey;
document.querySelector('#keySubmit').onclick = async () => {
  const key = document.querySelector('#keyInput').value.trim();
  const baseUrl = document.querySelector('#baseUrlInput').value.trim();
  const model = document.querySelector('#modelInput').value.trim();
  const remember = document.querySelector('#rememberProvider').checked;
  const error = document.querySelector('#keyError');
  error.classList.add('hidden');
  const button = document.querySelector('#keySubmit');
  button.disabled = true;
  button.textContent = '连接中…';
  try {
    const resp = await api.setProviderKey(key, baseUrl, model, remember);
    if (!resp.ok){
      error.textContent = resp.error || '连接失败';
      error.classList.remove('hidden');
      return;
    }
    onProviderStatus(resp.provider);
    closeKey();
  } catch (e){
    error.textContent = '无法连接本机 Companion，请稍后再试';
    error.classList.remove('hidden');
  } finally {
    button.disabled = false;
    button.textContent = '保存并连接';
  }
};

document.querySelector('#pluginConfigClose').onclick = () => pluginModal.classList.add('hidden');
pluginInput.onchange = () => {
  pluginConfigFile = pluginInput.files[0] || null;
  pluginName.textContent = pluginConfigFile
    ? pluginConfigFile.name
    : ((activePlugin && activePlugin.settings.input_label) || '选择配置文件');
  pluginSubmit.disabled = !pluginConfigFile;
  pluginResult.className = 'config-result hidden';
};
pluginSubmit.onclick = async () => {
  if (!activePlugin || !pluginConfigFile) return;
  pluginSubmit.disabled = true;
  pluginSubmit.textContent = '校验并应用中…';
  pluginResult.className = 'config-result hidden';
  try {
    const resp = await api.importPluginConfig(activePlugin.id, pluginConfigFile);
    if (!resp.ok){
      pluginResult.textContent = resp.error || '配置失败';
      pluginResult.className = 'config-result error';
      return;
    }
    pluginResult.textContent = resp.message || '配置已生效';
    pluginResult.className = 'config-result success';
    pluginConfigFile = null;
    pluginInput.value = '';
    pluginSubmit.textContent = '已完成';
  } catch (e){
    pluginResult.textContent = '无法连接本机 Companion，请稍后再试';
    pluginResult.className = 'config-result error';
  } finally {
    if (pluginConfigFile){
      pluginSubmit.disabled = false;
      pluginSubmit.textContent = (activePlugin.settings && activePlugin.settings.action_label) || '应用配置';
    }
  }
};

pluginLinkSubmit.onclick = async () => {
  if (!activePlugin) return;
  const url = pluginLinkUrl.value.trim();
  const token = pluginLinkToken.value.trim();
  const link = (activePlugin.settings && activePlugin.settings.link_import) || {};
  if (!url || (!token && !link.token_saved)){
    pluginResult.textContent = !url ? '请输入腾讯表格链接' : '请输入腾讯文档 MCP Token';
    pluginResult.className = 'config-result error';
    return;
  }
  pluginLinkSubmit.disabled = true;
  pluginLinkSubmit.textContent = '读取并校验中…';
  pluginResult.className = 'config-result hidden';
  try {
    const resp = await api.syncPluginConfig(activePlugin.id, url, token, pluginRememberToken.checked);
    pluginLinkToken.value = '';
    if (!resp.ok){
      pluginResult.textContent = resp.error || '读取失败';
      pluginResult.className = 'config-result error';
      return;
    }
    applyOnlineResult(resp);
    pluginResult.textContent = (resp.message || '在线配置已生效')
      + (resp.credential_warning ? `；${resp.credential_warning}` : '');
    pluginResult.className = 'config-result success';
  } catch (e){
    pluginLinkToken.value = '';
    pluginResult.textContent = '无法连接本机 Companion，请稍后再试';
    pluginResult.className = 'config-result error';
  } finally {
    pluginLinkSubmit.disabled = false;
    const link = (activePlugin.settings && activePlugin.settings.link_import) || {};
    pluginLinkSubmit.textContent = link.action_label || '读取并应用';
  }
};

pluginLinkRefresh.onclick = async () => {
  if (!activePlugin) return;
  pluginLinkRefresh.disabled = true;
  pluginLinkRefresh.textContent = '正在刷新…';
  pluginResult.className = 'config-result hidden';
  try {
    const resp = await api.refreshPluginConfig(activePlugin.id);
    if (!resp.ok){
      pluginResult.textContent = resp.error || '刷新失败';
      pluginResult.className = 'config-result error';
      return;
    }
    applyOnlineResult(resp);
    pluginResult.textContent = resp.message || '已刷新最新内容';
    pluginResult.className = 'config-result success';
  } catch (e){
    pluginResult.textContent = '无法连接本机 Companion，请稍后再试';
    pluginResult.className = 'config-result error';
  } finally {
    const link = (activePlugin.settings && activePlugin.settings.link_import) || {};
    pluginLinkRefresh.disabled = !link.can_refresh;
    pluginLinkRefresh.textContent = link.refresh_label || '刷新最新内容';
  }
};
