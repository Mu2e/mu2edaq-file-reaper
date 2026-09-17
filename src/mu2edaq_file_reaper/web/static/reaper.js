/* Shared dashboard helpers for mu2edaq File Reaper.
 *
 * Ported from mu2edaq-diskwatcher so the two applications look and behave the
 * same: each page registers its sortable tables with `registerTable()`,
 * supplies a render function and calls `startDashboard()`.  Sorting, the
 * refresh controls, stat cards and cell builders live here.
 *
 * Additions over diskwatcher: `apiFetch()` / `postJSON()` / `del()` carry the
 * admin session's CSRF token to the REST API, `showToast()` replaces browser
 * alert() (browser dialogs are never used), and `reasonModal()` collects an
 * optional reason before a mutating call.
 */

function escHtml(s) {
  if (s === null || s === undefined) return '';
  return String(s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

const DASH = '—';
const IS_ADMIN = document.documentElement.getAttribute('data-admin') === '1';

// ---- formatting --------------------------------------------------------
function fmtBytes(n) {
  if (n === null || n === undefined || isNaN(n)) return DASH;
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB'];
  let v = Number(n);
  for (const u of units) {
    if (Math.abs(v) < 1024) return v.toFixed(1) + ' ' + u;
    v /= 1024;
  }
  return v.toFixed(1) + ' EiB';
}

function fmtAge(seconds) {
  if (seconds === null || seconds === undefined || isNaN(seconds) || seconds < 0) return DASH;
  let s = Math.floor(seconds);
  const d = Math.floor(s / 86400); s -= d * 86400;
  const h = Math.floor(s / 3600);  s -= h * 3600;
  const m = Math.floor(s / 60);    s -= m * 60;
  if (d) return d + 'd ' + h + 'h ' + m + 'm';
  if (h) return h + 'h ' + m + 'm ' + s + 's';
  if (m) return m + 'm ' + s + 's';
  return s + 's';
}

// Accepts epoch seconds or an ISO-8601 string; returns a Date or null.
function toDate(v) {
  if (v === null || v === undefined || v === '') return null;
  if (typeof v === 'number') return new Date(v * 1000);
  const d = new Date(v);
  return isNaN(d.getTime()) ? null : d;
}

function fmtTs(v) {
  const d = toDate(v);
  if (!d) return DASH;
  const pad = n => String(n).padStart(2, '0');
  return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' +
         pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
}

// "3h 12m ago" style age from an epoch/ISO value.
function fmtAgo(v) {
  const d = toDate(v);
  if (!d) return DASH;
  return fmtAge((Date.now() - d.getTime()) / 1000) + ' ago';
}

function fmtPct(v, digits) {
  if (v === null || v === undefined || isNaN(v)) return DASH;
  return Number(v).toFixed(digits === undefined ? 1 : digits) + '%';
}

function qs(params) {
  const parts = [];
  for (const [k, v] of Object.entries(params || {})) {
    if (v === null || v === undefined || v === '') continue;
    parts.push(encodeURIComponent(k) + '=' + encodeURIComponent(v));
  }
  return parts.length ? '?' + parts.join('&') : '';
}

function jsonPre(obj) {
  return '<pre class="json-pre mb-0">' + escHtml(JSON.stringify(obj, null, 2)) + '</pre>';
}

// ---- API access --------------------------------------------------------
function csrfToken() {
  const meta = document.querySelector('meta[name="csrf-token"]');
  return meta ? meta.getAttribute('content') || '' : '';
}

// fetch() with the admin session cookie and CSRF header.  Resolves to the
// parsed JSON body; rejects with an Error whose message is the server's
// `error` field (or the HTTP status) so callers can toast it directly.
function apiFetch(url, opts) {
  opts = Object.assign({cache: 'no-store'}, opts || {});
  opts.credentials = 'same-origin';
  opts.headers = Object.assign({'X-CSRF-Token': csrfToken()}, opts.headers || {});
  return fetch(url, opts).then(r => {
    return r.text().then(text => {
      let data = null;
      try { data = text ? JSON.parse(text) : null; } catch (e) { data = {raw: text}; }
      if (!r.ok) {
        const msg = (data && data.error) ? data.error : ('HTTP ' + r.status);
        const err = new Error(msg);
        err.status = r.status;
        err.data = data;
        throw err;
      }
      return data;
    });
  });
}

function postJSON(url, body) {
  return apiFetch(url, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body || {}),
  });
}

