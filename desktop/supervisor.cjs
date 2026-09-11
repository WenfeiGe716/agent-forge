'use strict';

const { EventEmitter } = require('node:events');
const { spawn } = require('node:child_process');
const http = require('node:http');
const net = require('node:net');
const crypto = require('node:crypto');

const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

function findAvailablePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const port = server.address().port;
      server.close(error => error ? reject(error) : resolve(port));
    });
  });
}

function requestBackend({ origin, desktopToken, controlToken }, route, { method = 'GET', body, privileged = false, timeout = 5000 } = {}) {
  const base = new URL(origin);
  if (base.protocol !== 'http:' || base.hostname !== '127.0.0.1' || !base.port ||
      base.username || base.password || typeof route !== 'string' || !route.startsWith('/api/') ||
      route.includes('\\') || route.startsWith('//')) {
    return Promise.reject(new Error('无效的本地服务地址。'));
  }
  const target = new URL(route, base);
  if (target.origin !== base.origin) return Promise.reject(new Error('无效的本地服务地址。'));
  const payload = body === undefined ? null : JSON.stringify(body);
  const headers = { 'X-Forge-Desktop': desktopToken, Accept: 'application/json' };
  if (privileged) headers['X-Forge-Desktop-Control'] = controlToken;
  if (payload !== null) {
    headers['Content-Type'] = 'application/json';
    headers['Content-Length'] = Buffer.byteLength(payload);
  }
  if (method !== 'GET' && method !== 'HEAD') headers['X-Forge-Request'] = '1';
  return new Promise((resolve, reject) => {
    const request = http.request(target, { method, headers, agent: false }, response => {
      const chunks = [];
      let size = 0;
      response.on('data', chunk => {
        size += chunk.length;
        if (size > 1024 * 1024) request.destroy(new Error('本地服务响应超出限制。'));
        else chunks.push(chunk);
      });
      response.on('error', reject);
      response.on('end', () => {
        if (response.statusCode < 200 || response.statusCode >= 300) {
          reject(new Error(`本地服务请求失败（HTTP ${response.statusCode}）。`));
          return;
        }
        try {
          const raw = Buffer.concat(chunks).toString('utf8');
          resolve({ data: raw ? JSON.parse(raw) : null, headers: response.headers });
        } catch { reject(new Error('本地服务返回了无效的数据。')); }
      });
    });
    const timer = setTimeout(() => request.destroy(new Error('本地服务请求超时。')), timeout);
    request.once('close', () => clearTimeout(timer));
    request.once('error', reject);
    request.end(payload);
  });
}

function spawnInput(command, input, spawnImpl = spawn) {
  const child = spawnImpl(command.file, command.args || [], {
    cwd: command.cwd,
    env: { ...process.env, ...command.env },
    windowsHide: true,
    detached: false,
    stdio: ['pipe', 'pipe', 'pipe'],
  });
  // Neither launch secrets nor backend output are persisted or forwarded to a renderer.
  child.stderr?.resume();
  child.stdin?.on('error', () => {});
  child.stdin?.end(`${JSON.stringify(input)}\n`);
  return child;
}

async function stopChild(child, { graceMs = 8000, killMs = 2000 } = {}) {
  if (!child || !child.pid || child.exitCode !== null || child.signalCode) return;
  const waitForExit = ms => new Promise(resolve => {
    if (child.exitCode !== null || child.signalCode) return resolve();
    let timer;
    const done = () => { clearTimeout(timer); child.removeListener('exit', done); resolve(); };
    child.once('exit', done);
    timer = setTimeout(done, ms);
  });
  try { child.kill('SIGTERM'); } catch { return; }
  await waitForExit(graceMs);
  if (child.exitCode === null && !child.signalCode) {
    try { child.kill('SIGKILL'); } catch { return; }
    await waitForExit(killMs);
  }
}

