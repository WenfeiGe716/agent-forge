'use strict';

const { app, BrowserWindow, Menu, dialog, ipcMain, session, safeStorage, shell, Notification } = require('electron');
const fs = require('node:fs/promises');
const path = require('node:path');
const crypto = require('node:crypto');
const { pathToFileURL } = require('node:url');
const { CSP, isTrustedURL, isTrustedUIURL, isDownloadURL, assertTrustedIPC, hardenRequestHeaders, validateTaskNotification, needsQuitConfirmation } = require('./security.cjs');
const { BackendSupervisor, runPreparation } = require('./supervisor.cjs');

app.setName('Agent Forge');
// Keep the packaged app's workspace separate from the development workspace.
// This prevents a locally configured provider, model, or task history from
// being picked up when the distributable is opened on the build machine, while
// still giving each packaged installation a persistent workspace of its own.
const userDataName = app.isPackaged ? 'Agent Forge Packaged' : 'Agent Forge';
app.setPath('userData', path.join(app.getPath('appData'), userDataName));

let window = null;
let backend = null;
let browserSession = null;
let connection = null;
let ready = false;
let finalQuit = false;
let quitPending = false;
let restarting = false;
let startupTask = null;
let initialWindowLoad = Promise.resolve();
let preparationAbort = null;
let secrets = null;
const dataDir = path.join(app.getPath('userData'), 'workspace');
const bootURL = pathToFileURL(path.join(__dirname, 'boot.html')).href;
const bootStyleURL = pathToFileURL(path.join(__dirname, 'boot.css')).href;
const notificationTimes = new Map();
const recentNotifications = [];

function command() {
  if (app.isPackaged) {
    return { file: path.join(process.resourcesPath, 'backend', 'forge-server'), args: ['--desktop'], cwd: process.resourcesPath };
  }
  const root = path.resolve(__dirname, '..');
  return {
    file: path.join(root, 'rust', 'target', 'debug', process.platform === 'win32' ? 'forge-server.exe' : 'forge-server'),
    args: ['--desktop'],
    cwd: root,
  };
}

function showWindow() {
  if (window?.isDestroyed()) window = null;
  if (!window) void createWindow().catch(error => { void startupFailed(error); });
  if (window.isMinimized()) window.restore();
  window.show();
  window.focus();
}

function createSession() {
  const result = session.fromPartition(`forge-desktop-${crypto.randomUUID()}`, { cache: false });
  result.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  result.setPermissionCheckHandler(() => false);
  result.setDevicePermissionHandler(() => false);
  result.setDisplayMediaRequestHandler((_request, callback) => callback({}));
  result.webRequest.onBeforeRequest((details, callback) => {
    const allowed = details.url === bootURL || details.url === bootStyleURL || isTrustedURL(details.url, connection?.origin);
    callback({ cancel: !allowed });
  });
  result.webRequest.onBeforeSendHeaders((details, callback) => {
    callback({ requestHeaders: hardenRequestHeaders(details.requestHeaders, details.url, connection?.origin, connection?.desktopToken) });
  });
  result.webRequest.onHeadersReceived((details, callback) => {
    const headers = {};
    for (const [key, value] of Object.entries(details.responseHeaders || {})) {
      if (!['content-security-policy', 'content-security-policy-report-only'].includes(key.toLowerCase())) headers[key] = value;
    }
    if (isTrustedURL(details.url, connection?.origin)) headers['Content-Security-Policy'] = [CSP];
    callback({ responseHeaders: headers });
  });
  result.on('will-download', (event, item, contents) => {
    if (contents !== window?.webContents || !isTrustedUIURL(contents.getURL(), connection?.origin) ||
        !isDownloadURL(item.getURL(), connection?.origin)) {
      event.preventDefault();
      return;
    }
    const filename = path.basename(item.getFilename()).replace(/[\x00-\x1f\x7f]/g, '').slice(0, 200) || 'artifact';
    item.setSaveDialogOptions({
      title: '保存任务文件', buttonLabel: '保存', defaultPath: path.join(app.getPath('downloads'), filename),
      properties: ['showOverwriteConfirmation', 'createDirectory'],
    });
    // A completed download is never opened or executed automatically.
  });
  return result;
}

