'use strict';

const API = '/api';
const state = { q: '', category: '', tag: '', source: '', offset: 0, limit: 36, items: [], hasMore: false, busy: false };
const KIND = {
  youtube: ['YouTube', 'b-youtube'], instagram: ['Instagram', 'b-instagram'],
  tiktok: ['TikTok', 'b-tiktok'], twitter: ['X', 'b-twitter'],
  telegram: ['Telegram', 'b-telegram'], vk: ['VK', 'b-vk'], article: ['Статья', 'b-article'],
};

const $ = (sel, root = document) => root.querySelector(sel);
const grid = $('#grid');
const detail = $('#detail');
const drawer = $('#drawer');

const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
));

const fmtDate = (s) => {
  if (!s) return '';
  const [y, m, d] = s.split('-');
  return d ? `${d}.${m}.${y}` : s;
};

const fmtDur = (sec) => {
  if (!sec) return '';
  const m = Math.floor(sec / 60), s = sec % 60;
  return `${m}:${String(s).padStart(2, '0')}`;
};

const plural = (n, one, few, many) => {
  const mod10 = n % 10, mod100 = n % 100;
  if (mod10 === 1 && mod100 !== 11) return one;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return few;
  return many;
};

async function getJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${res.status} ${url}`);
  return res.json();
}

function obsidianLink(item) {
  if (!item.file) return '';
  const rel = 'library/' + String(item.file).replace(/\.md$/, '');
  return `obsidian://open?vault=${encodeURIComponent(item.vault || 'Brain')}&file=${encodeURIComponent(rel)}`;
}

function badge(item) {
  const [label, cls] = KIND[item.kind] || KIND.article;
  return `<span class="badge ${cls}">${esc(label)}</span>`;
}

/* ---------------- загрузка ---------------- */

async function load(reset = true) {
  if (state.busy) return;
  state.busy = true;
  if (reset) {
    state.offset = 0;
    state.items = [];
    grid.innerHTML = Array.from({ length: 6 }, () => '<div class="skeleton"></div>').join('');
  }
  const params = new URLSearchParams({
    q: state.q, category: state.category, tag: state.tag, source: state.source,
    limit: String(state.limit), offset: String(state.offset),
  });
  try {
    const data = await getJSON(`${API}/items?${params}`);
    state.items = reset ? data.items : state.items.concat(data.items);
    state.offset = state.items.length;
    state.hasMore = data.has_more;
  } catch (err) {
    console.error(err);
    if (reset) grid.innerHTML = '';
  }
  state.busy = false;
  renderCards();
}

function renderCards() {
  const items = state.items;
  if (!items.length) {
    grid.innerHTML = '';
    $('#empty').hidden = false;
    $('#more').hidden = true;
    return;
  }
  $('#empty').hidden = true;
  grid.innerHTML = items.map((item) => {
    const tags = (item.tags || []).slice(0, 3)
      .map((t) => `<span>${esc(t)}</span>`).join('');
    const dur = item.duration ? `<span class="card__dur">${fmtDur(item.duration)}</span>` : '';
    return `
      <article class="card" data-id="${item.id}" tabindex="0" role="button"
               aria-label="${esc(item.title)}">
        <div class="card__top">
          ${badge(item)}
          <span class="card__date">${fmtDate(item.saved || item.published)}</span>
        </div>
        <h3 class="card__title">${esc(item.title)}</h3>
        ${item.summary ? `<p class="card__summary">${esc(item.summary)}</p>` : ''}
        <div class="card__foot">
          <span class="card__cat">${esc(item.category || 'Другое')}</span>
          <div class="card__tags">${tags}</div>
          ${dur}
        </div>
      </article>`;
  }).join('');
  $('#more').hidden = !state.hasMore;
}

/* ---------------- фильтры ---------------- */

function renderStats(stats) {
  const by = stats.by_status || {};
  const total = stats.total_done || 0;
  const parts = [`${total} ${plural(total, 'материал', 'материала', 'материалов')}`];
  if (by.queued) parts.push(`в очереди ${by.queued}`);
  if (by.failed) parts.push(`с ошибкой ${by.failed}`);
  $('#stats').textContent = parts.join(' · ');
}

