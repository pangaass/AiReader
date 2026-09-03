const { app, BrowserWindow, dialog, ipcMain } = require('electron');
const { spawn } = require('node:child_process');
const fs = require('node:fs/promises');
const fsSync = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { pathToFileURL } = require('node:url');
const { getSettings, saveSettings } = require('./settings-store');

const APP_ROOT = __dirname;
const PROJECT_ROOT = path.resolve(APP_ROOT, '..');
const ARTIFACTS_ROOT = path.join(PROJECT_ROOT, 'artifacts');
const OUTPUT_ROOT = path.join(PROJECT_ROOT, 'output');
const PYTHON = path.join(PROJECT_ROOT, '.venv', 'bin', 'python');
const EXTRACTOR = path.join(APP_ROOT, 'python', 'extract_pdf.py');
const VISUALIZER_PRELOAD = path.join(APP_ROOT, 'visualizer-preload.js');

let mainWindow;
let testArtifactsWritten = false;
let activeBuild = null;

function existingFile(filePath) {
  return typeof filePath === 'string' && fsSync.existsSync(filePath) && fsSync.statSync(filePath).isFile();
}

function resolveArtifactPath(value) {
  if (!value || typeof value !== 'string') return null;
  const candidate = path.isAbsolute(value) ? value : path.resolve(PROJECT_ROOT, value);
  return existingFile(candidate) ? candidate : null;
}

async function readJson(filePath) {
  try {
    return JSON.parse(await fs.readFile(filePath, 'utf8'));
  } catch {
    return null;
  }
}

async function generatedPaperCatalog() {
  let entries = [];
  try {
    entries = await fs.readdir(ARTIFACTS_ROOT, { withFileTypes: true });
  } catch {
    return [];
  }
  const papers = [];
  for (const entry of entries.filter((item) => item.isDirectory())) {
    const artifactRoot = path.join(ARTIFACTS_ROOT, entry.name);
    const manifest = await readJson(path.join(artifactRoot, 'manifest.json'));
    if (!manifest || !['built', 'preview', 'review_failed'].includes(manifest.status)) continue;
    const irPath = resolveArtifactPath(manifest.artifacts?.ir) || path.join(artifactRoot, 'ir', 'paper_ir.json');
    const ir = await readJson(irPath);
    const pdfPath = resolveArtifactPath(ir?.source?.local_pdf) || path.join(artifactRoot, 'source.pdf');
    const visualizerPath = resolveArtifactPath(manifest.artifacts?.html)
      || resolveArtifactPath(path.join(OUTPUT_ROOT, `${manifest.paper_id || entry.name}-visualizer.html`));
    if (!existingFile(pdfPath)) continue;
    papers.push({
      id: manifest.paper_id || entry.name,
      title: ir?.paper?.title || entry.name,
      path: pdfPath,
      visualizerPath,
      irPath: existingFile(irPath) ? irPath : null,
      folder: path.basename(path.dirname(pdfPath)),
      generated: Boolean(visualizerPath),
      buildStatus: manifest.status,
    });
  }
  papers.sort((a, b) => a.title.localeCompare(b.title));
  const preferred = process.env.PAPER_READER_INITIAL_PDF;
  if (preferred) {
    const index = papers.findIndex((paper) => path.resolve(paper.path) === path.resolve(preferred));
    if (index >= 0) {
      const selected = papers.splice(index, 1)[0];
      selected.preferred = true;
      papers.unshift(selected);
    } else if (existingFile(preferred) && path.extname(preferred).toLowerCase() === '.pdf') {
      const stem = path.basename(preferred, path.extname(preferred));
      papers.unshift({ id: paperSlug(stem), title: stem, path: path.resolve(preferred), preferred: true, generated: false });
    }
  }
  return papers;
}

async function companionForPdf(filePath) {
  const resolved = path.resolve(filePath);
  const catalog = await generatedPaperCatalog();
  const exact = catalog.find((paper) => path.resolve(paper.path) === resolved);
  if (exact) return exact;
  const stem = path.basename(resolved, path.extname(resolved));
  const visualizerPath = [
    path.join(path.dirname(resolved), `${stem}-visualizer.html`),
    path.join(OUTPUT_ROOT, `${stem}-visualizer.html`),
  ].find(existingFile) || null;
  const irPath = [
    path.join(path.dirname(resolved), 'ir', 'paper_ir.json'),
    path.join(ARTIFACTS_ROOT, stem, 'ir', 'paper_ir.json'),
  ].find(existingFile) || null;
  return { id: stem, title: stem, path: resolved, visualizerPath, irPath, generated: Boolean(visualizerPath) };
}