function createWindow() {
  window = new BrowserWindow({
    width: 1400, height: 920, minWidth: 1024, minHeight: 680,
    title: 'Agent Forge', backgroundColor: '#f5f5f1', show: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      session: browserSession,
      nodeIntegration: false, nodeIntegrationInWorker: false, nodeIntegrationInSubFrames: false,
      contextIsolation: true, sandbox: true, webSecurity: true, allowRunningInsecureContent: false,
      webviewTag: false, safeDialogs: true, spellcheck: false,
    },
  });
  const current = window;
  current.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  current.webContents.on('will-attach-webview', event => event.preventDefault());
  const guardNavigation = (event, url) => {
    if (!isTrustedUIURL(url, connection?.origin)) {
      event.preventDefault();
      if (isTrustedUIURL(current.webContents.getURL(), connection?.origin) && isDownloadURL(url, connection?.origin)) {
        current.webContents.downloadURL(url);
      }
    }
  };
  current.webContents.on('will-navigate', guardNavigation);
  current.webContents.on('will-redirect', (event, url) => { if (!isTrustedUIURL(url, connection?.origin)) event.preventDefault(); });
  current.webContents.on('will-frame-navigate', (event, details) => {
    // Electron's event object itself carries these details in newer versions.
    const frame = details || event;
    if (!frame.isMainFrame) event.preventDefault();
  });
  current.webContents.on('render-process-gone', () => {
    if (!finalQuit && current === window) void showRendererFailure();
  });
  current.once('ready-to-show', () => current.show());
  current.on('close', event => {
    if (finalQuit) return;
    event.preventDefault();
    if (process.platform === 'darwin') current.hide();
    else void requestQuit();
  });
  current.on('closed', () => { if (window === current) window = null; });
  initialWindowLoad = current.loadURL(ready && connection ? connection.origin : bootURL);
  return initialWindowLoad;
}

async function showBootPage() {
  const current = window;
  if (!current) return;
  // A second loadURL for the same pending file can reject both loads with -3.
  // Keep real load failures visible to the startup error handler.
  await initialWindowLoad;
  if (current !== window || current.isDestroyed()) return;
  if (current.webContents.getURL() !== bootURL) await current.loadURL(bootURL);
}

async function showRendererFailure() {
  const result = await dialog.showMessageBox({
    type: 'error', title: '工作台需要重新打开', message: '工作台显示进程已停止，本地服务仍在后台运行。',
    buttons: ['重新打开工作台', '退出应用'], defaultId: 0, cancelId: 0,
  });
  if (result.response === 0 && window && connection) await window.loadURL(connection.origin);
  else if (result.response === 1) await requestQuit();
}

function validateSecrets(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value) ||
      ['ENCRYPTION_KEY', 'ADMIN_PASSWORD', 'SANDBOX_TOKEN'].some(key => typeof value[key] !== 'string' || !value[key] || value[key].length > 16384)) {
    throw new Error('应用密钥文件无效。请从完整备份恢复应用数据。');
  }
  return Object.fromEntries(['ENCRYPTION_KEY', 'ADMIN_PASSWORD', 'SANDBOX_TOKEN'].map(key => [key, value[key]]));
}

