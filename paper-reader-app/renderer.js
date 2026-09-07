import * as pdfjsLib from './node_modules/pdfjs-dist/build/pdf.mjs';
import { highlightStyle, normalizeLibrary, resolveSourceTarget, visibleBlockCount } from './app-state.mjs';

pdfjsLib.GlobalWorkerOptions.workerSrc = new URL('./node_modules/pdfjs-dist/build/pdf.worker.mjs', import.meta.url).href;

const elements = Object.fromEntries([
  'paper-title', 'paper-meta', 'paper-count', 'paper-list', 'library-folder', 'outline-count', 'outline',
  'visualizer-status', 'visualizer-placeholder', 'visualizer-view', 'build-button', 'build-progress',
  'build-title', 'build-stage', 'build-progress-bar', 'build-progress-detail', 'pdf-page-label', 'source-hint',
  'source-kind', 'source-text', 'pdf-scroll', 'pdf-placeholder', 'pdf-pages', 'zoom-out', 'zoom-in',
  'zoom-label', 'loading', 'loading-text', 'open-button', 'folder-button', 'sidebar-toggle', 'llm-button',
  'llm-dialog', 'api-endpoint', 'api-key', 'model-name', 'theme-setting', 'density-setting',
  'sidebar-setting', 'pdf-zoom-setting', 'reopen-setting', 'enable-llm', 'toast',
].map((id) => [id, document.getElementById(id)]));

const state = {
  library: [], currentPath: null, currentPaperId: null, analysis: null, sourceMap: {}, pdf: null,
  visualizerUrl: null, visualizerReady: false, zoom: 1.2, renderGeneration: 0,
  selectedSource: null, sidebarHidden: false, building: false,
  llm: { enabled: false, endpoint: 'https://api.openai.com/v1', apiKey: '', model: 'gpt-5.4-mini' },
  preferences: { theme: 'system', density: 'comfortable', sidebarVisible: true, pdfZoom: 1.2, reopenLast: true },
};