async function scanPdfFolder(root, depth = 0) {
  if (depth > 5) return [];
  let entries;
  try {
    entries = await fs.readdir(root, { withFileTypes: true });
  } catch {
    return [];
  }
  const results = [];
  for (const entry of entries) {
    if (entry.name.startsWith('.') || entry.name === 'node_modules') continue;
    const fullPath = path.join(root, entry.name);
    if (entry.isDirectory()) results.push(...await scanPdfFolder(fullPath, depth + 1));
    if (entry.isFile() && path.extname(entry.name).toLowerCase() === '.pdf') results.push(fullPath);
    if (results.length >= 250) break;
  }
  return results.slice(0, 250);
}

function buildSourceMap(ir) {
  if (!ir || !Array.isArray(ir.evidence)) return {};
  const sizes = new Map((ir.pages || []).map((page) => [Number(page.number), page]));
  return Object.fromEntries(ir.evidence.flatMap((item) => {
    const locator = item.locator || {};
    const page = Number(locator.page);
    const size = sizes.get(page);
    const bbox = locator.bbox;
    if (!item.id || !page) return [];
    let rects = [];
    if (size?.width && size?.height && Array.isArray(bbox) && bbox.length === 4) {
      rects = [{
        x: Math.max(0, bbox[0] / size.width),
        y: Math.max(0, bbox[1] / size.height),
        width: Math.min(1, Math.max(0, (bbox[2] - bbox[0]) / size.width)),
        height: Math.min(1, Math.max(0, (bbox[3] - bbox[1]) / size.height)),
      }];
    }
    return [[item.id, {
      evidenceId: item.id,
      page,
      rects,
      text: item.verbatim_text || '',
      blockIds: locator.block_ids || [],
    }]];
  }));
}

function runExtractor(filePath, llm = {}) {
  return new Promise((resolve, reject) => {
    const env = { ...process.env };
    if (llm.apiKey) env.PAPER_READER_LLM_KEY = llm.apiKey;
    if (llm.endpoint) env.PAPER_READER_LLM_ENDPOINT = llm.endpoint;
    if (llm.model) env.PAPER_READER_LLM_MODEL = llm.model;
    const args = [EXTRACTOR, filePath];
    if (llm.enabled) args.push('--llm');
    const child = spawn(PYTHON, args, { env, stdio: ['ignore', 'pipe', 'pipe'] });
    const stdout = [];
    const stderr = [];
    child.stdout.on('data', (chunk) => stdout.push(chunk));
    child.stderr.on('data', (chunk) => stderr.push(chunk));
    child.on('error', reject);
    child.on('close', (code) => {
      if (code !== 0) {
        reject(new Error(Buffer.concat(stderr).toString('utf8') || `解析进程退出：${code}`));
        return;
      }
      try {
        resolve(JSON.parse(Buffer.concat(stdout).toString('utf8')));
      } catch (error) {
        reject(new Error(`解析结果无效：${error.message}`));
      }
    });
  });
}

async function loadPaper(filePath, llm) {
  if (typeof filePath !== 'string' || path.extname(filePath).toLowerCase() !== '.pdf') throw new Error('只支持 PDF 文件');
  const stat = await fs.stat(filePath);
  if (!stat.isFile()) throw new Error('PDF 路径无效');
  const companion = await companionForPdf(filePath);
  const [analysis, data, ir] = await Promise.all([
    runExtractor(filePath, llm),
    fs.readFile(filePath),
    companion.irPath ? readJson(companion.irPath) : Promise.resolve(null),
  ]);
  return {
    analysis,
    pdfData: new Uint8Array(data),
    filePath,
    paperId: companion.id,
    visualizerUrl: companion.visualizerPath ? pathToFileURL(companion.visualizerPath).href : null,
    visualizerPath: companion.visualizerPath,
    sourceMap: buildSourceMap(ir),
  };
}

function paperSlug(value) {
  const slug = String(value || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 64);
  return slug || `paper-${crypto.createHash('sha256').update(String(value || 'paper')).digest('hex').slice(0, 8)}`;
}