function del(url) {
  return apiFetch(url, {method: 'DELETE'});
}

// ---- toasts ------------------------------------------------------------
function showToast(msg, kind) {
  const box = document.getElementById('toast-container');
  if (!box) { console.log('[toast]', kind, msg); return; }
  const cls = {success: 'text-bg-success', danger: 'text-bg-danger', warning: 'text-bg-warning',
               info: 'text-bg-primary'}[kind || 'info'] || 'text-bg-secondary';
  const el = document.createElement('div');
  el.className = 'toast align-items-center border-0 ' + cls;
  el.setAttribute('role', 'alert');
  el.innerHTML = '<div class="d-flex"><div class="toast-body">' + escHtml(msg) + '</div>' +
    '<button type="button" class="btn-close btn-close-white me-2 m-auto" ' +
    'data-bs-dismiss="toast" aria-label="Close"></button></div>';
  box.appendChild(el);
  if (window.bootstrap && bootstrap.Toast) {
    const t = new bootstrap.Toast(el, {delay: kind === 'danger' ? 8000 : 4000});
    el.addEventListener('hidden.bs.toast', () => el.remove());
    t.show();
  } else {
    el.classList.add('show');
    setTimeout(() => el.remove(), 5000);
  }
}

// ---- modals ------------------------------------------------------------
// reasonModal(title, callback, opts): ask for an optional free-text reason,
// then call callback(reason).  opts: {body, okLabel, okClass, requireReason,
// placeholder}.  This is the only dialog mechanism used -- never window.prompt.
let _reasonCb = null;

function reasonModal(title, callback, opts) {
  opts = opts || {};
  const modalEl = document.getElementById('reason-modal');
  if (!modalEl) { callback(''); return; }
  modalEl.querySelector('.modal-title').textContent = title;
  const body = modalEl.querySelector('#reason-modal-body');
  body.innerHTML = opts.body || '';
  body.hidden = !opts.body;
  const input = modalEl.querySelector('#reason-input');
  input.value = '';
  input.placeholder = opts.placeholder || 'Reason (optional, recorded in the history)';
  const wrap = modalEl.querySelector('#reason-input-wrap');
  wrap.hidden = opts.hideReason === true;
  const ok = modalEl.querySelector('#reason-ok');
  ok.textContent = opts.okLabel || 'OK';
  ok.className = 'btn ' + (opts.okClass || 'btn-primary');
  _reasonCb = function () {
    const reason = input.value.trim();
    if (opts.requireReason && !reason) { input.classList.add('is-invalid'); return false; }
    input.classList.remove('is-invalid');
    callback(reason);
    return true;
  };
  const modal = bootstrap.Modal.getOrCreateInstance(modalEl);
  modal.show();
  setTimeout(() => { if (!wrap.hidden) input.focus(); }, 200);
}

function confirmModal(title, bodyHtml, callback, opts) {
  opts = Object.assign({hideReason: true, okLabel: 'Confirm', okClass: 'btn-danger', body: bodyHtml}, opts || {});
  reasonModal(title, callback, opts);
}

function _bindReasonModal() {
  const modalEl = document.getElementById('reason-modal');
  if (!modalEl) return;
  const ok = modalEl.querySelector('#reason-ok');
  ok.addEventListener('click', () => {
    if (_reasonCb && _reasonCb()) bootstrap.Modal.getOrCreateInstance(modalEl).hide();
  });
  modalEl.querySelector('#reason-input').addEventListener('keydown', ev => {
    if (ev.key === 'Enter') { ev.preventDefault(); ok.click(); }
  });
}

