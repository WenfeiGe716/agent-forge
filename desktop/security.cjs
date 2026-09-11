'use strict';

const CSP = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data: blob:; font-src 'self' data:; object-src 'none'; frame-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'";

function isTrustedURL(value, origin) {
  if (typeof value !== 'string' || typeof origin !== 'string') return false;
  try {
    const url = new URL(value);
    const expected = new URL(origin);
    return expected.protocol === 'http:' && expected.hostname === '127.0.0.1' &&
      Boolean(expected.port) && url.origin === expected.origin &&
      !url.username && !url.password;
  } catch { return false; }
}

function isTrustedUIURL(value, origin) {
  if (!isTrustedURL(value, origin)) return false;
  return ['/', '/index.html'].includes(new URL(value).pathname);
}

function isDownloadURL(value, origin) {
  if (!isTrustedURL(value, origin)) return false;
  const pathname = new URL(value).pathname;
  return /^\/api\/runs\/[A-Za-z0-9_-]+\/artifacts\/[^/]+$/.test(pathname) ||
    /^\/api\/skills\/[A-Za-z0-9_-]+\/export$/.test(pathname);
}

function assertTrustedIPC(event, webContents, origin) {
  if (!webContents || webContents.isDestroyed() || event.sender !== webContents ||
      !event.senderFrame || event.senderFrame !== webContents.mainFrame ||
      !isTrustedUIURL(event.senderFrame.url, origin)) {
    throw new Error('此请求不来自受信任的应用窗口。');
  }
}

function hardenRequestHeaders(headers, url, origin, token) {
  const result = {};
  for (const [key, value] of Object.entries(headers || {})) {
    if (!['x-forge-desktop', 'x-forge-desktop-control'].includes(key.toLowerCase())) result[key] = value;
  }
  if (isTrustedURL(url, origin)) result['X-Forge-Desktop'] = token;
  return result;
}

function validateTaskNotification(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value) ||
      Object.keys(value).some(key => !['id', 'title', 'status'].includes(key))) {
    throw new Error('无效的任务通知。');
  }
  if (typeof value.id !== 'string' || !/^[A-Za-z0-9_.:-]{1,128}$/.test(value.id) ||
      typeof value.title !== 'string' || !value.title.trim() || value.title.length > 180 ||
      /[\x00-\x1f\x7f]/.test(value.title) ||
      !['completed', 'failed', 'awaiting_approval', 'awaiting_decision', 'awaiting_acceptance', 'needs_attention'].includes(value.status)) {
    throw new Error('无效的任务通知。');
  }
  return { id: value.id, title: value.title.trim(), status: value.status };
}

function countPending(value) {
  if (Array.isArray(value)) return value.length;
  if (Number.isInteger(value) && value >= 0) return value;
  return null;
}

function needsQuitConfirmation(status) {
  if (!status || typeof status !== 'object') return true;
  const active = countPending(status.active_runs);
  const prompts = status.prompts === undefined ? 0 : countPending(status.prompts);
  return active === null || prompts === null || active > 0 || prompts > 0;
}

module.exports = { CSP, isTrustedURL, isTrustedUIURL, isDownloadURL, assertTrustedIPC, hardenRequestHeaders, validateTaskNotification, needsQuitConfirmation };