async function buildVisualizer(filePath, requestedPaperId) {
  if (activeBuild) throw new Error('已有 Visualizer 构建任务正在运行');
  if (!existingFile(filePath) || path.extname(filePath).toLowerCase() !== '.pdf') throw new Error('PDF 路径无效');
  let paperId = paperSlug(requestedPaperId || path.basename(filePath, path.extname(filePath)));
  const sourceHash = crypto.createHash('sha256').update(await fs.readFile(filePath)).digest('hex');
  const existingManifest = await readJson(path.join(ARTIFACTS_ROOT, paperId, 'manifest.json'));
  const outputCollision = existingFile(path.join(OUTPUT_ROOT, `${paperId}-visualizer.html`));
  if ((existingManifest?.source_sha256 && existingManifest.source_sha256 !== sourceHash) || (!existingManifest && outputCollision)) {
    paperId = `${paperId.slice(0, 55)}-${sourceHash.slice(0, 8)}`;
  }
  const settings = await getSettings();
  const env = { ...process.env };
  if (settings.endpoint) env.OPENAI_BASE_URL = settings.endpoint;
  if (settings.apiKey) env.OPENAI_API_KEY = settings.apiKey;
  if (settings.model) {
    env.OPENAI_MODEL = settings.model;
    env.PAPER_READER_LLM_MODEL = settings.model;
  }
  const args = [
    '-m', 'paper_visualizer', filePath,
    '--project-root', PROJECT_ROOT,
    '--paper-id', paperId,
    '--progress-json',
  ];
  if (settings.apiKey && settings.model) args.push('--llm');
  return new Promise((resolve, reject) => {
    const child = spawn(PYTHON, args, { cwd: PROJECT_ROOT, env, stdio: ['ignore', 'pipe', 'pipe'] });
    activeBuild = child;
    const stdout = [];
    const stderr = [];
    let lineBuffer = '';
    const sendProgress = (payload) => mainWindow?.webContents.send('visualizer:progress', { paperId, ...payload });
    sendProgress({ stage: 'starting', status: 'running' });
    child.stdout.on('data', (chunk) => stdout.push(chunk));
    child.stderr.on('data', (chunk) => {
      const text = chunk.toString('utf8');
      stderr.push(Buffer.from(text));
      lineBuffer += text;
      const lines = lineBuffer.split('\n');
      lineBuffer = lines.pop() || '';
      for (const line of lines) {
        if (!line.startsWith('PAPER_VISUALIZER_PROGRESS ')) continue;
        try { sendProgress(JSON.parse(line.slice('PAPER_VISUALIZER_PROGRESS '.length))); }
        catch { /* Ignore malformed progress without interrupting the build. */ }
      }
    });
    child.on('error', (error) => {
      activeBuild = null;
      sendProgress({ stage: 'failed', status: 'failed', message: error.message });
      reject(error);
    });
    child.on('close', async (code) => {
      activeBuild = null;
      if (code !== 0) {
        const message = Buffer.concat(stderr).toString('utf8')
          .split('\n').filter((line) => line && !line.startsWith('PAPER_VISUALIZER_PROGRESS ')).slice(-8).join('\n');
        sendProgress({ stage: 'failed', status: 'failed', message: message || `构建进程退出：${code}` });
        reject(new Error(message || `Visualizer 构建失败：${code}`));
        return;
      }
      try {
        const manifest = JSON.parse(Buffer.concat(stdout).toString('utf8'));
        const visualizerPath = resolveArtifactPath(manifest.artifacts?.html);
        const irPath = resolveArtifactPath(manifest.artifacts?.ir);
        const ir = irPath ? await readJson(irPath) : null;
        const paperPath = resolveArtifactPath(ir?.source?.local_pdf) || path.join(ARTIFACTS_ROOT, paperId, 'source.pdf');
        if (!visualizerPath || !existingFile(paperPath)) throw new Error('构建完成但产物不完整');
        resolve({
          paperId,
          status: manifest.status,
          paperPath,
          visualizerPath,
          visualizerUrl: pathToFileURL(visualizerPath).href,
        });
      } catch (error) {
        reject(new Error(`无法读取构建产物：${error.message}`));
      }
    });
  });
}

