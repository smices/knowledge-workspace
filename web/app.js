const $ = (id) => document.getElementById(id);
const brand = window.__APP_BRAND__ || {};
const applyBrand = () => {
  const name = brand.name || 'Knowledge Workspace';
  const markText = brand.mark || 'K';
  document.documentElement.style.setProperty('--brand-primary', brand.primaryColor || '#4f5bd5');
  document.title = `${name}${brand.tagline ? ` · ${brand.tagline}` : ''}`;
  $('brand-name').textContent = name;
  $('brand-tagline').textContent = brand.tagline || '';
  $('brand-workspace').textContent = `${name.toUpperCase()} / WORKSPACE`;
  $('brand-footer-name').textContent = name;
  $('brand-footer-copy').textContent = brand.footer || '';
  const mark = $('brand-mark');
  mark.textContent = markText;
  if (brand.logoUrl) {
    const image = document.createElement('img');
    image.alt = '';
    image.src = brand.logoUrl;
    image.onerror = () => { mark.textContent = markText; };
    mark.replaceChildren(image);
  }
};
applyBrand();
const api = async (url, options = {}) => {
  const response = await fetch(url, { credentials: 'same-origin', ...options, headers: { ...(options.headers || {}) } });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw Error(data.detail || `请求失败 (${response.status})`);
  return data;
};
const stat = (text, error = false) => { const node = $('answer-status'); node.textContent = text; node.className = `status${error ? ' error' : ''}`; };
const esc = (value) => String(value || '').replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' }[char]));
const answerMarkup = (value) => esc(value).replace(/^## (.+)$/gm, '<h3>$1</h3>').replace(/\n/g, '<br>');
let citations = [];
let activeController = null;

function graphMarkup(graph) {
  const edges = graph.edges || [];
  if (!edges.length) return `<section id="relationship-graph" class="graph-section"><div class="graph-empty">${esc(graph.message || '当前证据不足以建立关系图。')}</div></section>`;
  const nodes = graph.nodes || [];
  const width = 720, height = 220;
  const positions = Object.fromEntries(nodes.map((node, index) => [node.id, { x: 80 + (index % 4) * 185, y: 62 + Math.floor(index / 4) * 105 }]));
  const lines = edges.map((edge, index) => {
    const source = positions[edge.source] || { x: 80, y: 62 };
    const target = positions[edge.target] || { x: 640, y: 62 };
    return `<g class="graph-link"><line x1="${source.x}" y1="${source.y}" x2="${target.x}" y2="${target.y}" marker-end="url(#graph-arrow)"/><text x="${(source.x + target.x) / 2}" y="${(source.y + target.y) / 2 - 7}">${esc(edge.label)}</text></g>`;
  }).join('');
  const circles = nodes.map((node) => {
    const point = positions[node.id];
    return `<g class="graph-node"><circle cx="${point.x}" cy="${point.y}" r="28"/><text x="${point.x}" y="${point.y + 4}">${esc(node.label)}</text></g>`;
  }).join('');
  return `<section id="relationship-graph" class="graph-section"><div class="graph-head"><div><strong>关系图谱</strong><small>同一关系已合并；点击关系查看全部原文证据</small></div><span>${edges.length} 条关系</span></div><div class="graph-canvas"><svg class="graph-svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="关系图谱"><defs><marker id="graph-arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 z"/></marker></defs>${lines}${circles}</svg></div><div class="graph-edges">${edges.map((edge, index) => `<button class="graph-edge-button" data-edge-index="${index}" type="button"><span><b>${esc(edge.source)}</b><i>${esc(edge.label)} →</i><b>${esc(edge.target)}</b></span><small>证据 ${(Array.isArray(edge.evidence) ? edge.evidence : [edge.evidence]).join('、')}</small></button>`).join('')}</div><div id="graph-evidence" class="graph-evidence" hidden></div></section>`;
}

function bindGraphEdges(graph) {
  document.querySelectorAll('.graph-edge-button').forEach((button) => button.addEventListener('click', () => {
    const edge = (graph.edges || [])[Number(button.dataset.edgeIndex)];
    const ids = Array.isArray(edge?.evidence) ? edge.evidence : [edge?.evidence];
    const evidence = ids.map((id) => ({ id, citation: (graph.citations || [])[id - 1] })).filter((item) => item.citation);
    const detail = $('graph-evidence');
    detail.hidden = false;
    detail.innerHTML = evidence.map(({ id, citation }) => `<article><strong>证据 ${id} · ${esc(citation.title)}</strong><small>第 ${esc(citation.chunk_index)} 段原文</small><pre>${esc(citation.content)}</pre></article>`).join('');
  }));
}

function bindGraph() {
  const button = $('graph-submit');
  if (!button) return;
  button.addEventListener('click', async () => {
    button.disabled = true;
    button.textContent = '整理关系图谱…';
    try {
      const data = await api('/api/v1/rag/graph', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ query: $('answer-query').value.trim(), limit: 5 }) });
      $('graph-slot').innerHTML = graphMarkup(data);
      bindGraphEdges(data);
    } catch (error) {
      $('graph-slot').innerHTML = `<section class="graph-section"><div class="graph-empty">${esc(error.message)}</div></section>`;
    } finally {
      button.disabled = false;
      button.textContent = '整理关系图谱';
    }
  });
}

