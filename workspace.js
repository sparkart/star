/* ══════════════════════════════════════════════════════════
   Star — shared behaviour for the secondary workspace pages
   (/content/ and /automation/).

   Modules, each a no-op when its markup is absent:
     · mobile nav drawer      · in-page section nav + scrollspy
     · debounced table filter · copy-to-clipboard + toasts
     · live API panel (/content/ only)

   Everything honours prefers-reduced-motion.
   ══════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  const $  = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.prototype.slice.call((root || document).querySelectorAll(sel));

  const motionQuery = window.matchMedia('(prefers-reduced-motion: reduce)');
  const reducedMotion = () => motionQuery.matches;

  function debounce(fn, wait) {
    let t;
    return function () {
      const args = arguments;
      clearTimeout(t);
      t = setTimeout(() => fn.apply(null, args), wait);
    };
  }

  /* A decorative Material Symbols glyph: the text content is the ligature
     name, which the icon font renders as the icon itself. */
  function msIcon(name, extraClass) {
    const el = document.createElement('span');
    el.className = extraClass ? 'ms ' + extraClass : 'ms';
    el.setAttribute('aria-hidden', 'true');
    el.textContent = name;
    return el;
  }

  function topbarOffset() {
    const bar = $('.topbar');
    return (bar ? bar.getBoundingClientRect().height : 56) + 24;
  }

  // ══ Toasts ═════════════════════════════════════════════
  const TOAST_ICONS = {
    success: 'check_circle',
    error:   'error',
    info:    'info',
    warn:    'warning'
  };

  function showToast(msg, variant) {
    const kind = TOAST_ICONS[variant] ? variant : 'info';
    const region = $('#toast-region') || document.body;
    $$('.toast', region).forEach(t => t.remove());

    const toast = document.createElement('div');
    toast.className = 'toast toast-' + kind;
    toast.appendChild(msIcon(TOAST_ICONS[kind]));
    const text = document.createElement('span');
    text.textContent = msg;
    toast.appendChild(text);
    region.appendChild(toast);

    setTimeout(() => {
      if (reducedMotion()) { toast.remove(); return; }
      toast.classList.add('is-out');
      setTimeout(() => toast.remove(), 240);
    }, 3200);
  }

  // ══ Mobile nav drawer ══════════════════════════════════
  function setupSidebar() {
    const hamburger = $('#hamburger');
    const sidebar = $('#sidebar');
    if (!hamburger || !sidebar) return;
    const scrim = $('#nav-scrim');

    function setOpen(open) {
      sidebar.classList.toggle('sidebar-open', open);
      hamburger.setAttribute('aria-expanded', String(open));
      if (scrim) {
        scrim.hidden = !open;
        // Let the element paint before the opacity transition starts.
        if (open) requestAnimationFrame(() => scrim.classList.add('is-open'));
        else scrim.classList.remove('is-open');
      }
      if (open) {
        const first = $('.nav-links a', sidebar);
        if (first) first.focus();
      }
    }

    hamburger.addEventListener('click', () => {
      setOpen(!sidebar.classList.contains('sidebar-open'));
    });
    if (scrim) scrim.addEventListener('click', () => { setOpen(false); hamburger.focus(); });

    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && sidebar.classList.contains('sidebar-open')) {
        setOpen(false);
        hamburger.focus();
      }
    });

    // Following an in-page link should reveal the target, not the drawer.
    sidebar.addEventListener('click', (e) => {
      const link = e.target.closest ? e.target.closest('a') : null;
      if (link && window.matchMedia('(max-width: 768px)').matches) setOpen(false);
    });

    // Widening past the breakpoint leaves the drawer state stale otherwise.
    window.addEventListener('resize', debounce(() => {
      if (!window.matchMedia('(max-width: 768px)').matches) setOpen(false);
    }, 150));
  }

  // ══ In-page section nav ════════════════════════════════
  function setupSectionNav() {
    const main = $('.main');
    if (!main) return;
    const headings = $$('h2[id], h3[id]', main);
    if (!headings.length) return;

    const subNav = $('#subNav');
    const links = {};

    if (subNav) {
      const label = document.createElement('span');
      label.className = 'nav-group-label';
      label.textContent = 'ในหน้านี้';
      subNav.appendChild(label);

      headings.forEach((h) => {
        const a = document.createElement('a');
        a.href = '#' + h.id;
        // Section headings carry an index span, so prefer the explicit label.
        a.textContent = h.dataset.navLabel || h.textContent.trim();
        a.className = h.tagName === 'H2' ? 'sub-main' : 'sub-child';
        subNav.appendChild(a);
        links[h.id] = a;
      });
    }

    // Permalink affordance, added after the rail so it never leaks into labels.
    headings.forEach((h) => {
      const a = document.createElement('a');
      a.className = 'head-anchor';
      a.href = '#' + h.id;
      a.setAttribute('aria-label', 'ลิงก์ไปยังหัวข้อ ' + (h.dataset.navLabel || h.textContent.trim()));
      a.appendChild(msIcon('link'));
      h.appendChild(a);
    });

    if (!subNav) return;

    let activeId = null;
    function setActive(id) {
      if (id === activeId) return;
      if (activeId && links[activeId]) links[activeId].classList.remove('active');
      activeId = id;
      if (links[id]) {
        links[id].classList.add('active');
        links[id].setAttribute('aria-current', 'true');
      }
      Object.keys(links).forEach((k) => {
        if (k !== id) links[k].removeAttribute('aria-current');
      });
    }

    function pickActive() {
      const offset = topbarOffset();
      let current = headings[0];
      for (let i = 0; i < headings.length; i++) {
        if (headings[i].getBoundingClientRect().top - offset <= 0) current = headings[i];
        else break;
      }
      // At the very bottom the last section can never reach the offset line.
      const atBottom = window.innerHeight + window.scrollY >=
        document.documentElement.scrollHeight - 4;
      if (atBottom) current = headings[headings.length - 1];
      setActive(current.id);
    }

    let ticking = false;
    function onScroll() {
      if (ticking) return;
      ticking = true;
      requestAnimationFrame(() => { pickActive(); ticking = false; });
    }

    window.addEventListener('scroll', onScroll, { passive: true });
    window.addEventListener('resize', debounce(pickActive, 150));
    window.addEventListener('hashchange', () => setTimeout(pickActive, 60));
    pickActive();
  }

  // ══ Debounced table filter ═════════════════════════════
  const rowText = new WeakMap();

  function textOf(row) {
    let cached = rowText.get(row);
    if (cached === undefined) {
      cached = (row.textContent || '').replace(/\s+/g, ' ').trim().toLowerCase();
      rowText.set(row, cached);
    }
    return cached;
  }

  function setupFilters() {
    $$('[data-filter-input]').forEach((input) => {
      const tables = $$(input.getAttribute('data-filter-target') || '');
      if (!tables.length) return;

      const field = input.closest('.search-field');
      const clearBtn = field ? $('.search-clear', field) : null;
      const countEl = input.getAttribute('data-filter-count')
        ? $(input.getAttribute('data-filter-count')) : null;
      const emptyEl = input.getAttribute('data-filter-empty')
        ? $(input.getAttribute('data-filter-empty')) : null;

      const rows = [];
      tables.forEach((t) => { $$('tbody tr', t).forEach((r) => rows.push(r)); });
      const total = rows.length;

      function apply() {
        const q = input.value.trim().toLowerCase();
        let shown = 0;
        rows.forEach((row) => {
          const hit = !q || textOf(row).indexOf(q) !== -1;
          row.hidden = !hit;
          if (hit) shown++;
        });
        if (field) field.classList.toggle('has-value', input.value.length > 0);
        if (countEl) {
          countEl.textContent = q
            ? 'แสดง ' + shown + ' จาก ' + total + ' รายการ'
            : total + ' รายการ';
        }
        if (emptyEl) emptyEl.hidden = shown !== 0;
        tables.forEach((t) => {
          const visible = $$('tbody tr', t).some((r) => !r.hidden);
          const wrap = t.closest('.table-wrap');
          if (wrap) wrap.hidden = !visible;
        });
      }

      input.addEventListener('input', debounce(apply, 160));
      input.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && input.value) {
          e.stopPropagation();      // keep Escape from also closing the drawer
          input.value = '';
          apply();
        }
      });
      if (clearBtn) {
        clearBtn.addEventListener('click', () => {
          input.value = '';
          apply();
          input.focus();
        });
      }
      apply();
    });
  }

  // ══ Copy to clipboard ══════════════════════════════════
  async function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      try {
        await navigator.clipboard.writeText(text);
        return true;
      } catch (e) { /* fall through to the legacy path */ }
    }
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.cssText = 'position:fixed;top:-1000px;left:0;opacity:0';
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
    ta.remove();
    return ok;
  }

  function setupCopy() {
    document.addEventListener('click', async (e) => {
      const btn = e.target.closest ? e.target.closest('[data-copy]') : null;
      if (!btn) return;
      const value = btn.getAttribute('data-copy');
      if (!value) return;
      const label = btn.getAttribute('data-copy-label') || value;
      const ok = await copyText(value);
      showToast(ok ? 'คัดลอก ' + label + ' แล้ว' : 'คัดลอกไม่สำเร็จ — กดค้างเพื่อคัดลอกเอง',
                ok ? 'success' : 'error');
    });
  }

  // ══ Live API panel (/content/) ═════════════════════════
  // Reads GET /api/stats and GET /api/health. Neither endpoint is proxied
  // yet, so the failure path is the one most users will see: it must say
  // exactly what happened rather than quietly showing zeros.

  const API_TIMEOUT = 8000;

  function ApiError(message, kind, status) {
    this.message = message;
    this.kind = kind;
    this.status = status || 0;
  }
  ApiError.prototype = Object.create(Error.prototype);

  async function fetchJSON(url) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), API_TIMEOUT);
    let res;
    try {
      res = await fetch(url, {
        headers: { Accept: 'application/json' },
        cache: 'no-store',
        signal: controller.signal
      });
    } catch (e) {
      throw new ApiError(
        e && e.name === 'AbortError'
          ? 'หมดเวลารอ (' + (API_TIMEOUT / 1000) + ' วินาที)'
          : 'เชื่อมต่อเซิร์ฟเวอร์ไม่ได้',
        e && e.name === 'AbortError' ? 'timeout' : 'network'
      );
    } finally {
      clearTimeout(timer);
    }

    if (!res.ok) {
      throw new ApiError('เซิร์ฟเวอร์ตอบกลับ HTTP ' + res.status +
        (res.status === 404 ? ' — ยังไม่มี backend รองรับ endpoint นี้' : ''),
        'http', res.status);
    }

    const type = res.headers.get('content-type') || '';
    if (type.indexOf('json') === -1) {
      throw new ApiError('เซิร์ฟเวอร์ตอบกลับเป็น ' + (type.split(';')[0] || 'ชนิดที่ไม่รู้จัก') +
        ' ไม่ใช่ JSON', 'shape', res.status);
    }
    try {
      return await res.json();
    } catch (e) {
      throw new ApiError('อ่าน JSON จาก response ไม่ได้', 'shape', res.status);
    }
  }

  const NUM_FMT = new Intl.NumberFormat('th-TH');

  function fmtInt(value) {
    return (typeof value === 'number' && isFinite(value)) ? NUM_FMT.format(value) : null;
  }

  function fmtTime(iso) {
    if (!iso) return null;
    const d = new Date(iso);
    if (isNaN(d.getTime())) return null;
    return d.toLocaleString('th-TH', {
      day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit'
    });
  }

  const CHECK_STATES = {
    ok:       { chip: 'chip-ok',     icon: 'check_circle', label: 'ปกติ' },
    degraded: { chip: 'chip-warn',   icon: 'warning',      label: 'ทำงานได้บางส่วน' },
    down:     { chip: 'chip-danger', icon: 'cancel',       label: 'ใช้งานไม่ได้' },
    unknown:  { chip: '',            icon: 'help',         label: 'ไม่ทราบสถานะ' }
  };

  // The health payload may give a check as a bare string or as
  // { status, detail } — normalise both into one shape.
  const CHECK_STATUS_ALIASES = { warn: 'degraded', fail: 'down' };

  function normalizeStatus(value) {
    const status = String(value == null || value === '' ? 'unknown' : value).toLowerCase();
    return CHECK_STATUS_ALIASES[status] || status;
  }

  function normalizeCheck(value) {
    if (value && typeof value === 'object') {
      return {
        status: normalizeStatus(value.status),
        detail: value.detail != null ? String(value.detail) : ''
      };
    }
    return { status: normalizeStatus(value), detail: '' };
  }

  function setupApiPanel() {
    const panel = $('#api-live');
    if (!panel) return;

    const chip = $('#api-chip');
    const chipText = $('#api-chip-text');
    const refreshBtn = $('#api-refresh');
    const statsSkeleton = $('#stats-skeleton');
    const statsRow = $('#stats-row');
    const statsError = $('#stats-error');
    const statsErrorDesc = $('#stats-error-desc');
    const statsStamp = $('#stats-stamp');
    const healthSkeleton = $('#health-skeleton');
    const healthBody = $('#health-body');
    const healthError = $('#health-error');
    const healthErrorDesc = $('#health-error-desc');
    const healthSummary = $('#health-summary');
    const healthChecks = $('#health-checks');

    const statFields = $$('[data-stat]', panel);

    function setChip(state, text) {
      if (!chip || !chipText) return;
      chip.classList.remove('is-live', 'is-error', 'is-warn', 'is-checking');
      if (state) chip.classList.add(state);
      chipText.textContent = text;
      chip.title = text;
    }

    function showStatsLoading() {
      if (statsSkeleton) statsSkeleton.hidden = false;
      if (statsRow) statsRow.hidden = true;
      if (statsError) statsError.hidden = true;
    }

    function showHealthLoading() {
      if (healthSkeleton) healthSkeleton.hidden = false;
      if (healthBody) healthBody.hidden = true;
      if (healthError) healthError.hidden = true;
    }

    function renderStats(data) {
      if (statsSkeleton) statsSkeleton.hidden = true;
      if (statsError) statsError.hidden = true;
      if (statsRow) statsRow.hidden = false;

      let filled = 0;
      statFields.forEach((el) => {
        const value = fmtInt(data[el.getAttribute('data-stat')]);
        if (value === null) {
          el.textContent = '—';
          el.setAttribute('data-state', 'empty');
        } else {
          el.textContent = value;
          el.setAttribute('data-state', 'ok');
          filled++;
        }
      });

      if (statsStamp) {
        const when = fmtTime(data.generated_at);
        statsStamp.textContent = filled === 0
          ? 'API ตอบกลับสำเร็จ แต่ไม่พบตัวเลขที่รู้จักใน payload'
          : (when ? 'ข้อมูลสด ณ ' + when + ' น.' : 'ข้อมูลสดจาก /api/stats');
      }
    }

    function renderStatsError(err) {
      if (statsSkeleton) statsSkeleton.hidden = true;
      if (statsRow) statsRow.hidden = true;
      if (statsError) statsError.hidden = false;
      if (statsErrorDesc) {
        statsErrorDesc.textContent = 'GET /api/stats — ' + err.message +
          ' จึงยังไม่มีตัวเลขสดให้แสดง (ตัวเลขอ้างอิงจากไฟล์จริงอยู่ในหัวข้อ “คลังคอนเทนต์”)';
      }
      statFields.forEach((el) => {
        el.textContent = '—';
        el.setAttribute('data-state', 'error');
      });
      if (statsStamp) statsStamp.textContent = '';
    }

    function renderHealth(data) {
      if (healthSkeleton) healthSkeleton.hidden = true;
      if (healthError) healthError.hidden = true;
      if (healthBody) healthBody.hidden = false;

      const overall = normalizeCheck(data.status).status;
      const meta = CHECK_STATES[overall] || CHECK_STATES.unknown;

      if (healthSummary) {
        healthSummary.textContent = '';
        const badge = document.createElement('span');
        badge.className = 'chip ' + meta.chip;
        badge.appendChild(msIcon(meta.icon));
        badge.appendChild(document.createTextNode(meta.label));
        healthSummary.appendChild(badge);

        const bits = [];
        if (data.version) bits.push('เวอร์ชัน ' + data.version);
        const when = fmtTime(data.checked_at);
        if (when) bits.push('ตรวจเมื่อ ' + when + ' น.');
        if (bits.length) {
          const note = document.createElement('span');
          note.className = 'section-aside';
          note.textContent = bits.join(' · ');
          healthSummary.appendChild(note);
        }
      }

      if (healthChecks) {
        healthChecks.textContent = '';
        const checks = (data.checks && typeof data.checks === 'object') ? data.checks : {};
        const names = Object.keys(checks);
        if (!names.length) {
          const li = document.createElement('li');
          li.className = 'check-detail';
          li.textContent = 'payload ไม่มีฟิลด์ checks — ไม่มีรายละเอียดรายบริการให้แสดง';
          healthChecks.appendChild(li);
          return;
        }
        names.forEach((name) => {
          const c = normalizeCheck(checks[name]);
          const cm = CHECK_STATES[c.status] || CHECK_STATES.unknown;
          const li = document.createElement('li');

          const badge = document.createElement('span');
          badge.className = 'chip ' + cm.chip;
          badge.appendChild(msIcon(cm.icon));
          badge.appendChild(document.createTextNode(cm.label));
          li.appendChild(badge);

          const label = document.createElement('span');
          label.className = 'check-name';
          label.textContent = name;
          li.appendChild(label);

          if (c.detail) {
            const detail = document.createElement('span');
            detail.className = 'check-detail';
            detail.textContent = c.detail;
            li.appendChild(detail);
          }
          healthChecks.appendChild(li);
        });
      }
    }

    function renderHealthError(err) {
      if (healthSkeleton) healthSkeleton.hidden = true;
      if (healthBody) healthBody.hidden = true;
      if (healthError) healthError.hidden = false;
      if (healthErrorDesc) {
        healthErrorDesc.textContent = 'GET /api/health — ' + err.message +
          ' สถานะบริการด้านล่างจึงยังไม่สามารถยืนยันได้';
      }
    }

    let inFlight = false;

    async function load() {
      if (inFlight) return;
      inFlight = true;
      if (refreshBtn) {
        refreshBtn.disabled = true;
        if (!reducedMotion()) refreshBtn.classList.add('is-spinning');
      }
      setChip('is-checking', 'กำลังตรวจสอบ…');
      showStatsLoading();
      showHealthLoading();

      const [stats, health] = await Promise.all([
        fetchJSON('/api/stats').then(
          (d) => ({ ok: true, data: d }), (e) => ({ ok: false, error: e })),
        fetchJSON('/api/health').then(
          (d) => ({ ok: true, data: d }), (e) => ({ ok: false, error: e }))
      ]);

      if (stats.ok) renderStats(stats.data); else renderStatsError(stats.error);
      if (health.ok) renderHealth(health.data); else renderHealthError(health.error);

      if (stats.ok && health.ok) {
        const overall = normalizeCheck(health.data.status).status;
        if (overall === 'ok') setChip('is-live', 'API ออนไลน์');
        else setChip('is-warn', 'API ตอบกลับ: ' + (CHECK_STATES[overall] || CHECK_STATES.unknown).label);
      } else if (stats.ok || health.ok) {
        setChip('is-warn', 'API ตอบกลับบางส่วน');
      } else {
        const code = stats.error.status || health.error.status;
        setChip('is-error', code ? 'API ไม่พร้อมใช้งาน (HTTP ' + code + ')' : 'API ไม่พร้อมใช้งาน');
      }

      if (refreshBtn) {
        refreshBtn.disabled = false;
        refreshBtn.classList.remove('is-spinning');
      }
      inFlight = false;
    }

    if (refreshBtn) {
      refreshBtn.addEventListener('click', () => {
        load().then(() => showToast('ตรวจสอบสถานะ API อีกครั้งแล้ว', 'info'));
      });
    }
    $$('[data-api-retry]').forEach((btn) => btn.addEventListener('click', () => load()));

    load();
  }

  // ══ Boot ═══════════════════════════════════════════════
  document.addEventListener('DOMContentLoaded', () => {
    setupSidebar();
    setupSectionNav();
    setupFilters();
    setupCopy();
    setupApiPanel();
  });

  window.Workspace = { showToast, copyText, debounce, reducedMotion };
})();
