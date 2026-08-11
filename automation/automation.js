/* ══════════════════════════════════════════════════════════
   Star — Automation control plane, shared core

   Loaded by all six automation pages. It owns the transport, the
   vocabulary the API speaks, and the page registry; it renders no
   page-specific markup of its own.

   Three rules shape every function below:

     1. The page renders only what the API actually returned. There is no
        optimistic state: a provider is "ready" because /api/providers said
        so, a job succeeded because /api/jobs/{id} said so. A platform that
        the backend reports as manual is never drawn as published.

     2. Credentials are write-only. Configure forms are never prefilled,
        values are dropped from the DOM the moment they are sent, and no
        response is trusted to contain a secret to display.

     3. Exactly one page module runs per document. A module is registered
        against the value of <body data-ac-page="…"> and is never invoked on
        another page, so no module can query an element that is not there.

   Shared behaviour (sidebar drawer, section nav, toasts) comes from
   /workspace.js; this file assumes that has loaded but degrades to inline
   status messages if it has not.
   ══════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  var $  = function (sel, root) { return (root || document).querySelector(sel); };
  var $$ = function (sel, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(sel));
  };

  /* The CSRF intent header the backend requires on every state-changing
     automation route. A cross-origin form cannot set it. */
  var INTENT_HEADER = 'X-Star-Intent';
  var INTENT_VALUE  = 'automation-control';

  var API_TIMEOUT   = 15000;
  var POLL_ACTIVE   = 1500;   /* while a job is running */
  var POLL_SUMMARY  = 4000;   /* dashboard summary, one job at a glance */
  var MAX_LOG_LINES = 400;

  var DAYS = [
    { key: 'sun', label: 'อาทิตย์' }, { key: 'mon', label: 'จันทร์' },
    { key: 'tue', label: 'อังคาร'  }, { key: 'wed', label: 'พุธ'    },
    { key: 'thu', label: 'พฤหัสบดี' }, { key: 'fri', label: 'ศุกร์'  },
    { key: 'sat', label: 'เสาร์'   }
  ];

  /* Mirrors star_jobs.STAGES, in pipeline order. */
  var STAGES = [
    { key: 'astro',  label: 'ดาราศาสตร์', icon: 'travel_explore' },
    { key: 'script', label: 'เขียนบท',    icon: 'edit_note' },
    { key: 'audio',  label: 'เสียงพากย์', icon: 'graphic_eq' },
    { key: 'video',  label: 'เรนเดอร์วิดีโอ', icon: 'movie' },
    { key: 'publish', label: 'เผยแพร่',   icon: 'send' }
  ];

  var STATUS_TH = {
    ready: 'พร้อมใช้งาน',
    configured: 'ตั้งค่าแล้ว',
    not_configured: 'ยังไม่ได้ตั้งค่า',
    error: 'มีปัญหา',
    manual: 'ต้องทำเอง'
  };

  var AUTOMATION_TH = {
    full_auto: 'อัตโนมัติเต็มรูปแบบ',
    semi_auto: 'กึ่งอัตโนมัติ',
    manual: 'ทำเอง'
  };

  var JOB_STATUS_TH = {
    queued: 'เข้าคิว',
    running: 'กำลังทำงาน',
    succeeded: 'สำเร็จ',
    failed: 'ล้มเหลว',
    cancelled: 'ยกเลิกแล้ว',
    blocked: 'ติดเงื่อนไข'
  };

  var JOB_STATUS_ICON = {
    queued: 'schedule',
    running: 'progress_activity',
    succeeded: 'check_circle',
    failed: 'error',
    cancelled: 'cancel',
    blocked: 'block'
  };

  var STAGE_LABEL = {};
  STAGES.forEach(function (s) { STAGE_LABEL[s.key] = s.label; });

  /* Same-origin paths, written once so no page can invent a base URL. */
  var PATHS = {
    dashboard: '/automation/',
    run:       '/automation/run/',
    jobs:      '/automation/jobs/',
    schedule:  '/automation/schedule/',
    providers: '/automation/providers/',
    guide:     '/automation/guide/'
  };

  /* ── tiny DOM helpers ──────────────────────────────────── */

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  function icon(name, extraClass) {
    var node = el('span', extraClass ? 'ms ' + extraClass : 'ms', name);
    node.setAttribute('aria-hidden', 'true');
    return node;
  }

  function clear(node) {
    while (node && node.firstChild) node.removeChild(node.firstChild);
    return node;
  }

  function toast(message, variant) {
    var region = $('#toast-region');
    if (!region) return;
    $$('.toast', region).forEach(function (t) { t.remove(); });
    var icons = { success: 'check_circle', error: 'error', info: 'info', warn: 'warning' };
    var kind = icons[variant] ? variant : 'info';
    var node = el('div', 'toast toast-' + kind);
    node.appendChild(icon(icons[kind]));
    node.appendChild(el('span', null, message));
    region.appendChild(node);
    setTimeout(function () { node.remove(); }, 4000);
  }

  function setStatus(node, message, kind) {
    if (!node) return;
    clear(node);
    node.className = 'status-msg' + (kind ? ' ' + kind : '');
    if (!message) return;
    var glyph = { success: 'check_circle', error: 'error', info: 'info' }[kind];
    if (glyph) node.appendChild(icon(glyph));
    node.appendChild(el('span', null, message));
  }

  /* An inline link appended to a status line, used to hand a job over to the
     jobs page without losing the message that explains why. */
  function statusLink(node, href, label) {
    if (!node) return null;
    var link = el('a', 'ac-inline-link', label);
    link.href = href;
    node.appendChild(link);
    return link;
  }

  function fmtTime(iso) {
    if (!iso) return '—';
    var d = new Date(iso);
    if (isNaN(d.getTime())) return '—';
    return d.toLocaleString('th-TH', {
      day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit'
    });
  }

  function fmtClock(iso) {
    if (!iso) return '';
    var d = new Date(iso);
    if (isNaN(d.getTime())) return '';
    return d.toLocaleTimeString('th-TH', { hour12: false });
  }

  function todayISO() {
    var now = new Date();
    return [
      now.getFullYear(),
      String(now.getMonth() + 1).padStart(2, '0'),
      String(now.getDate()).padStart(2, '0')
    ].join('-');
  }

  /* ── transport ─────────────────────────────────────────── */

  function ApiError(message, status, field) {
    this.message = message;
    this.status = status || 0;
    this.field = field || null;
  }
  ApiError.prototype = Object.create(Error.prototype);

  /* Same-origin only: paths are always relative to this document's origin,
     never a configurable base URL. */
  function api(method, path, body) {
    var controller = new AbortController();
    var timer = setTimeout(function () { controller.abort(); }, API_TIMEOUT);
    var headers = { Accept: 'application/json' };
    var mutating = method !== 'GET';
    if (mutating) headers[INTENT_HEADER] = INTENT_VALUE;
    if (body !== undefined) headers['Content-Type'] = 'application/json';

    return fetch(path, {
      method: method,
      headers: headers,
      cache: 'no-store',
      credentials: 'same-origin',
      signal: controller.signal,
      body: body === undefined ? undefined : JSON.stringify(body)
    }).catch(function (err) {
      throw new ApiError(
        err && err.name === 'AbortError'
          ? 'หมดเวลารอเซิร์ฟเวอร์ (' + (API_TIMEOUT / 1000) + ' วินาที)'
          : 'เชื่อมต่อเซิร์ฟเวอร์ไม่ได้', 0);
    }).then(function (res) {
      var type = res.headers.get('content-type') || '';
      var asJson = type.indexOf('json') !== -1
        ? res.json().catch(function () { return null; })
        : Promise.resolve(null);
      return asJson.then(function (payload) {
        if (!res.ok) {
          var message = (payload && payload.error) ||
            ('เซิร์ฟเวอร์ตอบกลับ HTTP ' + res.status);
          var error = new ApiError(message, res.status,
                                   payload && payload.field);
          error.payload = payload;
          throw error;
        }
        return payload;
      });
    }).finally(function () { clearTimeout(timer); });
  }

  /* ── shared page state ─────────────────────────────────── */

  var state = {
    providers: [],
    overview: null,
    schedule: null,
    job: null,        /* the job currently being followed */
    lastEventId: 0,
    pollTimer: null,
    limits: { max_range_days: 31 }
  };

  /* Fetchers store what the API returned and hand it back; every page renders
     that payload itself, because no two pages show the same slice of it. */
  function fetchOverview() {
    return api('GET', '/api/automation/overview').then(function (data) {
      state.overview = data;
      if (data && data.limits) state.limits = data.limits;
      return data;
    });
  }

  function fetchProviders() {
    return api('GET', '/api/providers').then(function (data) {
      state.providers = (data && data.providers) || [];
      return state.providers;
    });
  }

  function providerLabel(key) {
    var found = (state.providers || []).filter(function (p) {
      return p.provider === key;
    })[0];
    return (found && found.label) || key;
  }

  /* ── shared renderers ──────────────────────────────────── */

  function callout(glyph, title, body, variant) {
    var node = el('div', 'callout ' + (variant || ''));
    node.appendChild(icon(glyph));
    var wrap = el('div', 'callout-body');
    wrap.appendChild(el('div', 'callout-title', title));
    wrap.appendChild(el('p', null, body));
    node.appendChild(wrap);
    return node;
  }

  function chip(glyph, text) {
    var node = el('span', 'chip');
    node.appendChild(icon(glyph));
    node.appendChild(el('span', null, String(text)));
    return node;
  }

  function statusChip(status) {
    var node = el('span', 'chip ac-chip-' + status);
    node.appendChild(icon(JOB_STATUS_ICON[status] || 'help'));
    node.appendChild(el('span', null, JOB_STATUS_TH[status] || status));
    return node;
  }

  function tile(label, glyph, value, note, valueClass) {
    var node = el('div', 'ac-tile');
    var head = el('div', 'ac-tile-label');
    head.appendChild(icon(glyph));
    head.appendChild(el('span', null, label));
    node.appendChild(head);
    node.appendChild(el('div', 'ac-tile-value' + (valueClass ? ' ' + valueClass : ''),
                        value));
    if (note) node.appendChild(el('div', 'ac-tile-note', note));
    return node;
  }

  /* Placeholder rows shown while a request is in flight. Marked aria-busy and
     aria-hidden so a screen reader is told "loading" once, not "blank row"
     five times. */
  function skeleton(host, rows, className) {
    if (!host) return host;
    clear(host);
    host.setAttribute('aria-busy', 'true');
    for (var i = 0; i < rows; i++) {
      var row = el('div', 'sk ' + (className || 'ac-sk-row'));
      row.setAttribute('aria-hidden', 'true');
      host.appendChild(row);
    }
    return host;
  }

  function settled(host) {
    if (host) host.removeAttribute('aria-busy');
    return host;
  }

  /* A description of what went wrong plus a way to try again — never a dead
     empty box. */
  function errorState(host, title, message, onRetry) {
    if (!host) return;
    settled(host);
    clear(host);
    host.hidden = false;
    host.appendChild(icon('cloud_off', 'state-icon'));
    host.appendChild(el('p', 'state-title', title));
    host.appendChild(el('p', 'state-desc', message));
    if (onRetry) {
      var retry = el('button', 'btn btn-sm');
      retry.type = 'button';
      retry.appendChild(icon('refresh'));
      retry.appendChild(el('span', null, 'ลองใหม่'));
      retry.addEventListener('click', onRetry);
      host.appendChild(retry);
    }
  }

  function jobScopeText(job) {
    var input = (job && job.input) || {};
    return (input.from_date || '?') +
      (input.to_date && input.to_date !== input.from_date ? ' – ' + input.to_date : '') +
      ' · ' + ((input.days || []).length) + ' วันเกิด' +
      ' · ' + (input.dry_run ? 'โหมดซ้อม' : 'โหมดจริง');
  }

  /* Deep link to one job on the jobs page: the run page hands work over
     without duplicating the progress surface. */
  function jobHref(id) {
    return PATHS.jobs + '?job=' + encodeURIComponent(id);
  }

  function queryParam(name) {
    try {
      return new URLSearchParams(window.location.search).get(name) || '';
    } catch (e) {
      return '';
    }
  }

  /* ── checkbox groups ───────────────────────────────────── */

  function selectedValues(name) {
    return $$('input[name="' + name + '"]:checked').map(function (b) { return b.value; });
  }

  function applySelection(name, values) {
    $$('input[name="' + name + '"]').forEach(function (box) {
      box.checked = values.indexOf(box.value) !== -1;
    });
  }

  function buildPills(host, items, name, glyphKey, ordered, onChange) {
    if (!host) return;
    clear(host);
    items.forEach(function (item, index) {
      var id = 'ac-' + name + '-' + item.key;
      var wrap = el('label', 'ac-pill');
      wrap.htmlFor = id;
      var box = document.createElement('input');
      box.type = 'checkbox';
      box.id = id;
      box.name = name;
      box.value = item.key;
      if (onChange) box.addEventListener('change', onChange);
      var face = el('span');
      if (ordered) face.appendChild(el('span', 'ac-pill-order', String(index + 1)));
      if (item[glyphKey]) face.appendChild(icon(item[glyphKey]));
      face.appendChild(el('span', null, item.label));
      wrap.appendChild(box);
      wrap.appendChild(face);
      host.appendChild(wrap);
    });
  }

  /* ── page registry ─────────────────────────────────────── */

  var pages = {};
  var refreshHandlers = [];
  var started = false;

  /* Register a page module. `name` must match <body data-ac-page="…">.

     Registration is also a boot trigger. A cached copy of this file runs long
     before the page module has been fetched, so the boot attempt made on
     arrival can find an empty registry; the module that lands late starts
     itself instead of waiting for an event that has already fired. boot() is
     idempotent, so registering twice — or registering a page this document
     does not own — still starts exactly one module, exactly once. */
  function page(name, init) {
    pages[name] = init;
    if (document.readyState !== 'loading') boot();
  }

  function currentPage() {
    return (document.body && document.body.dataset &&
            document.body.dataset.acPage) || '';
  }

  /* The refresh button lives in the shared topbar; each page decides what
     reloading means for it. */
  function onRefresh(handler) { refreshHandlers.push(handler); }

  function wireShared() {
    var refresh = $('#ac-refresh');
    if (refresh) {
      refresh.addEventListener('click', function () {
        if (!refreshHandlers.length) return;
        toast('กำลังโหลดข้อมูลใหม่', 'info');
        refreshHandlers.forEach(function (fn) { fn(); });
      });
    }
    window.addEventListener('beforeunload', function () {
      if (state.pollTimer) clearTimeout(state.pollTimer);
    });
  }

  /* Page-aware boot: only the module registered for this document's
     data-ac-page runs, so nothing ever reaches for markup that another page
     owns. An unknown or missing value simply starts nothing.

     Called from every trigger — DOMContentLoaded, script arrival, module
     registration — and safe from all of them: `started` flips only when a
     module was actually found and invoked, so an attempt that came too early
     leaves the door open, and every attempt after the first one returns. */
  function boot() {
    if (started) return;
    var name = currentPage();
    var init = pages[name];
    if (typeof init !== 'function') return;
    started = true;
    wireShared();
    init();
  }

  window.StarAC = {
    /* constants */
    DAYS: DAYS, STAGES: STAGES, STAGE_LABEL: STAGE_LABEL,
    STATUS_TH: STATUS_TH, AUTOMATION_TH: AUTOMATION_TH,
    JOB_STATUS_TH: JOB_STATUS_TH, JOB_STATUS_ICON: JOB_STATUS_ICON,
    PATHS: PATHS, POLL_ACTIVE: POLL_ACTIVE, POLL_SUMMARY: POLL_SUMMARY,
    MAX_LOG_LINES: MAX_LOG_LINES, INTENT_HEADER: INTENT_HEADER,
    /* dom */
    $: $, $$: $$, el: el, icon: icon, clear: clear,
    toast: toast, setStatus: setStatus, statusLink: statusLink,
    callout: callout, chip: chip, statusChip: statusChip, tile: tile,
    skeleton: skeleton, settled: settled, errorState: errorState,
    /* format */
    fmtTime: fmtTime, fmtClock: fmtClock, todayISO: todayISO,
    jobScopeText: jobScopeText, jobHref: jobHref, queryParam: queryParam,
    /* transport */
    api: api, ApiError: ApiError, state: state,
    fetchOverview: fetchOverview, fetchProviders: fetchProviders,
    providerLabel: providerLabel,
    /* controls */
    selectedValues: selectedValues, applySelection: applySelection,
    buildPills: buildPills,
    /* lifecycle */
    page: page, currentPage: currentPage, onRefresh: onRefresh
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    /* The document is already parsed, so a module registered by now can start
       at once. One that has not arrived yet — this file is cached, its page
       module may still be in flight — boots from its own page() call. No
       timer: nothing here depends on a module winning a race against a delay. */
    boot();
  }
})();
