const { app, safeStorage } = require('electron');
const fs = require('node:fs/promises');
const path = require('node:path');

const DEFAULTS = {
  endpoint: 'https://api.openai.com/v1',
  model: 'gpt-5.4-mini',
  theme: 'system',
  density: 'comfortable',
  sidebarVisible: true,
  pdfZoom: 1.2,
  reopenLast: true,
};

function settingsPath() {
  return path.join(app.getPath('appData'), 'Paper Visualizer Reader', 'llm-settings.json');
}

function normalizeEndpoint(value) {
  const endpoint = String(value || '').trim().replace(/\/+$/, '');
  let parsed;
  try { parsed = new URL(endpoint); }
  catch { throw new Error('Endpoint 不是有效 URL'); }
  const localHttp = parsed.protocol === 'http:' && ['localhost', '127.0.0.1', '::1'].includes(parsed.hostname);
  if (parsed.protocol !== 'https:' && !localHttp) throw new Error('Endpoint 必须使用 HTTPS（本机地址可使用 HTTP）');
  if (parsed.username || parsed.password) throw new Error('Endpoint 不能包含账号或密码');
  return endpoint;
}

function normalizeModel(value) {
  const model = String(value || '').trim();
  if (!model || model.length > 160) throw new Error('Model Name 无效');
  return model;
}

function normalizeChoice(value, choices, fallback) {
  return choices.includes(value) ? value : fallback;
}

function normalizeZoom(value) {
  const zoom = Number(value);
  if (!Number.isFinite(zoom)) return DEFAULTS.pdfZoom;
  return Math.min(1.8, Math.max(0.8, Math.round(zoom * 10) / 10));
}

async function getSettings() {
  let saved = {};
  try { saved = JSON.parse(await fs.readFile(settingsPath(), 'utf8')); }
  catch { saved = {}; }
  let apiKey = '';
  if (saved.encryptedToken && safeStorage.isEncryptionAvailable()) {
    try { apiKey = safeStorage.decryptString(Buffer.from(saved.encryptedToken, 'base64')); }
    catch { apiKey = ''; }
  }
  return {
    endpoint: saved.endpoint || process.env.OPENAI_BASE_URL || DEFAULTS.endpoint,
    apiKey: apiKey || process.env.OPENAI_API_KEY || '',
    model: saved.model || process.env.PAPER_READER_LLM_MODEL || DEFAULTS.model,
    theme: normalizeChoice(saved.theme, ['system', 'light', 'dark'], DEFAULTS.theme),
    density: normalizeChoice(saved.density, ['comfortable', 'compact'], DEFAULTS.density),
    sidebarVisible: saved.sidebarVisible !== false,
    pdfZoom: normalizeZoom(saved.pdfZoom),
    reopenLast: saved.reopenLast !== false,
  };
}

async function saveSettings(input) {
  const endpoint = normalizeEndpoint(input?.endpoint);
  const model = normalizeModel(input?.model);
  const apiKey = String(input?.apiKey || '').trim();
  let existing = {};
  try { existing = JSON.parse(await fs.readFile(settingsPath(), 'utf8')); }
  catch { existing = {}; }
  if (apiKey && !safeStorage.isEncryptionAvailable()) throw new Error('系统安全存储当前不可用');
  const encryptedToken = apiKey
    ? safeStorage.encryptString(apiKey).toString('base64')
    : existing.encryptedToken;
  const payload = {
    endpoint,
    model,
    theme: normalizeChoice(input?.theme, ['system', 'light', 'dark'], DEFAULTS.theme),
    density: normalizeChoice(input?.density, ['comfortable', 'compact'], DEFAULTS.density),
    sidebarVisible: input?.sidebarVisible !== false,
    pdfZoom: normalizeZoom(input?.pdfZoom),
    reopenLast: input?.reopenLast !== false,
    updatedAt: new Date().toISOString(),
  };
  if (encryptedToken) payload.encryptedToken = encryptedToken;
  const target = settingsPath();
  const temporary = `${target}.tmp`;
  await fs.mkdir(path.dirname(target), { recursive: true });
  await fs.writeFile(temporary, JSON.stringify(payload, null, 2), { encoding: 'utf8', mode: 0o600 });
  await fs.rename(temporary, target);
  await fs.chmod(target, 0o600);
  return { ...payload, encryptedToken: undefined, hasToken: Boolean(encryptedToken) };
}

module.exports = { DEFAULTS, getSettings, normalizeChoice, normalizeEndpoint, normalizeModel, normalizeZoom, saveSettings, settingsPath };
