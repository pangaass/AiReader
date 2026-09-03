const { app, safeStorage } = require('electron');
const fs = require('node:fs/promises');
const path = require('node:path');

const DEFAULTS = {
  endpoint: 'https://api.openai.com/v1',
  model: 'gpt-5.4-mini',
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
  };
}

async function saveSettings(input) {
  const endpoint = normalizeEndpoint(input?.endpoint);
  const model = normalizeModel(input?.model);
  const apiKey = String(input?.apiKey || '').trim();
  if (!apiKey) throw new Error('Token 不能为空');
  if (!safeStorage.isEncryptionAvailable()) throw new Error('系统安全存储当前不可用');
  const payload = {
    endpoint,
    model,
    encryptedToken: safeStorage.encryptString(apiKey).toString('base64'),
    updatedAt: new Date().toISOString(),
  };
  const target = settingsPath();
  const temporary = `${target}.tmp`;
  await fs.mkdir(path.dirname(target), { recursive: true });
  await fs.writeFile(temporary, JSON.stringify(payload, null, 2), { encoding: 'utf8', mode: 0o600 });
  await fs.rename(temporary, target);
  await fs.chmod(target, 0o600);
  return { endpoint, model, hasToken: true };
}

module.exports = { DEFAULTS, getSettings, normalizeEndpoint, normalizeModel, saveSettings, settingsPath };