async function runPreparation(command, input, { spawnImpl = spawn, timeoutMs = 180000, signal } = {}) {
  if (signal?.aborted) throw new Error('准备应用数据已取消。');
  const child = spawnInput(command, { ...input, action: 'prepare' }, spawnImpl);
  let output = '';
  let oversized = false;
  child.stdout?.on('data', chunk => {
    if (Buffer.byteLength(output) + chunk.length > 1024 * 1024) oversized = true;
    else output += chunk.toString('utf8');
  });
  let timeout;
  let onAbort;
  try {
    const code = await Promise.race([
      new Promise((resolve, reject) => {
        child.once('error', () => reject(new Error('无法启动本地数据准备程序。')));
        child.once('close', resolve);
      }),
      new Promise((_, reject) => { timeout = setTimeout(() => reject(new Error('准备应用数据超时。')), timeoutMs); }),
      new Promise((_, reject) => {
        if (signal) {
          onAbort = () => reject(new Error('准备应用数据已取消。'));
          signal.addEventListener('abort', onAbort, { once: true });
        }
      }),
    ]);
    if (oversized) throw new Error('无法准备应用数据。请确认所选目录有效且磁盘空间充足。');
    let result;
    try { result = JSON.parse(output); } catch { throw new Error('本地数据准备程序返回了无效的数据。'); }
    if (code !== 0 || result?.ok === false) {
      // The bundled helper explicitly marks validation messages as safe; never show its other output.
      const safeError = result?.ok === false && typeof result.error === 'string' && result.error.length <= 500 && !/[\x00-\x1f\x7f]/.test(result.error);
      throw new Error(safeError ? result.error : '无法准备应用数据。请确认所选目录有效且磁盘空间充足。');
    }
    if (result?.ok !== true || typeof result.staging_dir !== 'string' || !result.secrets || typeof result.secrets !== 'object') {
      throw new Error('本地数据准备未完成。');
    }
    return result;
  } finally {
    clearTimeout(timeout);
    if (onAbort) signal.removeEventListener('abort', onAbort);
    output = '';
    await stopChild(child);
  }
}

class BackendSupervisor extends EventEmitter {
  constructor(command, options = {}) {
    super();
    this.command = command;
    this.spawnImpl = options.spawnImpl || spawn;
    this.portImpl = options.portImpl || findAvailablePort;
    this.requestImpl = options.requestImpl || requestBackend;
    this.startTimeoutMs = options.startTimeoutMs || 60000;
    this.pollMs = options.pollMs || 200;
    this.stopOptions = options.stopOptions || {};
    this.child = null;
    this.connection = null;
    this.stopping = false;
    this.starting = false;
  }

  async start({ dataDir, secrets }) {
    if (this.child || this.starting) throw new Error('本地服务已启动。');
    this.starting = true;
    this.stopping = false;
    try {
      for (let attempt = 0; attempt < 3; attempt += 1) {
        const port = await this.portImpl();
        if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('无法分配本地服务端口。');
        const connection = {
          origin: `http://127.0.0.1:${port}`,
          desktopToken: crypto.randomBytes(32).toString('hex'),
          controlToken: crypto.randomBytes(32).toString('hex'),
        };
        const child = spawnInput(this.command, {
          action: 'serve', data_dir: dataDir, port,
          desktop_token: connection.desktopToken,
          control_token: connection.controlToken,
          secrets,
        }, this.spawnImpl);
        this.child = child;
        this.connection = connection;
        let exited = false;
        let launchError = false;
        child.stdout?.resume();
        child.once('error', () => { launchError = true; });
        child.once('exit', (code, signal) => {
          exited = true;
          if (this.child === child) {
            this.child = null;
            this.connection = null;
            if (!this.stopping && !this.starting) this.emit('unexpected-exit', { code, signal });
          }
        });
        const deadline = Date.now() + this.startTimeoutMs;
        while (Date.now() < deadline && !exited && !launchError && !this.stopping) {
          try {
            const response = await this.requestImpl(connection, '/api/health', { timeout: 1000 });
            // Health requests disclose the ordinary desktop token, so ownership proof
            // must use the still-private control token received only over child stdin.
            const identity = crypto.createHash('sha256').update(connection.controlToken).digest('hex');
            if (response.data?.ok === true && response.data.desktop_instance === identity &&
                !exited && !launchError && !this.stopping) return connection;
          } catch { /* The owned child may still be starting or a raced port may be occupied. */ }
          await delay(this.pollMs);
        }
        const timedOut = !exited && !launchError && !this.stopping;
        await stopChild(child, this.stopOptions);
        if (this.child === child) this.child = null;
        this.connection = null;
        if (this.stopping) throw new Error('本地服务启动已取消。');
        if (launchError) throw new Error('无法启动内置本地服务，请检查应用是否完整。');
        if (timedOut) throw new Error('本地服务启动超时。');
      }
      throw new Error('本地服务启动失败，请检查应用数据或重新打开应用。');
    } finally { this.starting = false; }
  }

  request(route, options) {
    if (!this.connection || !this.child) return Promise.reject(new Error('本地服务未运行。'));
    return this.requestImpl(this.connection, route, options);
  }

  async stop() {
    this.stopping = true;
    const ownedChild = this.child;
    await stopChild(ownedChild, this.stopOptions);
    if (this.child === ownedChild) {
      this.child = null;
      this.connection = null;
    }
  }
}

module.exports = { BackendSupervisor, findAvailablePort, requestBackend, runPreparation };