function chip(label, count, active, value, kind) {
  return `<button class="chip${active ? ' is-active' : ''}" data-${kind}="${esc(value)}">` +
    `${esc(label)}${count != null ? ` <small>${count}</small>` : ''}</button>`;
}

function renderCategories(categories = []) {
  const total = categories.reduce((sum, c) => sum + c.count, 0);
  const html = [chip('Все', total, !state.category, '', 'category')]
    .concat(categories.map((c) => chip(c.name, c.count, state.category === c.name, c.name, 'category')));
  $('#categories').innerHTML = html.join('');
}

function renderTags(tags = []) {
  if (!tags.length) { $('#tags').innerHTML = ''; return; }
  const html = tags.slice(0, 28).map((t) => {
    const active = state.tag === t.tag;
    return `<button class="chip${active ? ' is-active' : ''}" data-tag="${esc(t.tag)}">#${esc(t.tag)}${active ? ' ×' : ''}</button>`;
  });
  if (state.tag) html.unshift(`<button class="chip" data-tag="">сбросить</button>`);
  $('#tags').innerHTML = html.join('');
}

/* ---------------- карточка ---------------- */

async function openItem(id) {
  let item;
  try {
    item = await getJSON(`${API}/item?id=${id}`);
  } catch (err) {
    console.error(err);
    return;
  }
  const meta = [];
  if (item.author) meta.push(esc(item.author));
  if (item.site) meta.push(esc(item.site));
  if (item.published) meta.push(esc(item.published));
  if (item.saved) meta.push(`добавлено ${fmtDate(item.saved)}`);
  if (item.duration) meta.push(fmtDur(item.duration));
  if (item.quality) meta.push('★'.repeat(item.quality));

  const tags = (item.tags || []).map((t) =>
    `<button class="chip" data-tag="${esc(t)}">#${esc(t)}</button>`).join('');
  const obsidian = obsidianLink(item);
  // запись уже в конвейере — повторная постановка ничего не даст
  const pending = item.status === 'queued' ? 'Уже в очереди'
    : item.status === 'processing' ? 'Обрабатывается…' : '';

  detail.innerHTML = `
    <div class="detail">
      <div class="detail__top">
        ${badge(item)}
        <span>${esc(item.category || 'Другое')}</span>
      </div>
      <h2>${esc(item.title)}</h2>
      <div class="detail__meta">${meta.map((m) => `<span>${m}</span>`).join('')}</div>
      <div class="detail__actions">
        <a class="btn btn--primary" href="${esc(item.url)}" target="_blank" rel="noopener">Открыть оригинал ↗</a>
        ${obsidian ? `<a class="btn" href="${esc(obsidian)}">Открыть в Obsidian</a>` : ''}
        <button class="btn" id="copy">Скопировать ссылку</button>
        <button class="btn" id="retry"${pending ? ' disabled' : ''}>${pending || 'Повторить'}</button>
        <button class="btn btn--danger" id="del">Удалить</button>
      </div>
      <div class="detail__note" id="note" hidden></div>
      <div class="md" id="md"></div>
      <div class="detail__tags">${tags}</div>
    </div>`;

  const md = $('#md');
  const source = item.content || item.summary || '';
  md.innerHTML = window.marked ? marked.parse(source, { gfm: true }) : `<p>${esc(source)}</p>`;
  // строка «Источник: …» дублирует метаданные над заголовком — убираем в вебе
  md.querySelectorAll('p').forEach((p) => {
    if (p.textContent.trim().startsWith('Источник:')) p.remove();
  });

  $('#copy').addEventListener('click', async (event) => {
    const link = `${location.origin}/#id=${item.id}`;
    try {
      await navigator.clipboard.writeText(link);
      event.target.textContent = 'Скопировано';
      setTimeout(() => { event.target.textContent = 'Скопировать ссылку'; }, 1600);
    } catch { prompt('Ссылка:', link); }
  });

  $('#del').addEventListener('click', async () => {
    if (!confirm('Удалить материал из избранного? Карточка уйдёт в data/trash.')) return;
    await fetch(`${API}/delete?id=${item.id}`, { method: 'POST' });
    closeDrawer();
    await refreshMeta();
    await load(true);
  });

  const retry = $('#retry');
  retry.addEventListener('click', async () => {
    if (retry.disabled) return;
    retry.disabled = true;
    retry.textContent = 'Ставлю в очередь…';
    const note = $('#note');
    try {
      const res = await fetch(`${API}/requeue?id=${item.id}`, { method: 'POST' });
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      retry.textContent = '♻️ В очереди';
      note.hidden = false;
      note.textContent = '♻️ Запись снова в очереди — карточка перезапишется, когда воркер её разберёт.';
      await refreshMeta();
      await load(true);
    } catch (err) {
      console.error(err);
      retry.disabled = false;
      retry.textContent = 'Повторить';
      note.hidden = false;
      note.textContent = `Не удалось поставить в очередь: ${err.message}`;
    }
  });

  drawer.setAttribute('aria-hidden', 'false');
  document.body.style.overflow = 'hidden';
  drawer.querySelector('.drawer__panel').scrollTop = 0;
  history.replaceState(null, '', `#id=${item.id}`);
}