// A read-only detail modal for JSON / HTML content.
function showDetail(title, html) {
  const modalEl = document.getElementById('detail-modal');
  if (!modalEl) return;
  modalEl.querySelector('.modal-title').textContent = title;
  modalEl.querySelector('.modal-body').innerHTML = html;
  bootstrap.Modal.getOrCreateInstance(modalEl).show();
}

// Key/value table for a flat dict.
function kvTable(obj, keys) {
  const ks = keys || Object.keys(obj || {});
  let html = '<table class="table table-sm kv-table mb-0"><tbody>';
  for (const k of ks) {
    let v = obj[k];
    if (v !== null && typeof v === 'object') v = JSON.stringify(v);
    if (v === null || v === undefined || v === '') v = DASH;
    html += '<tr><th>' + escHtml(k) + '</th><td>' + escHtml(v) + '</td></tr>';
  }
  return html + '</tbody></table>';
}

// ---- sortable tables ---------------------------------------------------
// name -> {accessors: {col: fn}, state: {col, dir}}
const TABLES = {};

function registerTable(name, accessors, defaultCol, defaultDir) {
  TABLES[name] = {accessors: accessors, state: {col: defaultCol, dir: defaultDir || 1}};
}

const SORT_ICON = {
  none: '<i class="bi bi-arrow-down-up text-muted ms-1 small"></i>',
  asc:  '<i class="bi bi-arrow-up ms-1 small"></i>',
  desc: '<i class="bi bi-arrow-down ms-1 small"></i>',
};

function updateSortIndicators(name) {
  const table = TABLES[name];
  if (!table) return;
  for (const col of Object.keys(table.accessors)) {
    const el = document.getElementById('sort-' + name + '-' + col);
    if (el) el.innerHTML = (table.state.col === col)
      ? (table.state.dir > 0 ? SORT_ICON.asc : SORT_ICON.desc)
      : SORT_ICON.none;
  }
}

function sortRows(name, rows) {
  const table = TABLES[name];
  if (!table) return rows;
  const {col, dir} = table.state;
  const accessor = table.accessors[col];
  if (!accessor) return rows;
  const byName = table.accessors.name;
  return [...rows].sort((a, b) => {
    const av = accessor(a), bv = accessor(b);
    if (typeof av === 'string' || typeof bv === 'string') {
      return dir * String(av || '').localeCompare(String(bv || ''));
    }
    // Missing values sort to the end whichever way the column points.
    const sentinel = dir > 0 ? Infinity : -Infinity;
    const an = (av === null || av === undefined) ? sentinel : av;
    const bn = (bv === null || bv === undefined) ? sentinel : bv;
    if (an !== bn) return dir * (an - bn);
    return String(byName ? byName(a) : '').localeCompare(String(byName ? byName(b) : ''));
  });
}

function toggleSort(name, col) {
  const table = TABLES[name];
  if (!table) return;
  table.state.dir = (table.state.col === col) ? -table.state.dir : 1;
  table.state.col = col;
  if (currentData) renderAll(currentData);
}

// ---- cell builders -----------------------------------------------------
function badge(state, title) {
  const key = String(state || 'unknown').toLowerCase();
  return '<span class="badge badge-' + escHtml(key) + '"' +
    (title ? ' title="' + escHtml(title) + '" data-bs-toggle="tooltip"' : '') + '>' +
    escHtml(key) + '</span>';
}

function nameCell(entry, href) {
  const label = entry.label && entry.label !== entry.path
    ? '<strong>' + escHtml(entry.label) + '</strong><br>' : '';
  const path = '<span class="path-cell' + (label ? ' text-muted' : '') + '">' +
    escHtml(entry.path) + '</span>';
  const inner = label + path;
  return '<td>' + (href ? '<a class="area-link" href="' + escHtml(href) + '">' + inner + '</a>' : inner) + '</td>';
}

