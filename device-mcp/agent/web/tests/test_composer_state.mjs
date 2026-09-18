import assert from 'node:assert/strict';
import test from 'node:test';
import { composerAction } from '../composer_state.js';

test('running composer switches between stop and append send', () => {
  assert.deepEqual(composerAction('running', ''), {mode: 'stop', disabled: false});
  assert.deepEqual(composerAction('running', '追加说明'), {mode: 'send', disabled: false});
  assert.deepEqual(composerAction('running', '   '), {mode: 'stop', disabled: false});
});

test('idle and stopped need text before send is enabled', () => {
  assert.deepEqual(composerAction('idle', ''), {mode: 'send', disabled: true});
  assert.deepEqual(composerAction('stopped', ''), {mode: 'send', disabled: true});
  assert.deepEqual(composerAction('stopped', '继续'), {mode: 'send', disabled: false});
});
