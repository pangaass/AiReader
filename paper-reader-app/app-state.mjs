export const TYPE_LABELS = {
  text: '句子',
  heading: '标题',
  equation: '公式',
  figure: '图片',
  table: '表格',
};

export function normalizeLibrary(samples, recentPaths = []) {
  const seen = new Set();
  const sampleByPath = new Map(samples.map((paper) => [paper.path, paper]));
  return [...recentPaths.map((filePath) => ({
    title: filePath.split('/').pop()?.replace(/\.pdf$/i, '') || 'PDF',
    ...sampleByPath.get(filePath),
    path: filePath,
  })), ...samples]
    .filter((paper) => paper.path && !seen.has(paper.path) && seen.add(paper.path));
}

export function filterBlocks(blocks, activeTypes) {
  return blocks.filter((block) => !block.replaced_by && activeTypes.has(block.type));
}

export function visibleBlockCount(blocks) {
  return blocks.filter((block) => !block.replaced_by).length;
}

export function highlightStyle(bbox) {
  return {
    left: `${bbox.x * 100}%`,
    top: `${bbox.y * 100}%`,
    width: `${bbox.width * 100}%`,
    height: `${bbox.height * 100}%`,
  };
}

export function paperStats(analysis) {
  const counts = Object.fromEntries(Object.keys(TYPE_LABELS).map((key) => [key, 0]));
  for (const block of analysis.blocks || []) {
    if (!block.replaced_by) counts[block.type] = (counts[block.type] || 0) + 1;
  }
  return counts;
}

export function normalizeSourceText(value) {
  return String(value || '')
    .normalize('NFKC')
    .replace(/-\s+/g, '')
    .toLocaleLowerCase()
    .replace(/[^\p{L}\p{N}]+/gu, '');
}

function textSimilarity(first, second) {
  const a = normalizeSourceText(first);
  const b = normalizeSourceText(second);
  if (!a || !b) return 0;
  if (a.includes(b) || b.includes(a)) return Math.min(a.length, b.length) / Math.max(a.length, b.length) + 0.5;
  const grams = (value) => new Set(Array.from({ length: Math.max(0, value.length - 2) }, (_, index) => value.slice(index, index + 3)));
  const left = grams(a);
  const right = grams(b);
  if (!left.size || !right.size) return 0;
  let shared = 0;
  for (const gram of left) if (right.has(gram)) shared += 1;
  return shared / Math.min(left.size, right.size);
}

export function resolveSourceTarget(request, analysis, sourceMap = {}) {
  if (!request || !analysis) return null;
  const exact = request.evidenceId ? sourceMap[request.evidenceId] : null;
  if (exact?.page) return { ...exact, matchedBy: 'evidence-locator' };

  const page = Number(request.page);
  if (!Number.isFinite(page) || page < 1) return null;
  const pageBlocks = (analysis.blocks || []).filter((block) => block.page === page && !block.replaced_by);
  const preferred = ['figure', 'table'].includes(request.kind)
    ? pageBlocks.filter((block) => block.type === request.kind)
    : pageBlocks;
  const candidates = preferred.length ? preferred : pageBlocks;
  const query = request.text || request.label || '';
  const ranked = candidates
    .map((block) => ({ block, score: textSimilarity(query, block.text || block.label || '') }))
    .sort((a, b) => b.score - a.score);
  const match = ranked[0]?.score >= 0.18 ? ranked[0].block : candidates[0];
  if (match) {
    return {
      page,
      rects: match.rects || [match.bbox],
      blockId: match.id,
      evidenceId: request.evidenceId || null,
      text: match.text || match.label || query,
      matchedBy: ranked[0]?.score >= 0.18 ? 'content-match' : 'page-type',
    };
  }
  return { page, rects: [], evidenceId: request.evidenceId || null, text: query, matchedBy: 'page' };
}