function closeDrawer() {
  if (drawer.getAttribute('aria-hidden') === 'true') return;
  drawer.setAttribute('aria-hidden', 'true');
  document.body.style.overflow = '';
  if (location.hash) history.replaceState(null, '', location.pathname);
}

/* ---------------- события ---------------- */

function bind() {
  let timer;
  $('#q').addEventListener('input', (event) => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      state.q = event.target.value.trim();
      load(true);
    }, 260);
  });

  $('#categories').addEventListener('click', (event) => {
    const btn = event.target.closest('[data-category]');
    if (!btn) return;
    state.category = btn.dataset.category;
    renderCategories(state.facets ? state.facets.categories : []);
    load(true);
  });

  const onTag = (event) => {
    const btn = event.target.closest('[data-tag]');
    if (!btn) return;
    state.tag = btn.dataset.tag === state.tag ? '' : btn.dataset.tag;
    renderTags(state.tagList);
    if (drawer.getAttribute('aria-hidden') === 'false') closeDrawer();
    load(true);
  };
  $('#tags').addEventListener('click', onTag);
  detail.addEventListener('click', onTag);

  grid.addEventListener('click', (event) => {
    const card = event.target.closest('.card');
    if (card) openItem(Number(card.dataset.id));
  });
  grid.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    const card = event.target.closest('.card');
    if (card) { event.preventDefault(); openItem(Number(card.dataset.id)); }
  });

  $('#more').addEventListener('click', () => load(false));
  $('#theme').addEventListener('click', () => {
    const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    localStorage.setItem('fav-theme', next);
  });
  drawer.addEventListener('click', (event) => {
    if (event.target.closest('[data-close]')) closeDrawer();
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') closeDrawer();
    if (event.key === '/' && document.activeElement !== $('#q')) {
      event.preventDefault();
      $('#q').focus();
    }
  });
  window.addEventListener('popstate', () => {
    const match = location.hash.match(/id=(\d+)/);
    if (match) openItem(Number(match[1]));
    else closeDrawer();
  });

  const observer = new IntersectionObserver((entries) => {
    if (entries.some((e) => e.isIntersecting) && state.hasMore && !state.busy) load(false);
  }, { rootMargin: '400px' });
  observer.observe($('#sentinel'));
}

/* ---------------- старт ---------------- */

async function refreshMeta() {
  try {
    const [stats, facets, tags] = await Promise.all([
      getJSON(`${API}/stats`), getJSON(`${API}/facets`), getJSON(`${API}/tags`),
    ]);
    state.facets = facets;
    state.tagList = tags.tags || [];
    renderStats(stats);
    renderCategories(facets.categories || []);
    renderTags(state.tagList);
  } catch (err) {
    console.error(err);
  }
}

(async function init() {
  await refreshMeta();
  bind();
  await load(true);
  const match = location.hash.match(/id=(\d+)/);
  if (match) openItem(Number(match[1]));
})();
