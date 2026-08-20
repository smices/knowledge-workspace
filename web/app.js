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
const answerMarkup = (value) => esc(value).replace(/^## (.+)$/gm, '<h3>$1</h3>').replace(/\n{2,}/g, '<br>').replace(/\n/g, '<br>');
let activeController = null;
let turnSequence = 0;

function graphMarkup(graph, markerId) {
  const edges = graph.edges || [];
  if (!edges.length) return `<section class="graph-section"><div class="graph-empty">${esc(graph.message || '当前证据不足以建立关系图。')}</div></section>`;
  const nodes = graph.nodes || [];
  const width = 720, height = 220;
  const positions = Object.fromEntries(nodes.map((node, index) => [node.id, { x: 80 + (index % 4) * 185, y: 62 + Math.floor(index / 4) * 105 }]));
  const lines = edges.map((edge, index) => {
    const source = positions[edge.source] || { x: 80, y: 62 };
    const target = positions[edge.target] || { x: 640, y: 62 };
    return `<g class="graph-link"><line x1="${source.x}" y1="${source.y}" x2="${target.x}" y2="${target.y}" marker-end="url(#${markerId})"/><text x="${(source.x + target.x) / 2}" y="${(source.y + target.y) / 2 - 7}">${esc(edge.label)}</text></g>`;
  }).join('');
  const circles = nodes.map((node) => {
    const point = positions[node.id];
    return `<g class="graph-node"><circle cx="${point.x}" cy="${point.y}" r="28"/><text x="${point.x}" y="${point.y + 4}">${esc(node.label)}</text></g>`;
  }).join('');
  return `<section class="graph-section"><div class="graph-head"><div><strong>关系图谱</strong><small>同一关系已合并；点击关系查看全部原文证据</small></div><span>${edges.length} 条关系</span></div><div class="graph-canvas"><svg class="graph-svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="关系图谱"><defs><marker id="${markerId}" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 z"/></marker></defs>${lines}${circles}</svg></div><div class="graph-edges">${edges.map((edge, index) => `<button class="graph-edge-button" data-edge-index="${index}" type="button"><span><b>${esc(edge.source)}</b><i>${esc(edge.label)} →</i><b>${esc(edge.target)}</b></span><small>证据 ${(Array.isArray(edge.evidence) ? edge.evidence : [edge.evidence]).join('、')}</small></button>`).join('')}</div><div data-role="graph-evidence" class="graph-evidence" hidden></div></section>`;
}

function bindGraphEdges(root, graph) {
  root.querySelectorAll('.graph-edge-button').forEach((button) => button.addEventListener('click', () => {
    const edge = (graph.edges || [])[Number(button.dataset.edgeIndex)];
    const ids = Array.isArray(edge?.evidence) ? edge.evidence : [edge?.evidence];
    const evidence = ids.map((id) => ({ id, citation: (graph.citations || [])[id - 1] })).filter((item) => item.citation);
    const detail = root.querySelector('[data-role="graph-evidence"]');
    detail.hidden = false;
    detail.innerHTML = evidence.map(({ id, citation }) => `<article><strong>证据 ${id} · ${esc(citation.title)}</strong><small>第 ${esc(citation.chunk_index)} 段原文</small><pre>${esc(citation.content)}</pre></article>`).join('');
  }));
}

function bindGraph(root, query, turnId) {
  const button = root.querySelector('[data-role="graph-submit"]');
  if (!button) return;
  button.addEventListener('click', async () => {
    button.disabled = true;
    button.textContent = '整理关系图谱…';
    try {
      const data = await api('/api/v1/rag/graph', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ query, limit: 5 }) });
      const slot = root.querySelector('[data-role="graph-slot"]');
      slot.innerHTML = graphMarkup(data, `graph-arrow-${turnId}`);
      bindGraphEdges(slot, data);
    } catch (error) {
      root.querySelector('[data-role="graph-slot"]').innerHTML = `<section class="graph-section"><div class="graph-empty">${esc(error.message)}</div></section>`;
    } finally {
      button.disabled = false;
      button.textContent = '整理关系图谱';
    }
  });
}

function graphAction(available) {
  return available
    ? '<div class="answer-tools"><button data-role="graph-submit" type="button">整理关系图谱</button><small>仅整理当前证据直接支持的关系。</small></div>'
    : '<div class="graph-unavailable" role="status">当前证据不足，暂不支持关系图谱。</div>';
}