function resolvedTheme(theme) {
  if (theme === 'light' || theme === 'dark') return theme;
  return matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

function setSidebarHidden(hidden) {
  state.sidebarHidden = Boolean(hidden);
  document.body.classList.toggle('sidebar-hidden', state.sidebarHidden);
  elements['sidebar-toggle'].setAttribute('aria-pressed', String(state.sidebarHidden));
  elements['sidebar-toggle'].setAttribute('aria-label', state.sidebarHidden ? '显示论文目录' : '隐藏论文目录');
}

function applyPreferences(settings) {
  state.preferences = {
    theme: settings.theme || 'system', density: settings.density || 'comfortable',
    sidebarVisible: settings.sidebarVisible !== false, pdfZoom: Number(settings.pdfZoom) || 1.2,
    reopenLast: settings.reopenLast !== false,
  };
  document.body.dataset.theme = resolvedTheme(state.preferences.theme);
  document.body.dataset.density = state.preferences.density;
  state.zoom = state.preferences.pdfZoom;
  setSidebarHidden(!state.preferences.sidebarVisible);
  elements['zoom-label'].textContent = `${Math.round(state.zoom * 100)}%`;
}

async function applyVisualizerPreferences() {
  if (!state.visualizerReady) return;
  const theme = resolvedTheme(state.preferences.theme);
  await elements['visualizer-view'].executeJavaScript(`(() => {
    document.documentElement.dataset.theme = ${JSON.stringify(theme)};
    document.documentElement.dataset.density = ${JSON.stringify(state.preferences.density)};
  })()`);
}

function showToast(message) {
  elements.toast.textContent = message;
  elements.toast.classList.remove('hidden');
  clearTimeout(showToast.timeout);
  showToast.timeout = setTimeout(() => elements.toast.classList.add('hidden'), 3200);
}

function setLoading(active, message = '') {
  elements.loading.classList.toggle('hidden', !active);
  if (message) elements['loading-text'].textContent = message;
}

function recentPaths() {
  try { return JSON.parse(localStorage.getItem('paper-reader-recent') || '[]'); }
  catch { return []; }
}

function saveRecent(filePath) {
  const paths = [filePath, ...recentPaths().filter((item) => item !== filePath)].slice(0, 12);
  localStorage.setItem('paper-reader-recent', JSON.stringify(paths));
}

function renderLibrary() {
  elements['paper-list'].replaceChildren();
  elements['paper-count'].textContent = String(state.library.length);
  state.library.forEach((paper) => {
    const button = document.createElement('button');
    button.className = `paper-item${paper.path === state.currentPath ? ' active' : ''}`;
    const thumb = document.createElement('span');
    thumb.className = 'paper-thumb';
    thumb.textContent = paper.generated || paper.visualizerPath ? 'VIS' : 'PDF';
    const info = document.createElement('span');
    const title = document.createElement('strong');
    title.textContent = paper.title;
    const meta = document.createElement('small');
    meta.textContent = paper.generated || paper.visualizerPath ? '可视化已就绪' : '仅原文';
    info.append(title, meta);
    button.append(thumb, info);
    button.addEventListener('click', () => loadPaper(paper.path));
    elements['paper-list'].append(button);
  });
}

function renderOutline() {
  const outline = state.analysis?.outline || [];
  elements.outline.replaceChildren();
  elements['outline-count'].textContent = String(outline.length);
  for (const item of outline) {
    const button = document.createElement('button');
    button.textContent = item.title;
    button.style.paddingLeft = `${8 + Math.max(0, item.level - 1) * 10}px`;
    button.addEventListener('click', () => {
      const block = state.analysis.blocks.find((candidate) => candidate.id === item.block_id)
        || state.analysis.blocks.find((candidate) => candidate.page === item.page);
      if (block) selectBlock(block);
      else activateSource({ kind: 'page', page: item.page, label: item.title });
    });
    elements.outline.append(button);
  }
}

function clearVisualizer() {
  state.visualizerReady = false;
  elements['visualizer-view'].classList.add('hidden');
  elements['visualizer-placeholder'].classList.remove('hidden');
  elements['visualizer-status'].textContent = '未找到可视化';
  elements['build-button'].textContent = '构建 Visualizer';
  elements['build-button'].disabled = !state.analysis;
  elements['visualizer-view'].removeAttribute('src');
}

function loadVisualizer(url) {
  state.visualizerUrl = url;
  if (!url) {
    clearVisualizer();
    return;
  }
  state.visualizerReady = false;
  elements['build-button'].textContent = '已构建';
  elements['build-button'].disabled = true;
  elements['visualizer-status'].textContent = '正在载入';
  elements['visualizer-placeholder'].classList.add('hidden');
  elements['visualizer-view'].classList.remove('hidden');
  elements['visualizer-view'].src = url;
}

const BUILD_STAGES = {
  starting: ['准备 Agent 流水线', 3],
  parse: ['解析 PDF 与版面', 12],
  model: ['构建论文知识模型', 27],
  content: ['内容智能体组织论文故事', 40],
  visual: ['设计可视化表达', 54],
  related: ['组织 Related Work', 66],
  page: ['生成页面模型', 78],
  render: ['渲染交互页面', 88],
  review: ['独立审核产物', 96],
  complete: ['构建完成', 100],
  failed: ['构建失败', 100],
};

function updateBuildProgress(payload) {
  if (!state.building || (payload.paperId && payload.paperId !== state.currentPaperId)) return;
  const [label, percent] = BUILD_STAGES[payload.stage] || [payload.stage || '正在处理', 5];
  elements['build-stage'].textContent = payload.status === 'completed' && payload.stage !== 'complete' ? `${label}完成` : `${label}…`;
  elements['build-progress-bar'].style.width = `${percent}%`;
  if (payload.cache_hit) elements['build-progress-detail'].textContent = '已复用验证过的阶段缓存，继续下一阶段';
}

async function buildCurrentVisualizer() {
  if (!state.currentPath || state.building) return null;
  state.building = true;
  elements['build-button'].disabled = true;
  elements['open-button'].disabled = true;
  elements['folder-button'].disabled = true;
  elements['llm-button'].disabled = true;
  elements['build-progress'].classList.remove('hidden');
  elements['build-title'].textContent = '正在构建 Visualizer';
  elements['build-stage'].textContent = '准备 Agent 流水线…';
  elements['build-progress-bar'].style.width = '3%';
  elements['build-progress-bar'].classList.remove('failed');
  elements['build-progress-detail'].textContent = '解析 → 建模 → 论文故事 → 可视化 → 页面生成 → 审核';
  try {
    const result = await window.paperReader.buildVisualizer(state.currentPath, state.currentPaperId);
    elements['build-title'].textContent = 'Visualizer 构建完成';
    elements['build-stage'].textContent = result.status === 'built' ? '已通过审核' : '已生成预览版本';
    elements['build-progress-bar'].style.width = '100%';
    const samples = await window.paperReader.getSamples();
    state.library = normalizeLibrary(samples, recentPaths());
    renderLibrary();
    await new Promise((resolve) => setTimeout(resolve, 450));
    elements['build-progress'].classList.add('hidden');
    state.building = false;
    await loadPaper(result.paperPath);
    showToast('Visualizer 已构建并载入');
    return result;
  } catch (error) {
    elements['build-title'].textContent = '构建失败';
    elements['build-stage'].textContent = error.message;
    elements['build-progress-detail'].textContent = '可以关闭提示后重试；已有 PDF 解析不会丢失。';
    elements['build-progress-bar'].style.width = '100%';
    elements['build-progress-bar'].classList.add('failed');
    showToast(`Visualizer 构建失败：${error.message}`);
    return null;
  } finally {
    state.building = false;
    elements['build-button'].disabled = Boolean(state.visualizerUrl);
    elements['open-button'].disabled = false;
    elements['folder-button'].disabled = false;
    elements['llm-button'].disabled = false;
  }
}

async function renderPdf() {
  const generation = ++state.renderGeneration;
  elements['pdf-pages'].replaceChildren();
  elements['pdf-placeholder'].classList.add('hidden');
  elements['zoom-label'].textContent = `${Math.round(state.zoom * 100)}%`;
  for (let pageNumber = 1; pageNumber <= state.pdf.numPages; pageNumber += 1) {
    const pdfPage = await state.pdf.getPage(pageNumber);
    if (generation !== state.renderGeneration) return;
    const viewport = pdfPage.getViewport({ scale: state.zoom });
    const page = document.createElement('div');
    page.className = 'pdf-page';
    page.dataset.page = String(pageNumber);
    page.style.width = `${viewport.width}px`;
    page.style.height = `${viewport.height}px`;
    const canvas = document.createElement('canvas');
    const pixelRatio = window.devicePixelRatio || 1;
    canvas.width = Math.floor(viewport.width * pixelRatio);
    canvas.height = Math.floor(viewport.height * pixelRatio);
    const context = canvas.getContext('2d');
    const number = document.createElement('span');
    number.className = 'page-number';
    number.textContent = String(pageNumber);
    page.append(canvas, number);
    elements['pdf-pages'].append(page);
    await pdfPage.render({
      canvasContext: context, viewport,
      transform: pixelRatio === 1 ? null : [pixelRatio, 0, 0, pixelRatio, 0, 0],
    }).promise;
  }
  if (generation !== state.renderGeneration) return;
  if (state.selectedSource) updatePdfHighlight(state.selectedSource, false);
}

function updatePdfHighlight(source, shouldScroll = true) {
  document.querySelectorAll('.highlight').forEach((node) => node.remove());
  if (!source) return;
  const page = elements['pdf-pages'].querySelector(`[data-page="${source.page}"]`);
  if (!page) return;
  for (const rect of source.rects || []) {
    if (!rect) continue;
    const highlight = document.createElement('div');
    highlight.className = 'highlight visible';
    Object.assign(highlight.style, highlightStyle(rect));
    page.append(highlight);
  }
  elements['pdf-page-label'].textContent = `第 ${source.page} / ${state.analysis.page_count} 页`;
  if (shouldScroll) {
    const containerRect = elements['pdf-scroll'].getBoundingClientRect();
    const pageRect = page.getBoundingClientRect();
    const firstHighlight = page.querySelector('.highlight');
    const targetRect = firstHighlight?.getBoundingClientRect() || pageRect;
    elements['pdf-scroll'].scrollBy({
      top: targetRect.top - containerRect.top - containerRect.height * 0.34,
      behavior: 'smooth',
    });
  }
}

function describeSource(request, source) {
  const label = request.kind === 'figure' ? '已定位论文图片'
    : request.kind === 'table' ? '已定位论文表格'
      : request.kind === 'page' ? '已跳转到原文页' : '已定位证据原文';
  elements['source-kind'].textContent = `${label} · 第 ${source.page} 页`;
  elements['source-text'].textContent = source.text || request.text || request.label || '';
  elements['source-hint'].classList.remove('hidden');
}

function activateSource(request) {
  const source = resolveSourceTarget(request, state.analysis, state.sourceMap);
  if (!source) {
    showToast('这个元素暂时没有可用的原文定位');
    return null;
  }
  state.selectedSource = source;
  document.body.dataset.selectedEvidenceId = source.evidenceId || '';
  updatePdfHighlight(source);
  describeSource(request, source);
  return source;
}

function selectBlock(block) {
  if (!block) return;
  activateSource({ kind: block.type, page: block.page, text: block.text || block.label, label: block.label || block.text });
}

function notifyIntegrationReady() {
  if (!state.analysis) return;
  if (state.visualizerUrl && !state.visualizerReady) return;
  setTimeout(() => window.paperReader.notifyReady(), 250);
}

async function loadPaper(filePath) {
  if (!filePath) return;
  if (!state.building) elements['build-progress'].classList.add('hidden');
  let loaded = false;
  setLoading(true, state.llm.enabled ? '正在增强表格并连接可视化…' : '提取原文坐标并连接可视化…');
  try {
    const payload = await window.paperReader.loadPaper(filePath, state.llm);
    state.currentPath = filePath;
    state.currentPaperId = payload.paperId;
    state.analysis = payload.analysis;
    state.sourceMap = payload.sourceMap || {};
    state.selectedSource = null;
    document.body.dataset.selectedEvidenceId = '';
    const loadingTask = pdfjsLib.getDocument({ data: new Uint8Array(payload.pdfData) });
    state.pdf = await loadingTask.promise;
    saveRecent(filePath);
    state.library = normalizeLibrary(state.library, [filePath]);
    const paper = state.library.find((item) => item.path === filePath);
    if (paper) {
      paper.title = state.analysis.title || paper.title;
      paper.visualizerPath = payload.visualizerPath;
      paper.generated = Boolean(payload.visualizerUrl);
    }
    elements['paper-title'].textContent = state.analysis.title || filePath.split('/').pop();
    const evidenceCount = Object.keys(state.sourceMap).length;
    elements['paper-meta'].textContent = `${state.analysis.page_count} 页 · ${visibleBlockCount(state.analysis.blocks)} 个原文块 · ${evidenceCount} 条精确证据定位`;
    elements['llm-button'].querySelector('.status-dot').classList.toggle('active', Boolean(state.llm.apiKey));
    elements['llm-button'].lastChild.textContent = state.llm.apiKey ? '设置 · 模型已配置' : '设置';
    renderLibrary();
    renderOutline();
    loadVisualizer(payload.visualizerUrl);
    elements['build-button'].disabled = Boolean(payload.visualizerUrl);
    await renderPdf();
    if (state.analysis.llm?.status === 'error') showToast(`LLM 增强失败，已保留本地解析：${state.analysis.llm.message}`);
    if (state.analysis.llm?.status === 'partial') showToast(`部分页面解析失败，已保留可用结果：${state.analysis.llm.message}`);
    loaded = true;
  } catch (error) {
    showToast(`无法打开论文：${error.message}`);
  } finally {
    setLoading(false);
    if (loaded) notifyIntegrationReady();
  }
}

async function choosePaper() {
  const filePath = await window.paperReader.choosePaper();
  if (!filePath) return;
  state.library = normalizeLibrary(state.library, [filePath]);
  renderLibrary();
  await loadPaper(filePath);
}

async function chooseFolder() {
  const result = await window.paperReader.chooseFolder();
  if (!result) return;
  elements['library-folder'].textContent = result.folder;
  state.library = normalizeLibrary(result.papers, recentPaths());
  renderLibrary();
  if (result.papers.length) await loadPaper(result.papers[0].path);
  else showToast('所选文件夹中没有找到 PDF');
}

function toggleSidebar() { setSidebarHidden(!state.sidebarHidden); }

function bindEvents() {
  const webviewPreload = new URL('./visualizer-preload.js', window.location.href).href;
  elements['visualizer-view'].setAttribute('preload', webviewPreload);
  elements['visualizer-view'].addEventListener('ipc-message', (event) => {
    if (event.channel === 'source-activate') activateSource(event.args[0]);
    if (event.channel === 'visualizer-ready') {
      state.visualizerReady = true;
      const count = event.args[0]?.evidenceCount || 0;
      elements['visualizer-status'].textContent = `${count} 条可定位证据`;
      applyVisualizerPreferences().catch(() => {});
      notifyIntegrationReady();
    }
  });
  elements['visualizer-view'].addEventListener('did-fail-load', () => {
    elements['visualizer-status'].textContent = '载入失败';
    showToast('Visualizer 页面载入失败');
  });
  window.paperReader.onBuildProgress(updateBuildProgress);
  elements['build-button'].addEventListener('click', buildCurrentVisualizer);
  elements['open-button'].addEventListener('click', choosePaper);
  elements['folder-button'].addEventListener('click', chooseFolder);
  elements['sidebar-toggle'].addEventListener('click', toggleSidebar);
  elements['zoom-in'].addEventListener('click', async () => {
    if (!state.pdf) return;
    state.zoom = Math.min(2, state.zoom + 0.1);
    await renderPdf();
  });
  elements['zoom-out'].addEventListener('click', async () => {
    if (!state.pdf) return;
    state.zoom = Math.max(0.7, state.zoom - 0.1);
    await renderPdf();
  });
  elements['llm-button'].addEventListener('click', () => elements['llm-dialog'].showModal());
  elements['enable-llm'].addEventListener('click', async (event) => {
    event.preventDefault();
    const apiKey = elements['api-key'].value.trim() || state.llm.apiKey;
    const settings = {
      endpoint: elements['api-endpoint'].value.trim(),
      apiKey,
      model: elements['model-name'].value.trim(),
      theme: elements['theme-setting'].value,
      density: elements['density-setting'].value,
      sidebarVisible: elements['sidebar-setting'].checked,
      pdfZoom: Number(elements['pdf-zoom-setting'].value),
      reopenLast: elements['reopen-setting'].checked,
    };
    try {
      await window.paperReader.saveSettings(settings);
      state.llm = { enabled: true, ...settings };
      applyPreferences(settings);
      await applyVisualizerPreferences();
      if (state.pdf) await renderPdf();
      elements['api-key'].value = '';
      elements['api-key'].placeholder = '已安全保存';
      elements['llm-dialog'].close();
      showToast('设置已保存');
    } catch (error) {
      showToast(`无法保存配置：${error.message}`);
    }
  });
}

async function init() {
  bindEvents();
  const settings = await window.paperReader.getSettings();
  state.llm = { enabled: false, ...settings };
  applyPreferences(settings);
  elements['api-endpoint'].value = settings.endpoint;
  elements['model-name'].value = settings.model;
  elements['theme-setting'].value = settings.theme;
  elements['density-setting'].value = settings.density;
  elements['sidebar-setting'].checked = settings.sidebarVisible;
  elements['pdf-zoom-setting'].value = String(settings.pdfZoom);
  elements['reopen-setting'].checked = settings.reopenLast;
  elements['api-key'].placeholder = settings.apiKey ? '已安全保存' : '尚未配置';
  const samples = await window.paperReader.getSamples();
  const preferredPath = samples.find((paper) => paper.preferred)?.path;
  const recent = recentPaths();
  state.library = normalizeLibrary(samples, recent);
  renderLibrary();
  if (state.library.length) await loadPaper((settings.reopenLast && recent[0]) || preferredPath || state.library[0].path);
}

matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
  if (state.preferences.theme !== 'system') return;
  document.body.dataset.theme = resolvedTheme('system');
  applyVisualizerPreferences().catch(() => {});
});