// A complete <td> holding a progress bar with an optional caption.
function progressBar(pct, cls, title, caption) {
  if (pct === null || pct === undefined || isNaN(pct)) {
    return '<td class="text-muted small">' + DASH + '</td>';
  }
  const width = Math.max(0, Math.min(100, pct)).toFixed(1);
  return '<td style="min-width:170px">' +
    '<div class="progress mb-1" style="height:16px" title="' + escHtml(title || '') + '">' +
    '<div class="progress-bar ' + cls + '" role="progressbar" style="width:' + width + '%" ' +
    'aria-valuenow="' + width + '" aria-valuemin="0" aria-valuemax="100">' +
    Number(pct).toFixed(1) + '%</div></div>' +
    (caption ? '<small class="text-muted">' + caption + '</small>' : '') +
    '</td>';
}

const BAR_CLASS = {
  GOOD: 'bg-success', WARNING: 'bg-warning', CRITICAL: 'bg-danger', FULL: 'bg-full',
  PAUSED: 'bg-secondary', DISABLED: 'bg-secondary', UNKNOWN: 'bg-unknown', MISSING: 'bg-dark',
};

// Tier markers: warning / critical / full with trigger -> stop, active highlighted.
function tiersCell(tiers, selected) {
  const names = ['warning', 'critical', 'full'];
  const present = names.filter(n => tiers && tiers[n]);
  if (!present.length) return '<td class="text-muted small">monitor only</td>';
  let html = '<td class="limit-cell">';
  for (const n of present) {
    const t = tiers[n];
    const cls = 'tier-line' + (t.active ? ' tier-active' : '') + (n === selected ? ' tier-selected' : '');
    const title = n + ': threshold ' + fmtPct(t.threshold) + ', triggers at ' + fmtPct(t.trigger) +
      ', stops at ' + fmtPct(t.stop) + ' (' + (t.policy || '') + ', min age ' + fmtAge(t.min_age) + ')' +
      (t.bytes_to_stop_str ? ', ' + t.bytes_to_stop_str + ' to free' : '');
    html += '<div class="' + cls + '" title="' + escHtml(title) + '">' +
      '<span class="lvl">' + n + '</span> ' + fmtPct(t.threshold, 0) +
      ' <span class="text-muted">(' + fmtPct(t.trigger, 0) + '&rarr;' + fmtPct(t.stop, 0) + ')</span>' +
      (t.active ? ' <i class="bi bi-lightning-charge-fill text-warning" title="active"></i>' : '') +
      '</div>';
  }
  return html + '</td>';
}

// ---- stat cards --------------------------------------------------------
function renderStatCards(prefix, counts) {
  for (const [key, value] of Object.entries(counts || {})) {
    const el = document.getElementById('stat-' + prefix + '-' + key);
    if (el) el.textContent = value;
  }
}

// ---- data fetch + refresh loop -----------------------------------------
let currentData = null;
let renderAll = function () {};
let refreshTimer = null;
let dataUrl = '/api/v1/areas';

const REFRESH_KEY = 'reaper.refreshMs';

function applyRefreshInterval(ms) {
  if (refreshTimer) { clearInterval(refreshTimer); refreshTimer = null; }
  const label = document.getElementById('refresh-label');
  if (ms > 0) {
    refreshTimer = setInterval(refreshData, ms);
    if (label) label.textContent = 'Auto-refreshes every ' +
      (ms >= 60000 ? (ms / 60000) + ' min' : (ms / 1000) + ' s');
  } else if (label) {
    label.textContent = 'Auto-refresh off';
  }
}

function setDataUrl(url) { dataUrl = url; }

function refreshData() {
  return apiFetch(dataUrl)
    .then(data => {
      currentData = data;
      renderAll(data);
      const stamp = document.getElementById('last-update');
      if (stamp) stamp.textContent = '✓ Updated ' + new Date().toLocaleTimeString();
      activateTooltips();
    })
    .catch(err => {
      console.error('Failed to fetch ' + dataUrl + ':', err);
      const stamp = document.getElementById('last-update');
      if (stamp) stamp.textContent = '⚠ Fetch failed: ' + err.message;
    });
}