async function prepareData() {
  if (!safeStorage.isEncryptionAvailable()) throw new Error('macOS 钥匙串当前不可用。请解锁登录钥匙串后重新打开应用。');
  await fs.mkdir(app.getPath('userData'), { recursive: true, mode: 0o700 });
  let existing;
  try { existing = await fs.lstat(dataDir); } catch (error) { if (error.code !== 'ENOENT') throw error; }
  if (existing) {
    if (!existing.isDirectory() || existing.isSymbolicLink()) throw new Error('应用数据目录无效，请检查数据目录及备份。');
    try {
      const keyFile = path.join(dataDir, 'secrets.enc');
      const keyStat = await fs.lstat(keyFile);
      if (!keyStat.isFile() || keyStat.isSymbolicLink() || keyStat.size > 65536) throw new Error('Invalid key file');
      return validateSecrets(JSON.parse(safeStorage.decryptString(await fs.readFile(keyFile))));
    } catch { throw new Error('无法从 macOS 钥匙串解密现有应用密钥。请解锁原登录钥匙串；应用不会覆盖或重建现有数据。'); }
  }
  let source;
  const seedFile = app.isPackaged
    ? path.join(process.resourcesPath, 'backend', 'seed.json')
    : path.join(__dirname, 'seed.json');
  const bundledSeed = await fs.access(seedFile).then(() => true).catch(() => false);
  if (!app.isPackaged || !bundledSeed) {
    const choice = await dialog.showMessageBox(window, {
      type: 'question', title: '欢迎使用 Agent Forge', message: '为桌面应用准备独立的工作空间',
      detail: '可以复制现有本地配置与任务数据，或创建全新的工作空间。导入完成后，桌面应用使用自己的数据副本。',
      buttons: ['导入现有本地配置', '全新开始', '取消'], defaultId: 0, cancelId: 2, noLink: true,
    });
    if (choice.response === 2) return null;
    if (choice.response === 0) {
      const picked = await dialog.showOpenDialog(window, {
        title: '选择现有 Agent Forge 项目目录', message: '请选择包含 .local/config.json 的项目目录。',
        buttonLabel: '导入此工作空间', properties: ['openDirectory', 'dontAddToRecent'],
      });
      if (picked.canceled || picked.filePaths.length !== 1) return null;
      source = picked.filePaths[0];
    }
  }
  preparationAbort = new AbortController();
  let prepared;
  try {
    const seed = source ? {} : await fs.access(seedFile).then(() => ({ seed_file: seedFile })).catch(() => ({}));
    prepared = await runPreparation(command(), { data_dir: dataDir, ...seed, ...(source ? { import_from: source } : {}) }, { signal: preparationAbort.signal });
  } finally { preparationAbort = null; }
  const staging = prepared.staging_dir;
  const expectedParent = await fs.realpath(path.dirname(dataDir));
  if (!path.isAbsolute(staging) || !path.basename(staging).startsWith('.forge-prepare-') ||
      await fs.realpath(path.dirname(staging)) !== expectedParent) throw new Error('本地数据准备目录无效。');
  const stat = await fs.lstat(staging);
  if (!stat.isDirectory() || stat.isSymbolicLink()) throw new Error('本地数据准备目录无效。');
  const result = validateSecrets(prepared.secrets);
  await fs.writeFile(path.join(staging, 'secrets.enc'), safeStorage.encryptString(JSON.stringify(result)), { mode: 0o600, flag: 'wx' });
  await fs.chmod(staging, 0o700);
  // Never replace another workspace if an unexpected concurrent writer appeared.
  try {
    await fs.lstat(dataDir);
    throw new Error('应用数据目录已存在。请重新打开应用。');
  } catch (error) { if (error.code !== 'ENOENT') throw error; }
  await fs.rename(staging, dataDir);
  return result;
}

async function installSessionCookie() {
  const response = await backend.request('/api/desktop/session', { method: 'POST', privileged: true });
  const values = response.headers['set-cookie'];
  const cookie = (Array.isArray(values) ? values : values ? [values] : []).find(value => /^forge_session=/i.test(value));
  const match = cookie?.match(/^forge_session=([^;\s]+)(?:;|$)/i);
  if (!match || match[1].length > 4096) throw new Error('无法建立本地工作空间会话。');
  await browserSession.cookies.set({
    url: connection.origin, name: 'forge_session', value: match[1], path: '/',
    httpOnly: true, secure: false, sameSite: 'strict',
  });
}

async function launchBackend() {
  if (finalQuit || quitPending) return;
  ready = false;
  connection = null;
  await showBootPage();
  connection = await backend.start({ dataDir, secrets });
  await installSessionCookie();
  ready = true;
  if (window) await window.loadURL(connection.origin);
}

async function resumeSession() {
  if (!ready || quitPending || !backend?.connection) return;
  showWindow();
  const answer = await dialog.showMessageBox(window, {
    type: 'question', message: '重新进入这台 Mac 上的本地工作空间？',
    detail: '将使用 macOS 钥匙串中的应用身份建立新的本地会话。',
    buttons: ['重新进入', '取消'], defaultId: 0, cancelId: 1,
  });
  if (answer.response !== 0) return;
  try { await installSessionCookie(); await window.loadURL(connection.origin); }
  catch { await dialog.showMessageBox(window, { type: 'error', message: '无法重新建立本地会话，请重新打开应用。' }); }
}

