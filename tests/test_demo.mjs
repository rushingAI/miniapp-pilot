import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

function page(){
  let definition;
  let stored;
  const sandbox = {
    require: () => ({fixtureBaseUrl: ''}),
    Page: value => {definition = value;},
    wx: {getStorageSync: () => stored, setStorageSync: (_, value) => {stored = value;}},
  };
  vm.runInNewContext(fs.readFileSync(new URL('../examples/task-list/pages/index/index.js', import.meta.url), 'utf8'), sandbox);
  definition.setData = values => Object.assign(definition.data, values);
  definition.onLoad();
  return definition;
}

test('synthetic task create/edit/filter/complete/reset workflow', () => {
  const p = page();
  p.onTitle({detail: {value: '阅读示例'}});
  p.save();
  assert.equal(p.data.tasks.length, 1);
  const event = {currentTarget: {dataset: {id: p.data.tasks[0].id}}};
  p.edit(event);
  p.onTitle({detail: {value: '阅读指南'}});
  p.save();
  assert.equal(p.data.tasks[0].title, '阅读指南');
  p.toggle(event);
  p.filter({currentTarget: {dataset: {filter: 'todo'}}});
  assert.equal(p.data.visibleTasks.length, 0);
  p.filter({currentTarget: {dataset: {filter: 'done'}}});
  assert.equal(p.data.visibleTasks.length, 1);
  p.reset();
  assert.equal(p.data.tasks.length, 0);
});

test('invalid names do not create tasks and missing service is explicit', () => {
  const p = page();
  for (const value of ['', '   ', 'x'.repeat(81)]) {
    p.onTitle({detail: {value}});
    p.save();
    assert.equal(p.data.tasks.length, 0);
    assert.ok(p.data.error);
  }
  p.loadFixtures();
  assert.equal(p.data.error, '未配置测试数据服务');
});
