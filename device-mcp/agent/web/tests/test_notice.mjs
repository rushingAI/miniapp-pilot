import { test } from 'node:test';
import assert from 'node:assert';
import { appendDismissibleNotice } from '../notice.js';

class FakeElement {
  constructor(tagName){
    this.tagName = tagName;
    this.children = [];
    this.attributes = {};
    this.removed = false;
    this.className = '';
    this.textContent = '';
  }
  appendChild(child){ this.children.push(child); return child; }
  setAttribute(name, value){ this.attributes[name] = value; }
  remove(){ this.removed = true; }
}

const fakeDocument = { createElement: tagName => new FakeElement(tagName) };

test('非致命提醒带有可关闭的×按钮', () => {
  const container = new FakeElement('div');
  const notice = appendDismissibleNotice(fakeDocument, container, '模型已持续无可见进展');

  assert.equal(container.children[0], notice);
  assert.equal(notice.className, 'dismissible-notice');
  assert.equal(notice.children[0].textContent, '⚠ 模型已持续无可见进展');

  const close = notice.children[1];
  assert.equal(close.tagName, 'button');
  assert.equal(close.textContent, '×');
  assert.equal(close.attributes['aria-label'], '关闭提醒');
  close.onclick();
  assert.equal(container.removed, true);
  assert.equal(notice.removed, false);
});