function renderCitations(citations) {
  return `<section class="citation-section"><div class="citation-heading"><div><strong>来源证据</strong><span>点击可回到本次检索原文</span></div><span>${citations.length} 条</span></div><div class="citation-list">${citations.map((citation, index) => `<button class="citation" data-citation="${index}" type="button"><span class="citation-number">${index + 1}</span><span class="citation-copy"><b>${esc(citation.title)}</b><small>第 ${esc(citation.chunk_index)} 段 · 查看原文</small></span><span class="citation-arrow">↗</span></button>`).join('')}</div><div class="citation-detail" hidden></div></section>`;
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

function bindCitations(root, citations) {
  root.querySelectorAll('.citation').forEach((button) => button.addEventListener('click', () => {
    const citation = citations[Number(button.dataset.citation)];
    const detail = root.querySelector('.citation-detail');
    detail.hidden = false;
    detail.innerHTML = `<div class="detail-head"><div><strong>${esc(citation.title)}</strong><small>第 ${esc(citation.chunk_index)} 段原文</small></div><button data-role="close-citation" type="button">收起</button></div><pre>${esc(citation.content)}</pre>`;
    detail.querySelector('[data-role="close-citation"]').onclick = () => { detail.hidden = true; };
  }));
}

const copyIcon = '<svg viewBox="0 0 20 20" aria-hidden="true"><rect x="6.5" y="6.5" width="9" height="9" rx="2"/><path d="M13.5 6.5V5A1.5 1.5 0 0 0 12 3.5H5A1.5 1.5 0 0 0 3.5 5v7A1.5 1.5 0 0 0 5 13.5h1.5"/></svg>';
const likeIcon = '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M6.5 16.5h-2a1 1 0 0 1-1-1v-6a1 1 0 0 1 1-1h2m0 8h7.1a2 2 0 0 0 1.94-1.52l1-4A2 2 0 0 0 14.6 8.5H12l.4-2.7A2 2 0 0 0 10.42 3.5h-.3L6.5 8.5v8Z"/></svg>';

async function copyText(value) {
  if (navigator.clipboard?.writeText) return navigator.clipboard.writeText(value);
  const input = document.createElement('textarea');
  input.value = value;
  input.style.position = 'fixed';
  input.style.opacity = '0';
  document.body.append(input);
  input.select();
  document.execCommand('copy');
  input.remove();
}

function cacheMarkup(data) {
  const level = data.cache?.level;
  const label = level === 'l0' ? '同步复用' : level === 'l1' ? '完全匹配' : level === 'l2' ? '相似问题' : '';
  return label ? `<span class="cache-note" title="知识内容未变化，已复用经过证据校验的答案">已复用 · ${label}</span>` : '';
}

function bindAnswerActions(root, data) {
  const copyButton = root.querySelector('[data-role="copy-answer"]');
  copyButton.addEventListener('click', async () => {
    try {
      await copyText(data.answer);
      copyButton.classList.add('is-done');
      copyButton.querySelector('span').textContent = '已复制';
      setTimeout(() => {
        copyButton.classList.remove('is-done');
        copyButton.querySelector('span').textContent = '复制';
      }, 1600);
    } catch (_) {
      stat('复制失败，请手动选择答案。', true);
    }
  });
  const likeButton = root.querySelector('[data-role="like-answer"]');
  likeButton.setAttribute('aria-pressed', String(Boolean(data.liked)));
  likeButton.classList.toggle('is-liked', Boolean(data.liked));
  likeButton.addEventListener('click', async () => {
    const wasLiked = likeButton.getAttribute('aria-pressed') === 'true';
    const liked = !wasLiked;
    likeButton.setAttribute('aria-pressed', String(liked));
    likeButton.classList.toggle('is-liked', liked);
    try {
      await api(`/api/v1/rag/answers/${encodeURIComponent(data.answer_id)}/feedback`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ liked, token: data.feedback_token }),
      });
      stat(liked ? '已记录：这个回答有帮助。' : '已取消点赞。');
    } catch (error) {
      likeButton.setAttribute('aria-pressed', String(wasLiked));
      likeButton.classList.toggle('is-liked', wasLiked);
      stat(error.message, true);
    }
  });
}

function renderAnswer(root, data, query, turnId) {
  const citations = data.citations || [];
  root.innerHTML = `<div class="answer-summary">${answerStateMarkup(data)}${cacheMarkup(data)}</div><div class="answer-text">${answerMarkup(data.answer)}</div>${renderEvidenceContract(data.evidence_contract)}${citations.length ? renderCitations(citations) : '<div class="empty-evidence">当前没有可展示的来源证据。</div>'}${graphAction(data.graph_available)}<div data-role="graph-slot"></div><div class="message-actions" aria-label="回答操作"><button data-role="copy-answer" type="button">${copyIcon}<span>复制</span></button><button data-role="like-answer" type="button" aria-pressed="false">${likeIcon}<span>有帮助</span></button></div>`;
  bindCitations(root, citations);
  bindGraph(root, query, turnId);
  bindAnswerActions(root, data);
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
  const turnId = ++turnSequence;
  const turn = document.createElement('article');
  turn.className = 'conversation-turn';
  turn.innerHTML = `<div class="user-message"><span class="message-label">你</span><p>${esc(query)}</p></div><section class="assistant-message answer" aria-label="助手回答"><div class="answer-loading"><span></span><span></span><span></span><em>检索与整理中，可随时取消</em></div></section>`;
  $('conversation').append(turn);
  const assistant = turn.querySelector('.assistant-message');
  setGenerating(true);
  stat('正在检索证据…');
  try {
    const data = await api('/api/v1/rag/answer', { signal: controller.signal, method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ query, limit: 3, temperature: 0 }) });
    const citations = data.citations || [];
    stat(data.cache?.hit
      ? `${data.answer_state_label || '回答完成'} · 已复用答案 · ${citations.length} 条来源`
      : `${data.answer_state_label || '回答完成'} · 已检索 ${citations.length} 条来源`);
    renderAnswer(assistant, data, query, turnId);
    $('answer-query').value = '';
    turn.scrollIntoView({ block: 'start' });
  } catch (error) {
    if (error.name === 'AbortError' || controller.signal.aborted) {
      stat('已取消，输入框已恢复。');
      turn.remove();
    } else {
      stat(error.message, true);
      assistant.innerHTML = `<div class="message-error" role="alert">${esc(error.message)}</div>`;
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
