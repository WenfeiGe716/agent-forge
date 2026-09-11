'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const { PassThrough } = require('node:stream');
const http = require('node:http');
const crypto = require('node:crypto');
const { BackendSupervisor, findAvailablePort, requestBackend, runPreparation } = require('../supervisor.cjs');

class FakeChild extends EventEmitter {
  constructor({ ignoreTerm = false } = {}) {
    super();
    this.pid = 12345;
    this.exitCode = null;
    this.signalCode = null;
    this.stdin = new PassThrough();
    this.stdout = new PassThrough();
    this.stderr = new PassThrough();
    this.input = '';
    this.kills = [];
    this.ignoreTerm = ignoreTerm;
    this.stdin.on('data', chunk => { this.input += chunk; });
  }
  finish(code = 0, signal = null) {
    this.exitCode = code;
    this.signalCode = signal;
    this.stdout.end();
    this.stderr.end();
    this.emit('exit', code, signal);
    this.emit('close', code, signal);
  }
  kill(signal) {
    this.kills.push(signal);
    if (!(this.ignoreTerm && signal === 'SIGTERM')) this.finish(null, signal);
    return true;
  }
}

const healthy = async connection => ({ data: { ok: true, desktop_instance: crypto.createHash('sha256').update(connection.controlToken).digest('hex') }, headers: {} });
const settings = { dataDir: '/tmp/test-workspace', secrets: { ADMIN_PASSWORD: 'never-in-process-args' } };
const command = { file: '/owned/backend', args: [], cwd: '/owned' };

test('random port allocation is loopback and does not target the existing default service', async () => {
  const port = await findAvailablePort();
  assert.equal(Number.isInteger(port), true);
  assert.ok(port > 0 && port < 65536);
});

