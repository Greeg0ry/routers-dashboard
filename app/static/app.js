'use strict';

const CHECKS = ['reach', 'internet', 'zapret', 'forkop', 'google', 'youtube', 'chatgpt', 'discord'];
const LABEL = { reach: 'Связь', internet: 'Интернет', zapret: 'zapret', forkop: 'forkop', google: 'Google', youtube: 'YouTube', chatgpt: 'ChatGPT', discord: 'Discord' };
const SHORT = { reach: 'Связь', internet: 'Инет', zapret: 'zapret', forkop: 'forkop', google: 'Google', youtube: 'YouTube', chatgpt: 'GPT', discord: 'Discord' };
const STATUS = { ok: 'работает', fail: 'не работает', na: 'не установлен', unknown: 'нет данных', '': 'нет данных' };
const OVERALL = { ok: 'Всё работает', problem: 'Есть проблемы', offline: 'Не в сети', nodata: 'Нет данных' };
const REFRESH_MS = 15000;

const svg = (d) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${d}</svg>`;
const ICON = {
  ok: svg('<path d="M5 12.5l4.5 4.5L19 7.5"/>'),
  fail: svg('<path d="M6 6l12 12M18 6L6 18"/>'),
  unknown: svg('<path d="M9.2 9a3 3 0 1 1 4.3 2.7c-.9.5-1.5 1.1-1.5 2.3"/><path d="M12 17.5v.1"/>'),
  na: svg('<path d="M7 12h10"/>'),
  bell: svg('<path d="M6 9a6 6 0 0 1 12 0c0 6 2.5 7 2.5 7h-17S6 15 6 9z"/><path d="M10 20a2 2 0 0 0 4 0"/>'),
  bellOff: svg('<path d="M8.6 4.1A6 6 0 0 1 18 9c0 2.3.4 3.9.9 5M6.3 7.2A6 6 0 0 0 6 9c0 6-2.5 7-2.5 7H16"/><path d="M10 20a2 2 0 0 0 4 0"/><path d="M3 3l18 18"/>'),
  close: svg('<path d="M6 6l12 12M18 6L6 18"/>'),
  external: svg('<path d="M14 4h6v6"/><path d="M20 4l-9 9"/><path d="M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>'),
  wrench: svg('<path d="M14.5 6.5a4 4 0 0 0 5 5L21 13l-8.5 8.5a2.1 2.1 0 0 1-3-3L18 10"/><path d="M14.5 6.5L17 4a5 5 0 0 0-6.5 6.5L3 18a2.1 2.1 0 0 0 3 3l1.5-1.5"/>'),
  update: svg('<path d="M12 3v12"/><path d="M7 10l5 5 5-5"/><path d="M5 21h14"/>'),
  refresh: svg('<path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v6h-6"/>'),
  search: svg('<circle cx="11" cy="11" r="7"/><path d="M20 20l-3.6-3.6"/>'),
};
ICON[''] = ICON.na;

const $ = (id) => document.getElementById(id);
const state = { data: null, filter: 'all', check: null, search: '', openId: null, detail: null, hours: 24, loadedAt: 0 };

// ---- helpers -----------------------------------------------------------------

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'html') el.innerHTML = v; // only ever fed the ICON constants above
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    el.append(kid.nodeType ? kid : document.createTextNode(kid));
  }
  return el;
}

function dur(seconds) {
  seconds = Math.max(0, Math.round(seconds));
  if (seconds < 90) return `${seconds} с`;
  const m = Math.floor(seconds / 60);
  if (m < 90) return `${m} мин`;
  const hrs = Math.floor(m / 60);
  if (hrs < 48) return `${hrs} ч`;
  return `${Math.floor(hrs / 24)} дн`;
}
const ago = (ts) => (ts ? `${dur(now() - ts)} назад` : '—');
const now = () => (state.data ? state.data.now + (Date.now() - state.loadedAt) / 1000 : Date.now() / 1000);
const pad = (n) => String(n).padStart(2, '0');
function clock(ts, withDate) {
  const d = new Date(ts * 1000);
  const time = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  const today = new Date().toDateString() === d.toDateString();
  return withDate || !today ? `${pad(d.getDate())}.${pad(d.getMonth() + 1)} ${time}` : time;
}
function size(kb) {
  if (kb == null) return '—';
  return kb >= 1048576 ? `${(kb / 1048576).toFixed(1)} ГБ` : `${Math.round(kb / 1024)} МБ`;
}

async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'monit' },
    body: JSON.stringify(body),
  };
  const r = await fetch(path, opts);
  if (r.status === 401) {
    location.href = '/login';
    throw new Error('unauthorized');
  }
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || `HTTP ${r.status}`);
  return r.json();
}

function toast(text) {
  const t = $('toast');
  t.textContent = text;
  t.classList.add('show');
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => t.classList.remove('show'), 2600);
}

// A failed poll becomes a failure only once the collector has confirmed it (services: 15 minutes).
const failed = (c) => Boolean(c && c.status === 'fail' && c.down);

function overall(d) {
  const c = d.checks;
  if (!c.reach || c.reach.status === 'unknown') return 'nodata';
  if (c.reach.status === 'fail') return 'offline';
  return CHECKS.some((k) => failed(c[k])) ? 'problem' : 'ok';
}

function checkTip(name, c) {
  if (!c) return `${LABEL[name]}: нет данных`;
  let text = `${LABEL[name]}: ${STATUS[c.status]}`;
  if (c.detail && c.status !== 'na') text += `\n${c.detail}`;
  if (c.status === 'fail' && !c.down) text += '\nсбой ещё не подтверждён, перепроверяется';
  if (c.since && c.status !== 'na') text += `\nуже ${dur(now() - c.since)}`;
  if (c.muted) text += '\nуведомления отключены';
  return text;
}

function badge(name, c) {
  const status = c ? c.status : 'unknown';
  const cls = ['badge', status, status === 'fail' && c && !c.down ? 'pending' : '', name === 'reach' ? 'lead' : '', c && c.muted ? 'is-muted' : ''].join(' ');
  return h('span', { class: cls, html: ICON[status], 'data-tip': checkTip(name, c), role: 'img', 'aria-label': `${LABEL[name]}: ${STATUS[status]}` });
}

function strip(values, start, bucket) {
  const el = h('div', { class: 'strip' });
  values.forEach((v, i) => {
    const from = start + i * bucket;
    el.append(h('i', { class: v === 'na' ? '' : v, 'data-tip': `${clock(from, true)} – ${clock(from + bucket)}\n${STATUS[v] || 'нет данных'}` }));
  });
  return el;
}

// ---- overview ----------------------------------------------------------------

function renderTiles(devices) {
  const tiles = $('tiles');
  tiles.replaceChildren();
  const total = devices.length;
  const offline = devices.filter((d) => overall(d) === 'offline').length;
  const online = devices.filter((d) => ['ok', 'problem'].includes(overall(d))).length;
  tiles.append(h('button', {
    class: 'tile' + (state.filter === 'offline' ? ' active' : ''),
    onclick: () => setFilter(state.filter === 'offline' ? 'all' : 'offline'),
  },
    h('div', { class: 'tile-label' }, 'На связи'),
    h('div', { class: 'tile-value' }, String(online), h('small', {}, ` / ${total}`)),
    meter(online, offline, total),
    h('div', { class: 'tile-note' + (offline ? ' bad' : ' good') }, offline ? `${offline} не в сети` : 'все на связи'),
  ));
  for (const name of CHECKS.slice(1)) {
    let ok = 0, fail = 0;
    for (const d of devices) {
      const c = d.checks[name];
      if (failed(c)) fail++;
      else if (c && (c.status === 'ok' || c.status === 'fail')) ok++;
    }
    tiles.append(h('button', {
      class: 'tile' + (state.check === name ? ' active' : ''),
      'data-tip': fail ? 'Показать роутеры, где не работает' : null,
      onclick: () => { state.check = state.check === name ? null : name; render(); },
    },
      h('div', { class: 'tile-label' }, LABEL[name]),
      h('div', { class: 'tile-value' }, String(ok), h('small', {}, ` / ${ok + fail}`)),
      meter(ok, fail, ok + fail),
      h('div', { class: 'tile-note' + (fail ? ' bad' : ' good') }, fail ? `${fail} не работает` : (ok ? 'везде работает' : 'нет данных')),
    ));
  }
}

function meter(ok, fail, total) {
  const m = h('div', { class: 'meter' });
  const add = (cls, n) => {
    if (!n || !total) return;
    const i = h('i', { class: cls });
    i.style.width = `${(100 * n) / total}%`;
    m.append(i);
  };
  add('m-ok', ok);
  add('m-fail', fail);
  return m;
}

function setFilter(f) {
  state.filter = f;
  render();
}

function renderFilters(devices) {
  const counts = { all: devices.length, problem: 0, offline: 0 };
  for (const d of devices) {
    const o = overall(d);
    if (o === 'problem') counts.problem++;
    if (o === 'offline') counts.offline++;
  }
  const names = { all: 'Все', problem: 'С проблемами', offline: 'Не в сети' };
  $('filters').replaceChildren(...Object.keys(names).map((f) =>
    h('button', { 'aria-pressed': String(state.filter === f), onclick: () => setFilter(f) }, names[f], h('span', { class: 'count' }, String(counts[f])))));
  $('check-filter').replaceChildren(state.check
    ? h('button', { class: 'chip', onclick: () => { state.check = null; render(); } }, `Не работает: ${LABEL[state.check]}`, h('span', { html: ICON.close }))
    : '');
}

function visibleDevices(devices) {
  const rank = { problem: 0, offline: 1, nodata: 2, ok: 3 };
  const query = state.search.trim().toLowerCase();
  return devices
    .filter((d) => {
      const o = overall(d);
      if (state.filter !== 'all' && o !== state.filter) return false;
      if (state.check && !failed(d.checks[state.check])) return false;
      if (!query) return true;
      return [d.name, d.hostname, d.ip, d.info.model, d.info.release].some((v) => v && String(v).toLowerCase().includes(query));
    })
    .sort((a, b) => rank[overall(a)] - rank[overall(b)] || a.name.localeCompare(b.name));
}

function renderTable(data) {
  $('thead').replaceChildren(h('tr', {},
    h('th', {}, 'Роутер'),
    CHECKS.map((c) => h('th', {}, LABEL[c])),
    h('th', {}, 'Последние 24 часа'),
    h('th', { class: 'right' }, 'Проверен'),
  ));
  const list = visibleDevices(data.devices);
  const bucket = 86400 / 48;
  $('rows').replaceChildren(...list.map((d) => {
    const o = overall(d);
    const sub = [d.info.model, d.info.release && `OpenWrt ${d.info.release}`].filter(Boolean).join(' · ') || d.ip;
    return h('tr', { class: o === 'offline' ? 'is-offline' : '', tabindex: '0', onclick: () => openDrawer(d.id), onkeydown: (e) => { if (e.key === 'Enter') openDrawer(d.id); } },
      h('td', {}, h('div', { class: 'r-name' },
        h('i', { class: `dot ${o}`, 'data-tip': OVERALL[o] }),
        h('div', {},
          h('div', { class: 'r-title' }, d.name, d.muted ? h('span', { html: ICON.bellOff, 'data-tip': 'Уведомления отключены' }) : null),
          h('div', { class: 'r-sub' }, sub)),
      )),
      CHECKS.map((c) => h('td', { 'data-label': SHORT[c] }, badge(c, d.checks[c]))),
      h('td', { class: 'c-strip' }, d.history && d.history.length ? strip(d.history, data.now - 86400, bucket) : ''),
      h('td', { class: 'right cell-time' }, d.probe_ts ? ago(d.probe_ts) : '—'),
    );
  }));
  $('empty').hidden = list.length > 0;
}

function eventLine(e, withDevice) {
  const down = e.kind === 'down';
  const what = h('div', { class: 'what' });
  if (withDevice) what.append(h('button', { class: 'link', onclick: () => openDrawer(e.device_id) }, e.device), ' · ');
  what.append(h('b', {}, LABEL[e.check] || e.check), down ? ' перестал работать' : ' снова работает');
  if (down && e.detail) what.append(h('span', {}, ` — ${e.detail}`));
  if (!down && e.duration != null) what.append(h('span', {}, ` — не работало ${dur(e.duration)}`));
  return h('li', {},
    h('span', { class: 'when' }, clock(e.ts)),
    h('span', { class: `badge ${down ? 'fail' : 'ok'}`, html: ICON[down ? 'fail' : 'ok'] }),
    what);
}

function renderEvents(events) {
  $('events').replaceChildren(...(events.length
    ? events.map((e) => eventLine(e, true))
    : [h('li', {}, h('span', { class: 'what' }, h('span', {}, 'Событий пока нет')))]));
}

function renderLive() {
  const d = state.data;
  if (!d) return;
  const live = $('live');
  const age = now() - d.last_cycle;
  live.className = 'live';
  let text;
  if (d.error) {
    live.classList.add('error');
    text = 'Ошибка опроса';
  } else if (d.running) {
    live.classList.add('busy');
    text = 'Идёт проверка…';
  } else if (!d.last_cycle) {
    text = 'Ожидание первой проверки';
  } else {
    if (age > d.interval * 3) live.classList.add('stale');
    text = `Проверено ${dur(age)} назад`;
  }
  $('live-text').textContent = text;
  $('banner').replaceChildren(d.error ? h('div', { class: 'banner' }, `Сборщик не смог выполнить проверку: ${d.error}`) : '');
}

// One button for the whole fleet: starts a staged rollout, shows its progress, stops it.
function renderForkopAll(d) {
  const f = d.forkop;
  const box = $('forkop-all');
  if (!f) return box.replaceChildren();
  const online = new Set(d.devices.filter((x) => x.checks.reach && x.checks.reach.status === 'ok').map((x) => x.id));
  const outdated = Object.entries(f.devices).filter(([id, p]) => p.outdated && online.has(id));
  const c = f.rollout.counts;
  if (f.rollout.active) {
    const total = Object.values(c).reduce((a, b) => a + b, 0);
    return box.replaceChildren(h('span', { class: 'chip' }, `forkop обновляется: ${(c.done || 0) + (c.failed || 0)} из ${total}`),
      h('button', { class: 'btn', onclick: async () => {
        await api('/api/forkop/cancel', {}).catch((e) => toast(`Ошибка: ${e.message}`));
        toast('Остальные роутеры пропущены; текущий доделывается');
        load();
      } }, 'Остановить'));
  }
  box.replaceChildren(h('button', {
    class: 'btn', disabled: !outdated.length,
    'data-tip': outdated.length
      ? `Последняя версия: ${f.latest.stable || '?'} (canary ${f.latest.canary || '?'}). Сначала обновится один роутер, затем остальные по два; при первой неудаче обновление остановится.`
      : (f.latest.stable ? `На всех доступных роутерах уже последняя версия (${f.latest.stable})` : 'Не удалось узнать последнюю версию forkop'),
    onclick: async () => {
      if (!window.confirm(`Обновить forkop на ${outdated.length} роутерах? На время обновления интернет у людей за роутером может пропадать на минуту-две.`)) return;
      try {
        const r = await api('/api/forkop/update', { devices: 'all' });
        toast(`Обновление запущено: роутеров ${r.count}`);
        load();
      } catch (e) {
        toast(`Ошибка: ${e.message}`);
      }
    },
  }, h('span', { html: ICON.update }), outdated.length ? `Обновить forkop у всех · ${outdated.length}` : 'forkop обновлён у всех'));
}

// The same for sing-box: every router where forkop has found a newer version of the installed build.
function renderSingboxAll(d) {
  const s = d.singbox;
  const box = $('singbox-all');
  if (!s) return box.replaceChildren();
  // the periodic refresh must not close the list while a build is being chosen
  if (!s.rollout.active && document.activeElement && document.activeElement.tagName === 'SELECT' && box.contains(document.activeElement)) return;
  const c = s.rollout.counts;
  if (s.rollout.active) {
    const total = Object.values(c).reduce((a, b) => a + b, 0);
    return box.replaceChildren(h('span', { class: 'chip' }, `sing-box, ${SINGBOX_ACTION[s.rollout.action] || 'обновление'}: ${total - (c.queued || 0) - (c.running || 0)} из ${total}`),
      h('button', { class: 'btn', onclick: async () => {
        try {
          await api('/api/singbox/cancel', {});
          toast('Остальные роутеры пропущены; текущий доделывается');
        } catch (e) {
          toast(`Ошибка: ${e.message}`);
        }
        load();
      } }, 'Остановить'));
  }
  const online = new Set(d.devices.filter((x) => x.checks.reach && x.checks.reach.status === 'ok').map((x) => x.id));
  const outdated = s.outdated.filter((id) => online.has(id));
  box.replaceChildren(h('button', {
    class: 'btn', disabled: !outdated.length || (d.forkop && d.forkop.rollout.active),
    'data-tip': outdated.length
      ? 'Обновляется установленная сборка, тип сборки не меняется. Сначала один роутер, затем остальные по два; при первой неудаче обновление остановится.'
      : 'Ни на одном доступном роутере forkop не нашёл новой версии sing-box. Роутеры, где обновления ещё не проверялись, сюда не попадают.',
    onclick: async () => {
      if (!window.confirm(`Обновить sing-box на ${outdated.length} роутерах? На каждом интернет пропадёт на несколько минут, пока forkop меняет sing-box.`)) return;
      try {
        const r = await api('/api/singbox/update', {});
        toast(`Обновление sing-box запущено: роутеров ${r.count}`);
        load();
      } catch (e) {
        toast(`Ошибка: ${e.message}`);
      }
    },
  }, h('span', { html: ICON.update }), outdated.length ? `Обновить sing-box у всех · ${outdated.length}` : 'sing-box обновлён у всех'),
  buildSelect(d, online));
}

// Moves the whole fleet to one build; the number is how many reachable routers have another one.
function buildSelect(d, online) {
  const known = d.devices.filter((x) => online.has(x.id) && x.info && x.info.singbox);
  const select = h('select', { class: 'btn', 'aria-label': 'Установить сборку sing-box у всех', disabled: d.forkop && d.forkop.rollout.active,
    'data-tip': 'Ставит выбранную сборку на все доступные роутеры, где стоит другая. Сначала один роутер, затем остальные по два; при первой неудаче установка остановится.',
    onchange: async () => {
      const build = select.value;
      const label = select.selectedOptions[0].dataset.label;
      const count = known.filter((x) => x.info.singbox.variant !== build).length;
      select.value = '';
      select.blur();
      if (!build || !window.confirm(`Установить ${label} на ${count} роутерах? На каждом интернет пропадёт на несколько минут. `
        + 'Если на роутере не хватит места, forkop откажется ставить сборку, и установка остановится на нём; при повторном запуске такой роутер пойдёт последним.')) return;
      try {
        const r = await api('/api/singbox/update', { build });
        toast(`Установка ${label} запущена: роутеров ${r.count}`);
        load();
      } catch (e) {
        toast(`Ошибка: ${e.message}`);
      }
    } },
    h('option', { value: '' }, 'Установить сборку у всех…'),
    SINGBOX_BUILDS.map(([build, , label]) => {
      const count = known.filter((x) => x.info.singbox.variant !== build).length;
      return h('option', { value: build, 'data-label': label, disabled: !count }, `${label} · ${count}`);
    }));
  return select;
}

function render() {
  const d = state.data;
  if (!d) return;
  renderForkopAll(d);
  renderSingboxAll(d);
  renderTiles(d.devices);
  renderFilters(d.devices);
  renderTable(d);
  renderEvents(d.events);
  renderLive();
}

// ---- drawer ------------------------------------------------------------------

async function openDrawer(id) {
  state.openId = id;
  state.detail = null;
  $('drawer').classList.add('open');
  $('scrim').classList.add('open');
  $('drawer').setAttribute('aria-hidden', 'false');
  renderDrawer();
  await loadDetail();
}

function closeDrawer() {
  clearTimeout(state.jobTimer);
  state.openId = null;
  $('drawer').classList.remove('open');
  $('scrim').classList.remove('open');
  $('drawer').setAttribute('aria-hidden', 'true');
}

async function loadDetail() {
  const id = state.openId;
  if (!id) return;
  try {
    const detail = await api(`/api/devices/${encodeURIComponent(id)}?hours=${state.hours}`);
    if (state.openId === id) {
      state.detail = detail;
      renderDrawer();
      // a running repair is followed closely, everything else at the normal pace
      clearTimeout(state.jobTimer);
      const updating = detail.forkop_update && UPDATE_RUNNING.includes(detail.forkop_update.stage);
      const swapping = detail.singbox && detail.singbox.action && ['queued', 'running'].includes(detail.singbox.action.stage);
      if ((detail.job && JOB_RUNNING.includes(detail.job.stage)) || updating || swapping) state.jobTimer = setTimeout(loadDetail, swapping ? 2500 : 4000);
    }
  } catch (e) {
    if (e.message !== 'unauthorized') toast(`Не удалось загрузить: ${e.message}`);
  }
}

function fact(label, value, extra) {
  return h('div', {}, h('dt', {}, label), h('dd', {}, value || '—'), extra);
}

function usage(total, avail) {
  if (!total) return null;
  const used = (total - avail) / total;
  const i = h('i', { class: used > 0.9 ? 'm-fail' : used > 0.75 ? 'm-warn' : 'm-accent' });
  i.style.width = `${Math.round(used * 100)}%`;
  return h('div', { class: 'meter' }, i);
}

const STAGE = {
  routing: 'разбираю запрос', investigating: 'ищу причину', awaiting: 'ждёт подтверждения', executing: 'выполняю',
  done: 'готово', failed: 'ошибка', cancelled: 'отменено',
};
const JOB_RUNNING = ['routing', 'investigating', 'executing'];

// The latest repair job for this router: diagnosis, plan to approve, result.
function jobCard(d) {
  const j = d.job;
  if (!j) return null;
  const act = async (action, done) => {
    try {
      await api(`/api/jobs/${j.id}/${action}`, {});
      toast(done);
      await loadDetail();
    } catch (e) {
      toast(`Ошибка: ${e.message}`);
    }
  };
  const mine = j.results.filter((r) => r.id === d.id);
  const failed = j.stage === 'failed' || mine.some((r) => !r.ok);
  const box = h('div', { class: 'job' });
  for (const g of j.plan.filter((x) => x.devices.some((dev) => dev.id === d.id))) {
    box.append(h('p', {}, h('b', {}, g.checks.map((c) => LABEL[c]).join(', ') || 'Проблема'), ` — ${g.diagnosis || ''}`));
    if (!g.fixable) {
      if (!g.healthy) box.append(h('p', { class: 'job-note' }, 'С роутера это не исправить.'));
    }
    else if (!mine.length) {
      box.append(h('ol', {}, g.steps.map((s) => h('li', {}, h('code', {}, s.command), h('small', {}, s.why)))));
      if (g.cached) box.append(h('p', { class: 'job-note' }, 'План взят из сохранённых решений — он уже помогал при таком же сбое.'));
    }
  }
  for (const r of mine) {
    box.append(h('p', { class: r.ok ? 'job-ok' : 'job-bad' },
      h('b', {}, r.ok ? 'Исправлено' : 'Не исправлено'),
      r.summary ? ` — ${r.summary}` : '', r.verdict ? ` Проба: ${r.verdict}.` : ''));
  }
  if (j.error) box.append(h('p', { class: 'job-bad' }, j.error));
  const others = new Set(j.plan.flatMap((g) => g.devices.map((dev) => dev.id))).size - 1;
  const buttons = h('div', { class: 'actions' });
  if (j.stage === 'awaiting') {
    buttons.append(
      h('button', { class: 'btn primary', onclick: () => act('confirm', 'Выполняю план') },
        others > 0 ? `Выполнить на ${others + 1} роутерах` : 'Выполнить'),
      h('button', { class: 'btn', onclick: () => act('cancel', 'Отменено') }, 'Отмена'));
  } else if (JOB_RUNNING.includes(j.stage)) {
    buttons.append(h('button', { class: 'btn', onclick: () => act('cancel', 'Отменено') }, 'Отменить'));
  } else if (j.stage === 'done' && mine.some((r) => !r.ok)) {
    buttons.append(h('button', { class: 'btn', onclick: () => act('deepen', 'Исследую заново') }, 'Разобраться глубже'));
  }
  if (buttons.children.length) box.append(buttons);
  return h('div', { class: 'card' }, h('div', { class: 'card-head' }, h('h3', {}, 'Исправление'),
    h('span', { class: `status-pill ${failed ? 'problem' : j.stage === 'done' ? 'ok' : 'nodata'}` },
      `${STAGE[j.stage] || j.stage} · ${ago(j.updated || j.ts)}`)), box);
}

const UPDATE_STAGE = { queued: 'в очереди', running: 'обновляется', done: 'обновлён', failed: 'не удалось', skipped: 'пропущен' };
const UPDATE_RUNNING = ['queued', 'running'];

async function updateForkop(d, extra) {
  try {
    await api('/api/forkop/update', { devices: [d.id], ...extra });
    toast('Обновление forkop запущено');
    await Promise.all([load(), loadDetail()]);
  } catch (e) {
    toast(`Ошибка: ${e.message}`);
  }
}

// The last forkop update of this router: versions, the installer's own output, retries.
function updateCard(d) {
  const u = d.forkop_update;
  if (!u) return null;
  const box = h('div', { class: 'job' },
    h('p', {}, h('b', {}, `${u.from_version} → ${u.to_version}`), ` · канал ${u.channel}${u.args ? ` · ${u.args}` : ''}`));
  if (u.log) box.append(h('pre', { class: 'log' }, u.log));
  if (u.stage === 'failed') {
    const buttons = h('div', { class: 'actions' });
    if ((u.log || '').includes('--allow-low-space-tiny')) {
      buttons.append(h('button', { class: 'btn', 'data-tip': 'На роутере мало места: установщик заменит sing-box на облегчённую сборку tiny',
        onclick: () => updateForkop(d, { allow_tiny: true }) }, 'Повторить, разрешив sing-box tiny'));
    }
    // No retry for "legacy migration": it replaces the current forkop settings with the old package's file.
    if (buttons.children.length) box.append(buttons);
  }
  return h('div', { class: 'card' }, h('div', { class: 'card-head' }, h('h3', {}, 'Обновление forkop'),
    h('span', { class: `status-pill ${u.stage === 'done' ? 'ok' : u.stage === 'failed' ? 'problem' : 'nodata'}` },
      `${UPDATE_STAGE[u.stage] || u.stage} · ${ago(u.updated || u.ts)}`)), box);
}

const SINGBOX_ACTION = {
  check_update: 'проверка обновления', install: 'обновление', install_x: 'установка Sing-Box X',
  install_extended: 'установка Extended', install_extended_compressed: 'установка Extended compressed',
  install_x_clean: 'Sing-Box X с удалением прежней сборки',
};
const SINGBOX_BUILDS = [['x', 'x', 'Sing-Box X'], ['extended', 'extended', 'Extended'], ['compressed', 'compressed', 'Extended compressed']];

async function singboxAction(d, action, started) {
  try {
    await api(`/api/devices/${encodeURIComponent(d.id)}/singbox`, { action });
    toast(started);
    await loadDetail();
  } catch (e) {
    toast(`Ошибка: ${e.message}`);
  }
}

// sing-box on this router: installed build, update check, update and switching the build.
// forkop on the router does all of it, the same way its own LuCI page does.
function singboxCard(d, blocked) {
  const s = d.singbox;
  if (!s) return null;
  const a = s.action;
  const running = Boolean(a && (a.stage === 'running' || a.stage === 'queued'));
  const off = blocked || running;
  const install = (action, what) => {
    if (window.confirm(`${what} на ${d.name}? Интернет за роутером пропадёт на несколько минут: forkop останавливается, пока меняется sing-box. Если что-то пойдёт не так, forkop сам вернёт прежнюю сборку.`)) {
      singboxAction(d, action, 'Запущено — forkop меняет sing-box');
    }
  };
  const note = { tiny: ' (tiny)', compressed: ' (compressed)' }[s.variant] || '';
  const box = h('div', { class: 'job' },
    h('p', {}, h('b', {}, s.variant === 'x' ? 'Sing-Box X' : 'Sing-box'), ` ${s.version}${note}`),
    h('p', {}, s.outdated ? ['Доступна новая версия: ', h('b', {}, s.latest)]
      : s.latest ? `Установлена последняя версия · проверено ${ago(s.checked)}` : 'Обновления ещё не проверялись'),
    h('div', { class: 'actions' },
      h('button', { class: 'btn', disabled: off, onclick: () => singboxAction(d, 'check', 'Проверяю обновление sing-box') },
        h('span', { html: ICON.search }), 'Проверить обновление'),
      h('button', { class: 'btn' + (s.outdated ? ' primary' : ''), disabled: off || !s.outdated,
        'data-tip': s.outdated ? `Обновить установленную сборку до ${s.latest}` : 'Новой версии нет — сначала проверьте обновление',
        onclick: () => install('update', `Обновить sing-box до ${s.latest}`) },
        h('span', { html: ICON.refresh }), 'Обновить')),
    h('p', { class: 'job-note' }, 'Установить другую сборку:'),
    h('div', { class: 'actions' }, SINGBOX_BUILDS.filter(([, variant]) => variant !== s.variant).map(([action, , label]) =>
      h('button', { class: 'btn', disabled: off, onclick: () => install(action, `Установить ${label}`) },
        h('span', { html: ICON.update }), label))));
  // a finished check has already said everything in the line above
  const shown = a && !(a.action === 'check_update' && a.stage === 'done');
  if (shown && a.log) box.append(h('pre', { class: 'log' }, a.log));
  // forkop refused X because it wants room for the old and the new sing-box together
  if (a && a.action === 'install_x' && a.stage === 'failed' && (a.log || '').includes('Not enough flash space') && s.variant !== 'x') {
    box.append(h('div', { class: 'actions' }, h('button', {
      class: 'btn', disabled: off,
      'data-tip': 'X меньше прежней сборки, но forkop требует место под обе сразу. Здесь прежний пакет сначала скачивается в память роутера для отката, затем удаляется, и forkop ставит X на освободившееся место.',
      onclick: () => {
        if (window.confirm(`Удалить прежний sing-box на ${d.name} и поставить Sing-Box X? Интернет за роутером пропадёт на несколько минут. `
          + 'Это в обход проверки места forkop: если X не встанет, прежний пакет возвращается из памяти роутера, но гарантии forkop здесь уже нет.')) {
          singboxAction(d, 'x_clean', 'Запущено — прежний sing-box удаляется, ставится X');
        }
      },
    }, h('span', { html: ICON.update }), 'Поставить X, удалив прежнюю сборку')));
  }
  return h('div', { class: 'card' }, h('div', { class: 'card-head' }, h('h3', {}, 'Sing-box'),
    shown ? h('span', { class: `status-pill ${a.stage === 'done' ? 'ok' : a.stage === 'failed' ? 'problem' : 'nodata'}` },
      `${SINGBOX_ACTION[a.action] || a.action}: ${{ queued: 'в очереди', running: 'идёт', done: 'готово', failed: 'не удалось', skipped: 'пропущено' }[a.stage] || a.stage} · ${ago(a.updated || a.ts)}`) : null), box);
}

function renderDrawer() {
  const drawer = $('drawer');
  const base = state.data && state.data.devices.find((x) => x.id === state.openId);
  const d = state.detail && state.detail.id === state.openId ? state.detail : base;
  if (!d) {
    drawer.replaceChildren();
    return;
  }
  const keepScroll = drawer.querySelector('.drawer-body') ? drawer.querySelector('.drawer-body').scrollTop : 0;
  const o = overall(d);
  const info = d.info || {};
  const hist = d.history && d.history.strips ? d.history : null;

  const head = h('div', { class: 'drawer-head' },
    h('div', { style: null },
      h('h2', {}, d.name),
      h('p', {}, [d.ip, d.hostname && d.hostname !== d.name ? `hostname ${d.hostname}` : null].filter(Boolean).join(' · '))),
    h('div', { class: 'spacer' }),
    h('button', { class: 'icon-btn', 'aria-label': 'Закрыть', html: ICON.close, onclick: closeDrawer }));

  const probeBtn = h('button', { class: 'btn primary', onclick: async () => {
    probeBtn.disabled = true;
    probeBtn.lastChild.textContent = 'Проверяю…';
    try {
      await api(`/api/devices/${encodeURIComponent(d.id)}/probe`, {});
      await Promise.all([load(), loadDetail()]);
      toast('Проверка выполнена');
    } catch (e) {
      toast(`Ошибка: ${e.message}`);
      renderDrawer();
    }
  } }, h('span', { html: ICON.refresh }), h('span', {}, 'Проверить сейчас'));

  const muteBtn = h('button', { class: 'btn' + (d.muted ? ' on' : ''), onclick: async () => {
    await api(`/api/devices/${encodeURIComponent(d.id)}/mute`, { muted: !d.muted });
    toast(d.muted ? 'Уведомления включены' : 'Уведомления по роутеру отключены');
    await Promise.all([load(), loadDetail()]);
  } }, h('span', { html: d.muted ? ICON.bellOff : ICON.bell }), d.muted ? 'Уведомления отключены' : 'Уведомления включены');

  const luciBtn = h('a', {
    class: 'btn', href: `/luci/${encodeURIComponent(d.id)}/`, target: '_blank', rel: 'noopener',
    'data-tip': 'Веб-интерфейс роутера через дашборд, без подключения к tailscale',
  }, h('span', { html: ICON.external }), 'LuCI');

  const broken = o !== 'offline' && o !== 'nodata' && CHECKS.some((k) => d.checks[k] && d.checks[k].status === 'fail');
  const working = d.job && [...JOB_RUNNING, 'awaiting'].includes(d.job.stage);
  const fixBtn = broken && h('button', {
    class: 'btn', disabled: working || !state.data.claude,
    'data-tip': !state.data.claude ? 'Claude не авторизован: отправьте боту команду /login'
      : 'Claude найдёт причину сбоя и предложит план исправления',
    onclick: async () => {
      fixBtn.disabled = true;
      try {
        await api(`/api/devices/${encodeURIComponent(d.id)}/fix`, {});
        toast('Claude ищет причину — план появится здесь и в Telegram');
        await loadDetail();
      } catch (e) {
        toast(`Ошибка: ${e.message}`);
        renderDrawer();
      }
    },
  }, h('span', { html: ICON.wrench }), 'Исправить');

  const fp = state.data.forkop && state.data.forkop.devices[d.id];
  const updating = d.forkop_update && UPDATE_RUNNING.includes(d.forkop_update.stage);
  const swapping = Boolean(d.singbox && d.singbox.action && ['queued', 'running'].includes(d.singbox.action.stage));
  const updateBtn = fp && h('button', {
    class: 'btn', disabled: !fp.outdated || updating || working || swapping || o === 'offline' || o === 'nodata' || state.data.forkop.rollout.active,
    'data-tip': fp.outdated
      ? `Установщик из репозитория Screamshow/forkop: ${fp.current} → ${fp.target} (канал ${fp.channel}). Настройки сохраняются, при сбое установщик сам откатывает версию.`
      : `Установлена ${fp.current}${fp.target ? ', это последняя версия канала ' + fp.channel : ''}`,
    onclick: () => {
      if (window.confirm(`Обновить forkop на ${d.name}: ${fp.current} → ${fp.target}? Интернет за роутером может пропасть на минуту-две.`)) updateForkop(d, {});
    },
  }, h('span', { html: ICON.update }), fp.outdated ? `Обновить forkop до ${fp.target}` : 'forkop обновлён');

  const actions = h('div', { class: 'actions' },
    h('span', { class: `status-pill ${o}` }, OVERALL[o]),
    h('div', { class: 'spacer' }), fixBtn, updateBtn, luciBtn, muteBtn, probeBtn);

  const ranges = h('div', { class: 'segmented' }, [[24, '24 ч'], [168, '7 дней'], [720, '30 дней']].map(([hrs, label]) =>
    h('button', { 'aria-pressed': String(state.hours === hrs), onclick: () => { state.hours = hrs; renderDrawer(); loadDetail(); } }, label)));

  const checks = h('div', { class: 'card' }, h('div', { class: 'card-head' }, h('h3', {}, 'Проверки'), ranges));
  for (const name of CHECKS) {
    const c = d.checks[name];
    if (!c) continue;
    const up = hist && hist.uptime[name];
    const row = h('div', { class: 'check' },
      badge(name, c),
      h('div', { class: 'check-title' }, LABEL[name],
        h('small', {}, c.status === 'na' ? 'не установлен' : `${STATUS[c.status]} ${c.since ? dur(now() - c.since) : ''}`)),
      h('div', { class: 'check-side' },
        up != null ? h('span', { 'data-tip': 'Доля успешных проверок за период' }, `${up}%`) : null,
        h('button', {
          class: 'bell' + (c.muted ? ' off' : ''), html: c.muted ? ICON.bellOff : ICON.bell,
          'data-tip': c.muted ? 'Уведомления по этой проверке отключены' : 'Отключить уведомления по этой проверке',
          'aria-label': 'Уведомления по проверке',
          onclick: async () => {
            await api(`/api/devices/${encodeURIComponent(d.id)}/mute`, { muted: !c.muted, check: name });
            await Promise.all([load(), loadDetail()]);
          },
        })),
      c.detail && c.status !== 'na' ? h('div', { class: 'check-detail' }, c.detail) : null,
      hist && c.status !== 'na' ? strip(hist.strips[name], hist.start, hist.bucket) : null);
    checks.append(row);
  }

  const system = h('div', { class: 'card' }, h('div', { class: 'card-head' }, h('h3', {}, 'Система')),
    h('dl', { class: 'facts' },
      fact('Модель', info.model),
      fact('OpenWrt', info.release),
      fact('Аптайм', info.uptime != null ? dur(info.uptime) : null),
      fact('Нагрузка', info.load),
      fact('Память', info.mem_total ? `свободно ${size(info.mem_avail)} из ${size(info.mem_total)}` : null, usage(info.mem_total, info.mem_avail)),
      info.swap_total ? fact('Swap', `свободно ${size(info.swap_free)} из ${size(info.swap_total)}`, usage(info.swap_total, info.swap_free)) : null,
      info.rss ? fact('Память процессов', Object.entries(info.rss).sort((a, b) => b[1] - a[1]).map(([n, kb]) => `${n} ${size(kb)}`).join(' · ')) : null,
      info.oom != null ? fact('Убито из-за нехватки памяти', info.oom
        ? `${info.oom} раз по журналу ядра${info.oom_last ? ` · последним ${info.oom_last.name}, ${ago(info.oom_last.ts)}` : ''}` : 'нет') : null,
      fact('Накопитель', info.ovl_total ? `свободно ${size(info.ovl_avail)} из ${size(info.ovl_total)}` : null, usage(info.ovl_total, info.ovl_avail)),
      fact('zapret', (info.zapret || []).join(', ') || 'не установлен'),
      fact('forkop', info.forkop_version),
      fact('sing-box', info.singbox ? `${info.singbox.version}${info.singbox.variant === 'stable' ? '' : ` (${info.singbox.variant})`}` : null),
      fact('Выход ChatGPT', info.gpt_loc),
      fact('В сети tailscale', d.online ? 'сейчас' : ago(d.last_seen)),
      fact('Данные получены', d.probe_ts ? ago(d.probe_ts) : null),
      fact('Источник данных', info.source === 'agent' ? 'агент на роутере' : info.source === 'ssh' ? 'опрос по SSH с сервера' : null),
    ));

  const body = h('div', { class: 'drawer-body' }, actions, jobCard(d), updateCard(d),
    singboxCard(d, updating || working || o === 'offline' || o === 'nodata'), checks);
  if (info.proxies && info.proxies.length) {
    body.append(h('div', { class: 'card' }, h('div', { class: 'card-head' }, h('h3', {}, 'Прокси forkop')),
      h('ul', { class: 'proxies' }, info.proxies.map((p) => h('li', {},
        h('div', {}, h('div', {}, p.now || '—'), h('div', { class: 'p-group' }, p.name)),
        h('div', { class: 'p-delay' }, p.delay ? `${p.delay} мс` : ''))))));
  }
  body.append(system);
  if (d.agent_command) {
    body.append(h('div', { class: 'card' }, h('div', { class: 'card-head' }, h('h3', {}, 'Агент на роутере'),
      h('span', { class: `status-pill ${d.agent ? 'ok' : 'nodata'}` }, d.agent ? 'присылает данные' : 'не установлен или молчит')),
      h('div', { class: 'agent' },
        h('p', {}, 'Агент сам отправляет состояние роутера на дашборд по HTTPS и не зависит от tailscale. Чтобы установить или обновить, выполните команду в терминале роутера:'),
        h('code', {}, d.agent_command),
        h('button', { class: 'btn', onclick: async () => {
          try {
            await navigator.clipboard.writeText(d.agent_command);
            toast('Команда скопирована');
          } catch (_) {
            toast('Не удалось скопировать — выделите текст вручную');
          }
        } }, 'Скопировать команду'))));
  }
  if (d.events) {
    body.append(h('div', { class: 'card' }, h('div', { class: 'card-head' }, h('h3', {}, 'События')),
      h('ul', { class: 'events' }, d.events.length
        ? d.events.map((e) => eventLine(e, false))
        : h('li', {}, h('span', { class: 'what' }, h('span', {}, 'Сбоев не зафиксировано'))))));
  }
  drawer.replaceChildren(head, body);
  body.scrollTop = keepScroll;
}

// ---- data loop ---------------------------------------------------------------

async function load() {
  try {
    state.data = await api('/api/overview');
    state.loadedAt = Date.now();
    render();
    if (state.openId && !state.detail) renderDrawer();
  } catch (e) {
    if (e.message === 'unauthorized') return;
    $('live').className = 'live error';
    $('live-text').textContent = 'Нет связи с сервером';
  }
}

async function tick() {
  if (document.hidden) return;
  await load();
  if (state.openId) await loadDetail();
}

// ---- wiring ------------------------------------------------------------------

$('search').addEventListener('input', (e) => { state.search = e.target.value; if (state.data) { renderTable(state.data); } });
$('scrim').addEventListener('click', closeDrawer);
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeDrawer(); });
document.addEventListener('visibilitychange', () => { if (!document.hidden) tick(); });

$('refresh').addEventListener('click', async () => {
  $('refresh').classList.add('spin');
  try {
    await api('/api/refresh', {});
    toast('Запущена проверка всех роутеров');
    setTimeout(load, 1500);
  } catch (e) {
    toast(`Ошибка: ${e.message}`);
  }
  setTimeout(() => $('refresh').classList.remove('spin'), 1200);
});

$('theme').addEventListener('click', () => {
  const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem('theme', next); } catch (_) { /* private mode */ }
});

$('logout').addEventListener('click', async () => {
  await api('/api/logout', {}).catch(() => {});
  location.href = '/login';
});

const tip = $('tip');
document.addEventListener('mouseover', (e) => {
  const target = e.target.closest ? e.target.closest('[data-tip]') : null;
  if (!target) {
    tip.classList.remove('show');
    return;
  }
  tip.textContent = target.dataset.tip;
  tip.classList.add('show');
  const r = target.getBoundingClientRect();
  const t = tip.getBoundingClientRect();
  let top = r.top - t.height - 8;
  if (top < 8) top = r.bottom + 8;
  tip.style.top = `${top}px`;
  tip.style.left = `${Math.min(window.innerWidth - t.width - 8, Math.max(8, r.left + r.width / 2 - t.width / 2))}px`;
});
document.addEventListener('scroll', () => tip.classList.remove('show'), true);

load();
setInterval(tick, REFRESH_MS);
setInterval(renderLive, 1000);