// Fill a <tbody>, or show a placeholder row when there is nothing to show.
function fillTable(id, rowsHtml, colspan, emptyMessage) {
  const tbody = document.getElementById(id);
  if (!tbody) return;
  tbody.innerHTML = rowsHtml ||
    '<tr><td colspan="' + colspan + '" class="text-muted p-3">' +
    escHtml(emptyMessage) + '</td></tr>';
}

function activateTooltips() {
  if (!window.bootstrap || !bootstrap.Tooltip) return;
  document.querySelectorAll('[data-bs-toggle="tooltip"]:not([data-tt])').forEach(el => {
    el.setAttribute('data-tt', '1');
    try { new bootstrap.Tooltip(el); } catch (e) { /* ignore */ }
  });
}

function startDashboard(url, render, defaultMs) {
  dataUrl = url;
  renderAll = render;

  const select = document.getElementById('refresh-interval');
  let stored = NaN;
  try { stored = parseInt(window.localStorage.getItem(REFRESH_KEY), 10); } catch (e) { /* private mode */ }
  const initial = Number.isFinite(stored) ? stored : (defaultMs || 5000);
  if (select) {
    select.value = String(initial);
    select.addEventListener('change', function () {
      const ms = parseInt(this.value, 10);
      try { window.localStorage.setItem(REFRESH_KEY, String(ms)); } catch (e) { /* ignore */ }
      applyRefreshInterval(ms);
    });
  }
  const button = document.getElementById('refresh-now');
  if (button) button.addEventListener('click', refreshData);

  Object.keys(TABLES).forEach(updateSortIndicators);
  refreshData();
  applyRefreshInterval(initial);
}

// ---- area actions shared by the Areas and area-detail pages ------------
const AREA_VERB_LABEL = {
  pause: 'Pause', resume: 'Resume', disable: 'Disable', enable: 'Enable', rescan: 'Rescan',
};

function areaAction(name, verb) {
  const label = AREA_VERB_LABEL[verb] || verb;
  const needsReason = verb === 'pause' || verb === 'disable';
  const go = function (reason) {
    postJSON('/api/v1/areas/' + encodeURIComponent(name) + '/' + verb, reason ? {reason: reason} : {})
      .then(() => { showToast(label + ' ' + name + ': ok', 'success'); refreshData(); })
      .catch(err => showToast(label + ' ' + name + ' failed: ' + err.message, 'danger'));
  };
  if (needsReason || verb === 'resume' || verb === 'enable') {
    reasonModal(label + ' ' + name, go, {
      okLabel: label,
      okClass: verb === 'disable' ? 'btn-danger' : 'btn-primary',
      body: verb === 'disable'
        ? '<p class="mb-2 small">Disabling stops scans <em>and</em> actions for this area until it is enabled again.</p>'
        : (verb === 'pause' ? '<p class="mb-2 small">Pausing keeps scanning but performs no compress/delete actions.</p>' : ''),
    });
  } else {
    go('');
  }
}