function graphAction(available) {
  return available
    ? '<div class="answer-tools"><button id="graph-submit" type="button">整理关系图谱</button><small>仅整理当前证据直接支持的关系。</small></div>'
    : '<div class="graph-unavailable" role="status">当前证据不足，暂不支持关系图谱。</div>';
}

function renderCitations() {
  return `<section class="citation-section"><div class="citation-heading"><div><strong>来源证据</strong><span>回答中的每个结论都可以回到原文</span></div><span>${citations.length} 条</span></div><div class="citation-list">${citations.map((citation, index) => `<button class="citation" data-citation="${index}" type="button"><span class="citation-number">${index + 1}</span><span class="citation-copy"><b>${esc(citation.title)}</b><small>第 ${esc(citation.chunk_index)} 段 · 查看原文</small></span><span class="citation-arrow">↗</span></button>`).join('')}</div><div id="citation-detail" class="citation-detail" hidden></div></section>`;
}

const supportLabels = { supported: '直接支持', cited: '有引用但需核对', partial: '部分支持', insufficient: '证据不足', conflict: '证据冲突' };
function renderEvidenceContract(contract = []) {
  if (!contract.length) return '';
  return `<section class="evidence-contract"><div class="citation-heading"><div><strong>结论证据契约</strong><span>逐条标出支持状态与置信度</span></div></div><div class="evidence-claims">${contract.map((item) => `<article class="evidence-claim"><p>${esc(item.claim)}</p><div><span class="support-${esc(item.support)}">${esc(supportLabels[item.support] || item.support)}</span><small>证据 ${(item.evidence || []).join('、') || '—'} · ${Math.round(Number(item.confidence || 0) * 100)}%</small></div></article>`).join('')}</div></section>`;
}

function answerStateMarkup(data) {
  const state = data.answer_state || 'no_answer';
  return `<span class="answer-state answer-state-${esc(state)}">${esc(data.answer_state_label || ({ answered: '已回答', partial: '部分回答', no_answer: '无答案', conflict: '证据冲突' }[state] || state))}</span>`;
}

function bindCitations() {
  document.querySelectorAll('.citation').forEach((button) => button.addEventListener('click', () => {
    const citation = citations[Number(button.dataset.citation)];
    const detail = $('citation-detail');
    detail.hidden = false;
    detail.innerHTML = `<div class="detail-head"><div><strong>${esc(citation.title)}</strong><small>第 ${esc(citation.chunk_index)} 段原文</small></div><button id="close-citation" type="button">收起</button></div><pre>${esc(citation.content)}</pre>`;
    $('close-citation').onclick = () => { detail.hidden = true; };
  }));
}