async function showDataFolder() {
  const result = await shell.openPath(dataDir);
  if (result) throw new Error('无法打开应用数据目录。');
  return true;
}

function installIPC() {
  const handle = (name, fn) => ipcMain.handle(name, async (event, ...args) => {
    assertTrustedIPC(event, window?.webContents, connection?.origin);
    if (!ready || quitPending) throw new Error('应用尚未就绪。');
    return fn(...args);
  });
  let directoryPickerOpen = false;
  handle('forge:select-project-directory', async (...args) => {
    if (args.length || directoryPickerOpen) return null;
    directoryPickerOpen = true;
    try {
      const picked = await dialog.showOpenDialog(window, {
        title: '选择项目目录', buttonLabel: '使用此目录',
        properties: ['openDirectory', 'createDirectory', 'dontAddToRecent'],
      });
      if (picked.canceled || picked.filePaths.length !== 1 || quitPending) return null;
      const selected = picked.filePaths[0];
      const response = await backend.request('/api/desktop/authorize-directory', {
        method: 'POST', privileged: true, body: { path: selected },
      });
      if (typeof response.data?.ticket !== 'string' || !/^[A-Za-z0-9_-]{20,256}$/.test(response.data.ticket)) throw new Error('无法授权此项目目录。');
      return { path: selected, name: path.basename(selected), ticket: response.data.ticket };
    } finally { directoryPickerOpen = false; }
  });
  handle('forge:show-data-folder', async (...args) => {
    if (args.length) throw new Error('无效的应用请求。');
    return showDataFolder();
  });
  handle('forge:notify-task', async (...args) => {
    if (args.length !== 1) throw new Error('无效的任务通知。');
    const task = validateTaskNotification(args[0]);
    if (!Notification.isSupported()) return false;
    const now = Date.now();
    const key = `${task.id}:${task.status}`;
    for (const [oldKey, time] of notificationTimes) if (now - time > 300000) notificationTimes.delete(oldKey);
    while (recentNotifications.length && now - recentNotifications[0] > 60000) recentNotifications.shift();
    if (notificationTimes.has(key) || recentNotifications.length >= 5) return false;
    notificationTimes.set(key, now);
    recentNotifications.push(now);
    const labels = { completed: '任务已完成', failed: '任务失败', awaiting_approval: '任务等待审批', awaiting_decision: '任务等待决策', awaiting_acceptance: '任务等待验收', needs_attention: '任务需要关注' };
    const notification = new Notification({ title: labels[task.status], body: task.title, silent: false });
    notification.once('click', showWindow);
    notification.show();
    return true;
  });
}

async function requestQuit() {
  if (finalQuit || quitPending) return;
  quitPending = true;
  try {
    if (ready && backend?.connection) {
      let status;
      try { status = (await backend.request('/api/desktop/status', { privileged: true, timeout: 3000 })).data; } catch { status = null; }
      if (needsQuitConfirmation(status)) {
        const answer = await dialog.showMessageBox(window || undefined, {
          type: 'warning', title: '退出 Agent Forge',
          message: status ? '工作空间仍有进行中的任务。' : '暂时无法确认任务状态。',
          detail: '退出将停止此应用启动的本地服务，并中断尚未完成的执行。关闭窗口可以让任务继续在后台运行。',
          buttons: ['继续运行', '退出应用'], defaultId: 0, cancelId: 0, noLink: true,
        });
        if (answer.response !== 1) return;
      }
    }
    preparationAbort?.abort();
    await backend?.stop();
    if (startupTask) await startupTask.catch(() => {});
    await backend?.stop();
    connection = null;
    ready = false;
    secrets = null;
    await browserSession?.clearStorageData();
    finalQuit = true;
    app.quit();
  } finally { if (!finalQuit) quitPending = false; }
}