window.__paperReaderTest = {
  async selectFirstVisualizerEvidence() {
    if (!state.visualizerReady) return null;
    return elements['visualizer-view'].executeJavaScript(`(() => {
      const target = document.querySelector('[data-evidence-id]');
      if (!target) return null;
      const evidenceId = target.dataset.evidenceId;
      target.click();
      return { evidenceId };
    })()`);
  },
  async testVisualizerTargets() {
    if (!state.visualizerReady) return [];
    const selectors = ['.summary-card', '.math-var', '.graph-node.interactive', '.cell-evidence', '.image-open', '.relation-evidence'];
    const results = [];
    for (const selector of selectors) {
      const target = await elements['visualizer-view'].executeJavaScript(`(() => {
        const node = document.querySelector(${JSON.stringify(selector)});
        if (!node) return null;
        node.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
        return { evidenceId: node.dataset.evidenceId || null, page: node.dataset.imagePage || null };
      })()`);
      if (!target) {
        results.push({ selector, available: false });
        continue;
      }
      await new Promise((resolve) => setTimeout(resolve, 80));
      results.push({ selector, available: true, request: target, locatedPage: state.selectedSource?.page || null, rectCount: state.selectedSource?.rects?.length || 0 });
    }
    return results;
  },
  async auditVisualizerTargets() {
    if (!state.visualizerReady) return null;
    const inventory = await elements['visualizer-view'].executeJavaScript(`(() => ({
      evidenceIds: [...new Set([...document.querySelectorAll('[data-evidence-id]')].map((node) => node.dataset.evidenceId).filter(Boolean))],
      images: [...document.querySelectorAll('[data-image-page]')].map((node) => ({
        page: Number(node.dataset.imagePage),
        kind: node.closest('.paper-table') ? 'table' : 'figure',
        text: node.dataset.imageCaption || '',
        label: node.dataset.imageTitle || node.dataset.imageAlt || '',
      })),
      pageTargets: [...document.querySelectorAll('[data-source-page]')].map((node) => Number(node.dataset.sourcePage)).filter(Boolean),
      panels: document.querySelectorAll('.panel').length,
      locatablePanels: [...document.querySelectorAll('.panel')].filter((panel) =>
        panel.querySelector('[data-evidence-id], [data-image-page], .page-text')
        || panel.closest('section')?.querySelector('[data-evidence-id], [data-image-page]')
        || document.querySelector('[data-evidence-id], [data-image-page]')
      ).length,
    }))()`);
    const evidenceLocations = inventory.evidenceIds.map((evidenceId) => resolveSourceTarget({ evidenceId }, state.analysis, state.sourceMap));
    const imageLocations = inventory.images.map((request) => resolveSourceTarget(request, state.analysis, state.sourceMap));
    const pageLocations = inventory.pageTargets.map((page) => resolveSourceTarget({ kind: 'page', page }, state.analysis, state.sourceMap));
    return {
      evidenceTargets: inventory.evidenceIds.length,
      exactEvidenceLocations: evidenceLocations.filter((item) => item?.matchedBy === 'evidence-locator' && item.rects?.length).length,
      imageTargets: inventory.images.length,
      locatedImages: imageLocations.filter((item) => item?.page && item.rects?.length).length,
      pageTargets: inventory.pageTargets.length,
      locatedPageTargets: pageLocations.filter((item) => item?.page).length,
      panels: inventory.panels,
      locatablePanels: inventory.locatablePanels,
    };
  },
  buildCurrentVisualizer,
  loadPaper,
  activateSource,
};

init();