function setGenerating(generating) {
  const input = $('answer-query');
  const button = $('answer-submit');
  input.disabled = generating;
  button.disabled = false;
  button.classList.toggle('cancel-button', generating);
  button.innerHTML = generating ? '取消生成 <span>×</span>' : '生成答案 <span>↗</span>';
  $('answer-form').classList.toggle('is-generating', generating);
  $('answer-form').setAttribute('aria-busy', generating ? 'true' : 'false');
}

async function submitAnswer(event) {
  event.preventDefault();
  if (activeController) {
    activeController.abort();
    return;
  }
  const query = $('answer-query').value.trim();
  if (!query) return;
  const controller = new AbortController();
  activeController = controller;
  setGenerating(true);
  stat('正在检索证据…');
  $('answer-result').innerHTML = '<div class="answer-loading"><span></span><span></span><span></span><em>检索与整理中，可随时取消</em></div>';
  try {
    const data = await api('/api/v1/rag/answer', { signal: controller.signal, method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ query, limit: 3, temperature: 0 }) });
    citations = data.citations || [];
    stat(`${data.answer_state_label || '回答完成'} · 已检索 ${citations.length} 条来源`);
    $('answer-result').innerHTML = `<div class="answer-summary">${answerStateMarkup(data)}</div><div class="answer-text">${answerMarkup(data.answer)}</div>${renderEvidenceContract(data.evidence_contract)}${citations.length ? renderCitations() : '<div class="empty-evidence">当前没有可展示的来源证据。</div>'}${graphAction(data.graph_available)}<div id="graph-slot"></div>`;
    bindCitations();
    bindGraph();
  } catch (error) {
    if (error.name === 'AbortError' || controller.signal.aborted) {
      stat('已取消，输入框已恢复。');
      $('answer-result').innerHTML = '';
    } else {
      stat(error.message, true);
      $('answer-result').innerHTML = '';
    }
  } finally {
    if (activeController === controller) activeController = null;
    setGenerating(false);
  }
}

async function loadPopular() {
  try {
    const data = await api('/api/v1/questions/top');
    const element = $('popular-questions');
    element.innerHTML = data.items?.length ? data.items.map((item, index) => `<button class="popular-question" data-question="${esc(item.question)}" type="button"><span class="question-rank">${String(index + 1).padStart(2, '0')}</span><span class="popular-text">${esc(item.question)}</span><small>${item.count} 次</small><span class="popular-arrow">↗</span></button>`).join('') : '<span class="muted">暂无常用问题</span>';
    document.querySelectorAll('.popular-question').forEach((button) => button.addEventListener('click', () => {
      if (activeController) return;
      $('answer-query').value = button.dataset.question;
      $('answer-form').requestSubmit();
      window.scrollTo({ top: $('answer').offsetTop - 88, behavior: 'smooth' });
    }));
  } catch (error) {
    $('popular-questions').innerHTML = '<span class="muted">登录后显示常用问题</span>';
  }
}

$('answer-form').addEventListener('submit', submitAnswer);
const themeToggle = $('theme-toggle');
const applyTheme = (dark) => { document.body.dataset.theme = dark ? 'dark' : 'light'; themeToggle.textContent = dark ? '☼' : '◐'; };
applyTheme(localStorage.getItem('sn-theme') === 'dark');
themeToggle.addEventListener('click', () => { const dark = document.body.dataset.theme !== 'dark'; localStorage.setItem('sn-theme', dark ? 'dark' : 'light'); applyTheme(dark); });
document.addEventListener('keydown', (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
    event.preventDefault();
    $('answer-query').focus();
  }
  if (event.key === 'Escape' && activeController) activeController.abort();
});
loadPopular();