test('private main requests carry control header only when requested and never follow redirects', async t => {
  const requests = [];
  const server = http.createServer((request, response) => {
    requests.push(request);
    request.resume();
    if (request.url === '/api/redirect') { response.writeHead(302, { Location: 'https://example.com' }); response.end(); }
    else { response.writeHead(200, { 'Content-Type': 'application/json' }); response.end('{"ok":true}'); }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise(resolve => server.close(resolve)));
  const connection = { origin: `http://127.0.0.1:${server.address().port}`, desktopToken: 'desktop', controlToken: 'control' };
  await requestBackend(connection, '/api/health');
  await requestBackend(connection, '/api/desktop/session', { method: 'POST', privileged: true });
  assert.equal(requests[0].headers['x-forge-desktop'], 'desktop');
  assert.equal(requests[0].headers['x-forge-desktop-control'], undefined);
  assert.equal(requests[1].headers['x-forge-desktop-control'], 'control');
  assert.equal(requests[1].headers['x-forge-request'], '1');
  assert.equal(requests[1].headers.origin, undefined);
  await assert.rejects(requestBackend(connection, '/api/redirect'), /HTTP 302/);
  await assert.rejects(requestBackend({ ...connection, origin: 'https://example.com' }, '/api/health'));
  await assert.rejects(requestBackend(connection, '//example.com/api/health'));
});

test('supervisor launches a child with stdin-only secrets and stops only that owned child', async () => {
  let launched;
  const owned = new FakeChild();
  const unrelated = new FakeChild();
  const supervisor = new BackendSupervisor(command, {
    spawnImpl: (...args) => { launched = args; return owned; }, portImpl: async () => 32100, requestImpl: healthy,
  });
  const connection = await supervisor.start(settings);
  assert.equal(connection.origin, 'http://127.0.0.1:32100');
  assert.equal(JSON.stringify(launched).includes('never-in-process-args'), false);
  const launch = JSON.parse(owned.input);
  assert.equal(launch.secrets.ADMIN_PASSWORD, 'never-in-process-args');
  assert.equal(launch.port, 32100);
  assert.notEqual(launch.desktop_token, launch.control_token);
  assert.equal(launched[2].detached, false);
  await assert.rejects(supervisor.start(settings), /已启动/);
  await supervisor.stop();
  await supervisor.stop();
  assert.deepEqual(owned.kills, ['SIGTERM']);
  assert.deepEqual(unrelated.kills, []);
  assert.equal(supervisor.connection, null);
});

test('an unrelated healthy port is not accepted without the owned desktop identity', async () => {
  const owned = new FakeChild();
  const supervisor = new BackendSupervisor(command, {
    spawnImpl: () => owned, portImpl: async () => 32101, requestImpl: async () => ({ data: { ok: true } }),
    startTimeoutMs: 15, pollMs: 1, stopOptions: { graceMs: 5, killMs: 5 },
  });
  await assert.rejects(supervisor.start(settings), /超时/);
  assert.deepEqual(owned.kills, ['SIGTERM']);
  assert.equal(supervisor.connection, null);
});

test('a port-race listener cannot authenticate by hashing the received ordinary desktop token', async () => {
  const owned = new FakeChild();
  const supervisor = new BackendSupervisor(command, {
    spawnImpl: () => owned, portImpl: async () => 32107,
    requestImpl: async connection => ({ data: { ok: true, desktop_instance: crypto.createHash('sha256').update(connection.desktopToken).digest('hex') } }),
    startTimeoutMs: 10, pollMs: 1,
  });
  await assert.rejects(supervisor.start(settings), /超时/);
  assert.deepEqual(owned.kills, ['SIGTERM']);
});

test('an early bind failure retries a fresh random port rather than attaching to that port', async () => {
  const children = [];
  let port = 32102;
  const supervisor = new BackendSupervisor(command, {
    spawnImpl: () => { const child = new FakeChild(); children.push(child); return child; },
    portImpl: async () => port++,
    requestImpl: async connection => {
      if (children.length === 1) { children[0].finish(1); throw new Error('connection refused'); }
      return healthy(connection);
    }, pollMs: 1,
  });
  const connection = await supervisor.start(settings);
  assert.equal(connection.origin, 'http://127.0.0.1:32103');
  assert.equal(children.length, 2);
  assert.deepEqual(children[0].kills, []);
  await supervisor.stop();
});

test('port errors never spawn or terminate any process', async () => {
  let spawned = false;
  const supervisor = new BackendSupervisor(command, {
    spawnImpl: () => { spawned = true; return new FakeChild(); },
    portImpl: async () => { throw new Error('port unavailable'); },
  });
  await assert.rejects(supervisor.start(settings), /port unavailable/);
  await supervisor.stop();
  assert.equal(spawned, false);
});

test('explicit shutdown escalates only its unresponsive child after a bounded grace period', async () => {
  const child = new FakeChild({ ignoreTerm: true });
  const supervisor = new BackendSupervisor(command, {
    spawnImpl: () => child, portImpl: async () => 32104, requestImpl: healthy, stopOptions: { graceMs: 10, killMs: 10 },
  });
  let unexpected = 0;
  supervisor.on('unexpected-exit', () => { unexpected += 1; });
  await supervisor.start(settings);
  await supervisor.stop();
  assert.deepEqual(child.kills, ['SIGTERM', 'SIGKILL']);
  assert.equal(unexpected, 0);
});

test('unexpected child termination clears connection and notifies the application', async () => {
  const child = new FakeChild();
  const supervisor = new BackendSupervisor(command, { spawnImpl: () => child, portImpl: async () => 32105, requestImpl: healthy });
  await supervisor.start(settings);
  const seen = [];
  supervisor.on('unexpected-exit', detail => seen.push(detail));
  child.finish(3);
  assert.deepEqual(seen, [{ code: 3, signal: null }]);
  assert.equal(supervisor.connection, null);
  await assert.rejects(supervisor.request('/api/health'), /未运行/);
});

test('preparation returns private secrets but only surfaces explicitly safe validation errors', async () => {
  const invoke = value => () => {
    const child = new FakeChild();
    queueMicrotask(() => { child.stdout.write(JSON.stringify(value)); child.finish(value.ok ? 0 : 1); });
    return child;
  };
  const result = await runPreparation(command, { data_dir: '/tmp/work' }, {
    spawnImpl: invoke({ ok: true, staging_dir: '/tmp/.forge-prepare-test', secrets: { ENCRYPTION_KEY: 'private' } }),
  });
  assert.equal(result.secrets.ENCRYPTION_KEY, 'private');
  await assert.rejects(runPreparation(command, {}, { spawnImpl: invoke({ ok: false, error: '原工作空间还有 1 个未结束任务' }) }), /未结束任务/);
  await assert.rejects(runPreparation(command, {}, { spawnImpl: invoke({ ok: false, error: 'traceback\nsecret' }) }), error => !error.message.includes('secret'));
});

test('cancelling preparation terminates its owned helper and does not wait for the full timeout', async () => {
  const child = new FakeChild();
  const abort = new AbortController();
  const result = runPreparation(command, {}, { spawnImpl: () => child, timeoutMs: 5000, signal: abort.signal });
  abort.abort();
  await assert.rejects(result, /已取消/);
  assert.deepEqual(child.kills, ['SIGTERM']);
});
