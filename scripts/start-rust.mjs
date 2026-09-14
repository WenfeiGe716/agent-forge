import fs from 'node:fs';
import path from 'node:path';
import net from 'node:net';
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { spawn, spawnSync } from 'node:child_process';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const args = process.argv.slice(2);
let port = 8787;
let build = true;
for (let i = 0; i < args.length; i++) {
  if (args[i] === '--port') port = Number(args[++i]);
  else if (args[i] === '--no-build') build = false;
  else if (args[i] === '--help') {
    console.log('Usage: bash start.sh [--port 8787] [--no-build]\nLegacy rollback: bash start.sh --legacy');
    process.exit(0);
  } else throw new Error(`Unknown option: ${args[i]}`);
}
if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('Invalid port');
const probe = net.createServer();
await new Promise((resolve, reject) => probe.once('error', reject).listen(port, '127.0.0.1', resolve));
await new Promise(resolve => probe.close(resolve));
const local = path.join(root, '.local');
fs.mkdirSync(local, { recursive: true, mode: 0o700 });
const configPath = path.join(local, 'config.json');
const legacy = path.join(local, 'forge.db');
if (!fs.existsSync(configPath)) {
  if (fs.existsSync(legacy)) throw new Error('Legacy database needs its original .local/config.json encryption key');
  const config = {
    ENCRYPTION_KEY: crypto.randomBytes(32).toString('base64url') + '=',
    ADMIN_PASSWORD: crypto.randomBytes(24).toString('base64url'),
    DATA_ROOT: path.join(local, 'data'),
    IMPORT_ROOT: path.join(root, 'imports'),
  };
  fs.writeFileSync(configPath, JSON.stringify(config, null, 2), { flag: 'wx', mode: 0o600 });
  console.log(`Initial admin password is stored in ${configPath}`);
}
const config = JSON.parse(fs.readFileSync(configPath, 'utf8'));
if (typeof config.ENCRYPTION_KEY !== 'string') throw new Error('Missing product encryption key');
const frontend = path.join(root, 'frontend', 'dist');
if (!fs.existsSync(path.join(frontend, 'index.html'))) throw new Error('Build frontend first: node scripts/build-offline.mjs');
if (build) {
  const result = spawnSync('cargo', ['build', '--manifest-path', path.join(root, 'rust/Cargo.toml'), '--locked', '-p', 'forge-server'], { stdio: 'inherit' });
  if (result.error || result.status !== 0) process.exit(result.status || 1);
}
const binary = path.join(root, 'rust', 'target', 'debug', 'forge-server');
const product = path.join(local, 'product.sqlite3');
const serverArgs = ['--listen', `127.0.0.1:${port}`, '--product-db', product, '--rust-db', path.join(local, 'runs.sqlite3'),
  '--data-root', config.DATA_ROOT || path.join(local, 'data'), '--import-root', config.IMPORT_ROOT || path.join(root, 'imports'), '--frontend', frontend];
if (!fs.existsSync(product) && fs.existsSync(legacy)) serverArgs.push('--import-db', legacy);
const env = { ...process.env, ...Object.fromEntries(Object.entries(config).map(([k,v]) => [k,String(v)])),
  ALLOWED_ORIGINS: `http://127.0.0.1:${port},http://localhost:${port}`, SECURE_COOKIE: 'false' };
const gateway = path.join(root, 'runtime/opencodex-gateway.ts');
if (!env.FORGE_OPENCODEX_ENTRY && fs.existsSync(gateway)) env.FORGE_OPENCODEX_ENTRY = gateway;
const discoveredBun = spawnSync('which', ['bun'], { encoding: 'utf8' });
const bun = [path.join(root, '.local/runtimes/bun/bin/bun'),
  path.join(root, '.local/build/gateway-toolchain/node_modules/@oven/bun-darwin-aarch64/bin/bun'),
  discoveredBun.status === 0 ? discoveredBun.stdout.trim() : ''].find(candidate => candidate && fs.existsSync(candidate));
if (!env.FORGE_BUN_BINARY && bun) env.FORGE_BUN_BINARY = bun;
const server = spawn(binary, serverArgs, { cwd: root, env, stdio: 'inherit' });
server.on('error', error => { console.error(error.message); process.exitCode = 1; });
for (const signal of ['SIGINT','SIGTERM']) process.on(signal, () => server.kill(signal));
server.on('exit', code => { process.exitCode = code || 0; });
