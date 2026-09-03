const test = require('node:test');
const assert = require('node:assert/strict');

test('normalized highlight geometry maps to percentages', async () => {
  const { highlightStyle } = await import('../app-state.mjs');
  assert.deepEqual(highlightStyle({ x: 0.1, y: 0.2, width: 0.3, height: 0.04 }), {
    left: '10%', top: '20%', width: '30%', height: '4%',
  });
});

test('library paths are deduplicated with recent papers first', async () => {
  const { normalizeLibrary } = await import('../app-state.mjs');
  const result = normalizeLibrary([{ title: 'Sample', path: '/a.pdf', preferred: true }], ['/b.pdf', '/a.pdf']);
  assert.deepEqual(result.map((paper) => paper.path), ['/b.pdf', '/a.pdf']);
  assert.equal(result[1].preferred, true);
  assert.equal(result[1].title, 'Sample');
});

test('table replacements hide source sentences without deleting them', async () => {
  const { filterBlocks, paperStats, visibleBlockCount } = await import('../app-state.mjs');
  const blocks = [
    { id: 'sentence', type: 'text', replaced_by: 'table' },
    { id: 'table', type: 'table' },
    { id: 'prose', type: 'text' },
  ];
  assert.deepEqual(filterBlocks(blocks, new Set(['text', 'table'])).map((block) => block.id), ['table', 'prose']);
  assert.equal(visibleBlockCount(blocks), 2);
  assert.deepEqual(paperStats({ blocks }), { text: 1, heading: 0, equation: 0, figure: 0, table: 1 });
});

test('evidence locator wins over fuzzy content matching', async () => {
  const { resolveSourceTarget } = await import('../app-state.mjs');
  const analysis = { blocks: [{ id: 'fallback', type: 'text', page: 2, text: 'same text', bbox: { x: 0, y: 0, width: 1, height: 0.1 } }] };
  const sourceMap = { evidence: { evidenceId: 'evidence', page: 2, rects: [{ x: 0.2, y: 0.3, width: 0.4, height: 0.1 }] } };
  const result = resolveSourceTarget({ evidenceId: 'evidence', page: 2, text: 'same text' }, analysis, sourceMap);
  assert.equal(result.matchedBy, 'evidence-locator');
  assert.equal(result.rects[0].x, 0.2);
});

test('figure clicks fall back to a figure block on the requested page', async () => {
  const { resolveSourceTarget } = await import('../app-state.mjs');
  const analysis = { blocks: [
    { id: 'text', type: 'text', page: 4, text: 'Figure 2 overview', bbox: { x: 0, y: 0, width: 1, height: 0.1 } },
    { id: 'figure', type: 'figure', page: 4, label: 'Figure 2', bbox: { x: 0.1, y: 0.2, width: 0.8, height: 0.3 } },
  ] };
  const result = resolveSourceTarget({ kind: 'figure', page: 4, label: 'Figure 2' }, analysis, {});
  assert.equal(result.blockId, 'figure');
  assert.equal(result.page, 4);
});