async function backendFailed() {
  ready = false;
  connection = null;
  if (finalQuit || quitPending || restarting) return;
  restarting = true;
  try {
    showWindow();
    await showBootPage();
    const answer = await dialog.showMessageBox(window, {
      type: 'error', title: '本地服务已停止', message: 'Agent Forge 的本地服务意外停止。',
      detail: '应用数据已保留。可以重新启动服务后查看任务状态。',
      buttons: ['重新启动服务', '退出应用'], defaultId: 0, cancelId: 1,
    });
    if (answer.response === 0) {
      try { await launchBackend(); } catch (error) { await startupFailed(error); }
    } else await requestQuit();
  } finally { restarting = false; }
}

async function startupFailed(error) {
  if (quitPending || finalQuit) return;
  await dialog.showMessageBox(window || undefined, {
    type: 'error', title: 'Agent Forge 无法启动',
    message: error instanceof Error ? error.message : '本地服务启动失败。',
    detail: '现有应用数据会保留。请检查磁盘空间、应用完整性及登录钥匙串后重新打开应用。',
    buttons: ['退出应用'],
  });
  // Avoid waiting on the startup promise from within that promise.
  setImmediate(() => { void requestQuit(); });
}

function installMenu() {
  const appMenu = {
    label: 'Agent Forge', submenu: [
      { role: 'about', label: '关于 Agent Forge' },
      { type: 'separator' },
      { label: '显示工作台', accelerator: 'CmdOrCtrl+1', click: showWindow },
      { label: '重新进入本地工作空间', click: () => { void resumeSession(); } },
      { label: '打开应用数据目录', click: () => { void showDataFolder().catch(() => {}); } },
      { type: 'separator' },
      ...(process.platform === 'darwin' ? [{ role: 'hide', label: '隐藏 Agent Forge' }, { role: 'hideOthers', label: '隐藏其他应用' }, { role: 'unhide', label: '显示全部' }, { type: 'separator' }] : []),
      { label: '退出 Agent Forge', accelerator: 'CmdOrCtrl+Q', click: () => { void requestQuit(); } },
    ],
  };
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    appMenu,
    { label: '编辑', submenu: [{ role: 'undo', label: '撤销' }, { role: 'redo', label: '重做' }, { type: 'separator' }, { role: 'cut', label: '剪切' }, { role: 'copy', label: '复制' }, { role: 'paste', label: '粘贴' }, { role: 'selectAll', label: '全选' }] },
    { label: '显示', submenu: [
      { label: '刷新工作台', accelerator: 'CmdOrCtrl+R', click: () => { if (ready) window?.webContents.reload(); } },
      { role: 'resetZoom', label: '实际大小' }, { role: 'zoomIn', label: '放大' }, { role: 'zoomOut', label: '缩小' },
      { type: 'separator' }, { role: 'togglefullscreen', label: '进入全屏' },
      ...(!app.isPackaged ? [{ role: 'toggleDevTools', label: '开发者工具' }] : []),
    ] },
    { label: '窗口', submenu: [{ role: 'minimize', label: '最小化' }, { role: 'zoom', label: '缩放' }, { label: '显示工作台', click: showWindow }] },
  ]));
}

if (!app.requestSingleInstanceLock()) {
  finalQuit = true;
  app.quit();
} else {
  app.on('second-instance', () => { if (app.isReady()) showWindow(); });
  app.on('activate', () => { if (app.isReady()) showWindow(); });
  app.on('window-all-closed', () => { if (process.platform !== 'darwin') void requestQuit(); });
  app.on('before-quit', event => { if (!finalQuit) { event.preventDefault(); void requestQuit(); } });
  app.on('web-contents-created', (_event, contents) => {
    contents.on('will-attach-webview', event => event.preventDefault());
  });
  void app.whenReady().then(() => {
    app.setAboutPanelOptions({ applicationName: 'Agent Forge', applicationVersion: app.getVersion(), copyright: '本地 AI 工作空间' });
    browserSession = createSession();
    backend = new BackendSupervisor(command());
    backend.on('unexpected-exit', () => { void backendFailed(); });
    installMenu();
    installIPC();
    const initialPage = createWindow();
    startupTask = (async () => {
      try {
        await initialPage;
        secrets = await prepareData();
        if (!secrets || quitPending) {
          if (!quitPending) setImmediate(() => { void requestQuit(); });
          return;
        }
        await launchBackend();
      } catch (error) { await startupFailed(error); }
    })();
  }).catch(error => { void startupFailed(error); });
}