function registerIpc() {
  ipcMain.handle('settings:get', () => getSettings());
  ipcMain.handle('settings:save', (_event, settings) => saveSettings(settings));
  ipcMain.handle('visualizer:build', (_event, filePath, paperId) => buildVisualizer(filePath, paperId));
  ipcMain.handle('library:samples', () => generatedPaperCatalog());
  ipcMain.handle('library:choose-folder', async () => {
    const result = await dialog.showOpenDialog(mainWindow, { title: '选择论文文件夹', properties: ['openDirectory'] });
    if (result.canceled) return null;
    const folder = result.filePaths[0];
    const paths = await scanPdfFolder(folder);
    const papers = await Promise.all(paths.map(companionForPdf));
    return { folder, papers };
  });
  ipcMain.handle('paper:choose', async () => {
    const result = await dialog.showOpenDialog(mainWindow, {
      title: '选择论文 PDF', properties: ['openFile'], filters: [{ name: 'PDF 论文', extensions: ['pdf'] }],
    });
    return result.canceled ? null : result.filePaths[0];
  });
  ipcMain.handle('paper:load', (_event, filePath, llm) => loadPaper(filePath, llm));
  ipcMain.on('ui:ready', async () => {
    if (testArtifactsWritten) return;
    const screenshotPath = process.env.PAPER_READER_SCREENSHOT;
    const reportPath = process.env.PAPER_READER_TEST_REPORT;
    if ((!screenshotPath && !reportPath) || !mainWindow) return;
    testArtifactsWritten = true;
    if (reportPath) {
      let buildResult = null;
      if (process.env.PAPER_READER_TEST_BUILD === '1') {
        buildResult = await mainWindow.webContents.executeJavaScript('window.__paperReaderTest?.buildCurrentVisualizer()');
      }
      const report = await mainWindow.webContents.executeJavaScript(`
        new Promise(async (resolve) => {
          const result = await window.__paperReaderTest?.selectFirstVisualizerEvidence();
          const targetResults = await window.__paperReaderTest?.testVisualizerTargets();
          const linkAudit = await window.__paperReaderTest?.auditVisualizerTargets();
          const sidebarButton = document.querySelector('#sidebar-toggle');
          sidebarButton?.click();
          await new Promise((done) => setTimeout(done, 250));
          const sidebar = document.querySelector('.sidebar');
          const collapsedSidebarWidth = sidebar ? Math.round(sidebar.getBoundingClientRect().width) : -1;
          sidebarButton?.click();
          await new Promise((done) => setTimeout(done, 250));
          setTimeout(() => {
            const highlights = [...document.querySelectorAll('.highlight.visible')];
            resolve({
              libraryItems: document.querySelectorAll('.paper-item').length,
              pdfPages: document.querySelectorAll('.pdf-page').length,
              visualizerLoaded: !document.querySelector('#visualizer-view')?.classList.contains('hidden')
                && Boolean(document.querySelector('#visualizer-view')?.getURL()),
              selectedEvidenceId: document.body.dataset.selectedEvidenceId || null,
              highlightedPage: highlights[0]?.parentElement?.dataset.page || null,
              highlightCount: highlights.length,
              sourceRequest: result || null,
              targetResults: targetResults || [],
              linkAudit,
              collapsedSidebarWidth,
              llmSettings: {
                endpoint: document.querySelector('#api-endpoint')?.value || null,
                model: document.querySelector('#model-name')?.value || null,
                tokenStored: document.querySelector('#api-key')?.placeholder === '已安全保存',
              },
              buildButton: {
                text: document.querySelector('#build-button')?.textContent || null,
                enabled: !document.querySelector('#build-button')?.disabled,
              },
              buildResult: ${JSON.stringify(buildResult)},
            });
          }, 1000);
        })
      `);
      await fs.writeFile(reportPath, JSON.stringify(report, null, 2));
    }
    if (screenshotPath) {
      const image = await mainWindow.webContents.capturePage();
      await fs.writeFile(screenshotPath, image.toPNG());
    }
    app.quit();
  });
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1800, height: 1080, minWidth: 1180, minHeight: 720,
    title: 'Paper Visualizer Reader', backgroundColor: '#f3f1ec',
    webPreferences: {
      preload: path.join(APP_ROOT, 'preload.js'), contextIsolation: true,
      nodeIntegration: false, sandbox: true, webviewTag: true,
    },
  });
  mainWindow.webContents.on('will-attach-webview', (event, webPreferences, params) => {
    if (webPreferences.preload !== VISUALIZER_PRELOAD || !String(params.src || '').startsWith('file://')) {
      event.preventDefault();
      return;
    }
    webPreferences.nodeIntegration = false;
    webPreferences.contextIsolation = true;
    webPreferences.sandbox = true;
  });
  mainWindow.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  mainWindow.loadFile(path.join(APP_ROOT, 'index.html'));
}

app.whenReady().then(() => {
  registerIpc();
  createWindow();
  app.on('activate', () => { if (BrowserWindow.getAllWindows().length === 0) createWindow(); });
});

app.on('window-all-closed', () => { if (process.platform !== 'darwin') app.quit(); });
app.on('before-quit', () => { if (activeBuild && !activeBuild.killed) activeBuild.kill(); });