function dryRunPlan(name) {
  showToast('Running dry-run scan of ' + name + '…', 'info');
  postJSON('/api/v1/dry-run/' + encodeURIComponent(name), {})
    .then(res => {
      const rep = res.report || {};
      const st = res.state || {};
      let html = '<div class="row g-3 mb-3">';
      const items = [['Outcome', rep.outcome], ['Reason', rep.reason], ['Passes', rep.passes],
                     ['Would act on', rep.acted], ['Would free', fmtBytes(rep.bytes_freed)],
                     ['Duration', (rep.duration_s !== undefined ? rep.duration_s + ' s' : DASH)]];
      for (const [k, v] of items) {
        html += '<div class="col-6 col-md-4"><div class="text-muted small">' + escHtml(k) + '</div>' +
          '<div class="fw-bold">' + escHtml(v === null || v === undefined ? DASH : v) + '</div></div>';
      }
      html += '</div>';
      for (const kind of ['compress', 'delete']) {
        const q = (st.queues || {})[kind] || {total: 0, entries: []};
        html += '<h6 class="mt-3">' + kind + ' queue <small class="text-muted">(' + q.total +
          ' total, showing ' + (q.entries || []).length + ')</small></h6>';
        if (!(q.entries || []).length) { html += '<p class="text-muted small">empty</p>'; continue; }
        html += '<div class="table-responsive" style="max-height:260px;overflow:auto">' +
          '<table class="table table-sm table-striped mb-0"><thead><tr><th>#</th><th>Path</th>' +
          '<th>Size</th><th>Key age</th><th>Tier</th><th>Policy</th></tr></thead><tbody>';
        q.entries.forEach((e, i) => {
          html += '<tr><td>' + (i + 1) + '</td><td class="path-cell">' + escHtml(e.path) + '</td>' +
            '<td class="text-nowrap">' + escHtml(e.size_str) + '</td>' +
            '<td class="text-nowrap">' + fmtAge((Date.now() / 1000) - e.key_ts) + '</td>' +
            '<td>' + escHtml(e.tier) + '</td><td>' + escHtml(e.policy) + '</td></tr>';
        });
        html += '</tbody></table></div>';
      }
      showDetail('Dry-run plan: ' + name, html);
      refreshData();
    })
    .catch(err => showToast('Dry run failed: ' + err.message, 'danger'));
}

function areaActionButtons(a) {
  if (!IS_ADMIN) return '';
  const n = JSON.stringify(a.name);
  let html = '<div class="btn-group btn-group-sm" role="group">';
  if (a.paused) {
    html += '<button class="btn btn-outline-success" title="Resume actions" onclick=\'areaAction(' + escHtml(n) + ',"resume")\'>' +
      '<i class="bi bi-play-fill"></i></button>';
  } else {
    html += '<button class="btn btn-outline-warning" title="Pause actions" onclick=\'areaAction(' + escHtml(n) + ',"pause")\'' +
      (a.disabled ? ' disabled' : '') + '><i class="bi bi-pause-fill"></i></button>';
  }
  if (a.disabled) {
    html += '<button class="btn btn-outline-success" title="Enable area" onclick=\'areaAction(' + escHtml(n) + ',"enable")\'>' +
      '<i class="bi bi-power"></i></button>';
  } else {
    html += '<button class="btn btn-outline-danger" title="Disable area" onclick=\'areaAction(' + escHtml(n) + ',"disable")\'>' +
      '<i class="bi bi-x-octagon"></i></button>';
  }
  html += '<button class="btn btn-outline-secondary" title="Rescan now" onclick=\'areaAction(' + escHtml(n) + ',"rescan")\'' +
    (a.disabled ? ' disabled' : '') + '><i class="bi bi-arrow-repeat"></i></button>';
  html += '<button class="btn btn-outline-primary" title="Dry-run plan" onclick=\'dryRunPlan(' + escHtml(n) + ')\'' +
    (a.disabled ? ' disabled' : '') + '><i class="bi bi-eyeglasses"></i></button>';
  return html + '</div>';
}

// Exclude one path (queue rows on several pages).
function excludePath(path, area) {
  reasonModal('Exclude ' + path, function (reason) {
    postJSON('/api/v1/exclusions', {pattern: path, area: area || null, reason: reason || null})
      .then(() => { showToast('Excluded ' + path, 'success'); refreshData(); })
      .catch(err => showToast('Exclude failed: ' + err.message, 'danger'));
  }, {okLabel: 'Exclude', body: '<p class="small mb-2">The file is protected from compression and deletion' +
      (area ? ' in area <code>' + escHtml(area) + '</code>' : ' in every area') +
      ' until the rule is removed on the Exclusions page.</p>'});
}

document.addEventListener('DOMContentLoaded', function () {
  _bindReasonModal();
  activateTooltips();
});
