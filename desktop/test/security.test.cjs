'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { CSP, isTrustedURL, isTrustedUIURL, isDownloadURL, assertTrustedIPC, hardenRequestHeaders, validateTaskNotification, needsQuitConfirmation } = require('../security.cjs');

const origin = 'http://127.0.0.1:34567';

test('trust requires the owned loopback origin; UI privilege also requires an app entry page', () => {
  assert.equal(isTrustedURL(`${origin}/api/health`, origin), true);
  for (const value of ['https://example.com', 'http://localhost:34567/', 'http://127.0.0.1:8787/', `${origin}.evil.test/`, 'file:///tmp/index.html', `${origin.replace('http://', 'http://user:pass@')}/`]) {
    assert.equal(isTrustedURL(value, origin), false, value);
  }
  assert.equal(isTrustedURL(origin, undefined), false);
  assert.equal(isTrustedUIURL(`${origin}/?view=tasks#work`, origin), true);
  assert.equal(isTrustedUIURL(`${origin}/index.html`, origin), true);
  assert.equal(isTrustedUIURL(`${origin}/api/runs/run/artifacts/evil.html`, origin), false);
  assert.equal(isTrustedUIURL(`${origin}/other.html`, origin), false);
});

test('IPC requires the live owned WebContents and its actual trusted main frame', () => {
  const mainFrame = { url: `${origin}/` };
  const owned = { mainFrame, isDestroyed: () => false };
  const event = { sender: owned, senderFrame: mainFrame };
  assert.doesNotThrow(() => assertTrustedIPC(event, owned, origin));
  assert.throws(() => assertTrustedIPC({ ...event, sender: {} }, owned, origin));
  assert.throws(() => assertTrustedIPC({ ...event, senderFrame: { url: `${origin}/` } }, owned, origin));
  assert.throws(() => assertTrustedIPC({ ...event, senderFrame: null }, owned, origin));
  mainFrame.url = `${origin}/api/runs/1/artifacts/untrusted.html`;
  assert.throws(() => assertTrustedIPC(event, owned, origin));
  mainFrame.url = `${origin}/`;
  owned.isDestroyed = () => true;
  assert.throws(() => assertTrustedIPC(event, owned, origin));
});

test('request headers strip renderer-supplied privileges and never send desktop tokens elsewhere', () => {
  const forged = { 'x-forge-desktop': 'forged', 'X-FORGE-DESKTOP-CONTROL': 'forged-control', Accept: 'application/json' };
  assert.deepEqual(hardenRequestHeaders(forged, `${origin}/api/projects`, origin, 'secret'), { Accept: 'application/json', 'X-Forge-Desktop': 'secret' });
  assert.deepEqual(hardenRequestHeaders(forged, 'https://example.com', origin, 'secret'), { Accept: 'application/json' });
  assert.deepEqual(forged, { 'x-forge-desktop': 'forged', 'X-FORGE-DESKTOP-CONTROL': 'forged-control', Accept: 'application/json' });
});

test('only known owned artifact and skill export routes can open native Save dialogs', () => {
  assert.equal(isDownloadURL(`${origin}/api/runs/run_123/artifacts/source.zip`, origin), true);
  assert.equal(isDownloadURL(`${origin}/api/skills/sk_123/export?version=2`, origin), true);
  for (const value of [`${origin}/api/settings`, `${origin}/api/runs/x/artifacts/../../settings`, `${origin}/`, 'https://example.com/api/runs/a/artifacts/payload.app']) {
    assert.equal(isDownloadURL(value, origin), false);
  }
});

test('task notifications accept real final/actionable statuses and reject malformed or oversized input', () => {
  for (const status of ['completed', 'failed', 'needs_attention', 'awaiting_approval', 'awaiting_decision', 'awaiting_acceptance']) {
    assert.equal(validateTaskNotification({ id: 'run_123', title: '  一项任务  ', status }).title, '一项任务');
  }
  for (const value of [null, [], { id: '../file', title: 'a', status: 'completed' }, { id: 'a', title: 'a'.repeat(181), status: 'completed' }, { id: 'a', title: '\n', status: 'completed' }, { id: 'a', title: 'a', status: 'running' }, { id: 'a', title: 'a', status: 'completed', path: '/tmp' }]) {
    assert.throws(() => validateTaskNotification(value));
  }
});

test('Quit checks active runs, pending prompts, and fails closed on unknown status', () => {
  assert.equal(needsQuitConfirmation({ active_runs: 0 }), false);
  assert.equal(needsQuitConfirmation({ active_runs: [], prompts: [] }), false);
  for (const status of [null, {}, { active_runs: 1 }, { active_runs: ['run'] }, { active_runs: 0, prompts: 1 }, { active_runs: -1 }, { active_runs: '0' }, { active_runs: 0, prompts: null }]) {
    assert.equal(needsQuitConfirmation(status), true);
  }
});

test('CSP excludes script execution escapes and subframes', () => {
  assert.match(CSP, /script-src 'self';/);
  assert.doesNotMatch(CSP, /unsafe-eval/);
  assert.match(CSP, /frame-src 'none'/);
  assert.match(CSP, /object-src 'none'/);
});
