import { test } from 'node:test';
import assert from 'node:assert';
import fs from 'node:fs';
import vm from 'node:vm';

test('the initial companion label is connecting rather than stale online', () => {
  const html = fs.readFileSync(new URL('../index.html', import.meta.url), 'utf8');

  assert.match(html, /id="companionStatus" data-state="checking"/);
  assert.match(html, /id="companionStatusText">本地服务连接中</);
});



test('an ordinary websocket reconnect does not discard the current page', () => {
  const sourceUrl = new URL('../ws.js', import.meta.url);
  let source = fs.readFileSync(sourceUrl, 'utf8')
    .replace(/^import .*;$/gm, '')
    .replace('export const api =', 'const api =')
    .replace(/bootstrap\(\);\s*$/, '')
    .concat('\nglobalThis.__connect = connect;');
  const sockets = [];
  const reconnects = [];
  let reloads = 0;

  class FakeWebSocket {
    constructor() { sockets.push(this); }
    send() {}
  }

  const context = {
    WebSocket: FakeWebSocket,
    location: {protocol: 'http:', host: '127.0.0.1:8000', reload: () => { reloads += 1; }},
    sessionStorage: {getItem: () => null, removeItem() {}},
    fetch: () => Promise.reject(new Error('unused')),
    FormData: class {},
    setInterval: () => 1,
    clearInterval: () => {},
    setTimeout: callback => { reconnects.push(callback); return 1; },
    clearTimeout: () => {},
    document: {querySelector: () => null},
    console,
  };
  vm.runInNewContext(source, context);

  context.__connect();
  sockets[0].onopen();
  sockets[0].onclose();
  reconnects.shift()();
  sockets[1].onopen();

  assert.equal(reloads, 0);
});



test('websocket connection state replaces the stale companion online label', () => {
  const sourceUrl = new URL('../ws.js', import.meta.url);
  let source = fs.readFileSync(sourceUrl, 'utf8')
    .replace(/^import .*;$/gm, '')
    .replace('export const api =', 'const api =')
    .replace(/bootstrap\(\);\s*$/, '')
    .concat('\nglobalThis.__connect = connect;');
  const sockets = [];
  const reconnects = [];
  const status = {dataset: {state: 'checking'}};
  const label = {textContent: '本地服务连接中'};
  const activity = {textContent: '思考中'};

  class FakeWebSocket {
    constructor() { sockets.push(this); }
    send() {}
  }

  const context = {
    WebSocket: FakeWebSocket,
    location: {protocol: 'http:', host: '127.0.0.1:8000', reload() {}},
    sessionStorage: {getItem: () => null, removeItem() {}},
    fetch: () => Promise.reject(new Error('unused')),
    FormData: class {},
    setInterval: () => 1,
    clearInterval: () => {},
    setTimeout: callback => { reconnects.push(callback); return 1; },
    clearTimeout: () => {},
    document: {querySelector: selector => ({
      '#companionStatus': status,
      '#companionStatusText': label,
      '#typingText': activity,
    })[selector] || null},
    console,
  };
  vm.runInNewContext(source, context);

  context.__connect();
  sockets[0].onopen();
  assert.equal(status.dataset.state, 'online');
  assert.equal(label.textContent, '本地服务在线');
  assert.equal(activity.textContent, '思考中');

  sockets[0].onclose();
  assert.equal(status.dataset.state, 'offline');
  assert.equal(label.textContent, '本地服务已断开');
  assert.equal(activity.textContent, '连接已断开，执行状态未知');
});

test('interactive status restores turn state without a removed architecture handler', () => {
  const sourceUrl = new URL('../ws.js', import.meta.url);
  let source = fs.readFileSync(sourceUrl, 'utf8')
    .replace(/^import .*;$/gm, '')
    .replace('export const api =', 'const api =')
    .replace(/bootstrap\(\);\s*$/, '')
    .concat('\nglobalThis.__dispatch = dispatch;');
  const restored = [];
  const context = {
    fetch: () => Promise.reject(new Error('unused')),
    FormData: class {},
    document: {querySelector: () => null},
    restoreTurnState: (...args) => restored.push(args),
    onScreen() {}, onDeviceStatus() {}, onSettingsDeviceStatus() {},
    onDrawer() {}, resetTurn() {}, onChat() {},
    console,
  };
  vm.runInNewContext(source, context);

  context.__dispatch({
    type: 'interactive_status',
    busy: false,
    turn_state: 'stopped',
    active_suite: {suite_id: 'suite-1'},
  });

  assert.deepEqual(JSON.parse(JSON.stringify(restored)), [
    [false, 'stopped', {suite_id: 'suite-1'}],
  ]);
});

test('ordinary websocket reconnect refreshes authoritative runtime state', async () => {
  const sourceUrl = new URL('../ws.js', import.meta.url);
  let source = fs.readFileSync(sourceUrl, 'utf8')
    .replace(/^import .*;$/gm, '')
    .replace('export const api =', 'const api =')
    .replace(/bootstrap\(\);\s*$/, '')
    .concat('\nglobalThis.__connect = connect;');
  const sockets = [];
  const reconnects = [];
  const requests = [];
  const restored = [];

  class FakeWebSocket {
    constructor() { sockets.push(this); }
    send() {}
  }

  const context = {
    WebSocket: FakeWebSocket,
    location: {protocol: 'http:', host: '127.0.0.1:8000', reload() {}},
    sessionStorage: {getItem: () => null, removeItem() {}},
    fetch: async url => {
      requests.push(url);
      return {json: async () => ({
        events: [], last_event_seq: 0, busy: false, turn_state: 'stopped',
        active_suite: {suite_id: 'suite-1'}, device: {status: 'ready'},
      })};
    },
    FormData: class {},
    setInterval: () => 1,
    clearInterval: () => {},
    setTimeout: callback => { reconnects.push(callback); return 1; },
    clearTimeout: () => {},
    document: {querySelector: () => null, documentElement: {classList: {add() {}, remove() {}}}},
    restoreTurnState: (...args) => restored.push(args),
    onDeviceStatus() {}, onSettingsDeviceStatus() {},
    console,
  };
  vm.runInNewContext(source, context);

  context.__connect();
  await sockets[0].onopen();
  sockets[0].onclose();
  reconnects.shift()();
  await sockets[1].onopen();

  assert.deepEqual(requests, ['/api/snapshot?after=0']);
  assert.deepEqual(JSON.parse(JSON.stringify(restored)), [
    [false, 'stopped', {suite_id: 'suite-1'}],
  ]);
});
