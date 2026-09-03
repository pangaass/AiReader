const { ipcRenderer } = require('electron');

function evidenceRegistry() {
  try { return JSON.parse(document.getElementById('evidenceRegistry')?.textContent || '{}'); }
  catch { return {}; }
}

function pageFromHref(href) {
  const match = String(href || '').match(/[#?&]page=(\d+)/i);
  return match ? Number.parseInt(match[1], 10) : null;
}

function sourceForTarget(target) {
  const direct = target.closest?.('[data-evidence-id]');
  const registry = evidenceRegistry();
  if (direct?.dataset.evidenceId) {
    const item = registry[direct.dataset.evidenceId] || {};
    return {
      kind: 'evidence', evidenceId: direct.dataset.evidenceId,
      page: Number(item.page) || null, text: item.verbatim_text || '',
      sectionId: item.section_id || null,
      label: direct.getAttribute('aria-label') || direct.textContent?.trim() || '',
    };
  }
  const image = target.closest?.('[data-image-page]');
  if (image?.dataset.imagePage) {
    return {
      kind: image.closest('.paper-table') ? 'table' : 'figure',
      page: Number(image.dataset.imagePage), text: image.dataset.imageCaption || '',
      label: image.dataset.imageTitle || image.dataset.imageAlt || '',
    };
  }
  const pageLink = target.closest?.('.page-link');
  const linkedPage = pageFromHref(pageLink?.getAttribute('href'));
  if (linkedPage) return { kind: 'page', page: linkedPage, text: '', label: pageLink.textContent?.trim() || '' };
  const pageTarget = target.closest?.('[data-source-page]');
  if (pageTarget?.dataset.sourcePage) {
    return { kind: 'page', page: Number(pageTarget.dataset.sourcePage), text: '', label: pageTarget.textContent?.trim() || '' };
  }
  const component = target.closest?.('.panel');
  if (component) {
    const evidenceTarget = component.querySelector('[data-evidence-id]');
    if (evidenceTarget) return sourceForTarget(evidenceTarget);
    const imageTarget = component.querySelector('[data-image-page]');
    if (imageTarget) return sourceForTarget(imageTarget);
    const pageText = component.querySelector('.page-text')?.textContent || '';
    const page = Number.parseInt(pageText.match(/\d+/)?.[0] || '', 10);
    if (page) {
      const kind = component.classList.contains('formula-card') ? 'equation'
        : component.classList.contains('paper-table') ? 'table'
          : component.classList.contains('paper-figure') ? 'figure' : 'page';
      return { kind, page, text: component.textContent?.trim() || '', label: pageText.trim() };
    }
    const section = component.closest('section');
    const sectionEvidence = section?.querySelector('[data-evidence-id]');
    if (sectionEvidence) return sourceForTarget(sectionEvidence);
    const sectionImage = section?.querySelector('[data-image-page]');
    if (sectionImage) return sourceForTarget(sectionImage);
    const componentTop = component.getBoundingClientRect().top;
    const nearest = [...document.querySelectorAll('[data-evidence-id], [data-image-page]')]
      .sort((left, right) => Math.abs(left.getBoundingClientRect().top - componentTop) - Math.abs(right.getBoundingClientRect().top - componentTop))[0];
    if (nearest) return sourceForTarget(nearest);
  }
  return null;
}

function installIntegration() {
  const style = document.createElement('style');
  style.textContent = `
    .panel { cursor: pointer; }
    [data-evidence-id], [data-image-page], [data-source-page] { outline-offset: 3px; }
    .electron-source-flash { animation: electron-source-flash .7s ease-out; }
    @keyframes electron-source-flash { 0% { outline: 3px solid #ddad48; } 100% { outline: 3px solid transparent; } }
  `;
  document.head.append(style);
  document.addEventListener('click', (event) => {
    const source = sourceForTarget(event.target);
    if (!source) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    const visualTarget = event.target.closest?.('[data-evidence-id], [data-image-page], [data-source-page], .panel');
    visualTarget?.classList.add('electron-source-flash');
    setTimeout(() => visualTarget?.classList.remove('electron-source-flash'), 750);
    ipcRenderer.sendToHost('source-activate', source);
  }, true);
  document.addEventListener('keydown', (event) => {
    if (!['Enter', ' '].includes(event.key)) return;
    const source = sourceForTarget(event.target);
    if (source) {
      event.preventDefault();
      event.stopImmediatePropagation();
      ipcRenderer.sendToHost('source-activate', source);
    }
  }, true);
  ipcRenderer.sendToHost('visualizer-ready', {
    evidenceCount: Object.keys(evidenceRegistry()).length,
    title: document.title,
  });
}

window.addEventListener('DOMContentLoaded', installIntegration, { once: true });
