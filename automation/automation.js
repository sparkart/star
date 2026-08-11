/* ══════════════════════════════════════════════════════════
   Star — Automation Control Center

   Talks to the star_api automation control plane and nothing else.
   Two rules shape every function below:

     1. The page renders only what the API actually returned. There is no
        optimistic state: a provider is "ready" because /api/providers said
        so, a job succeeded because /api/jobs/{id} said so. A platform that
        the backend reports as manual is never drawn as published.

     2. Credentials are write-only. Configure forms are never prefilled,
        values are dropped from the DOM the moment they are sent, and no
        response is trusted to contain a secret to display.

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

  /* ══ Overview ═══════════════════════════════════════════ */

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

  function renderOverview(data) {
    var host = $('#ac-overview');
    if (!host) return;
    clear(host);

    var schedule = data.schedule || {};
    var active = data.active_job;

    host.appendChild(tile(
      'ผู้ให้บริการพร้อมใช้', 'hub',
      String(data.providers_ready != null ? data.providers_ready : '—'),
      (data.providers_pending || 0) + ' รายการยังต้องตั้งค่า'));

    host.appendChild(tile(
      'งานที่กำลังทำงาน', 'sync',
      active ? (JOB_STATUS_TH[active.status] || active.status) : 'ไม่มี',
      active ? ('ความคืบหน้า ' + (active.progress || 0) + '%') : 'คิวว่าง',
      'is-sm'));

    host.appendChild(tile(
      'ตารางเวลาอัตโนมัติ', 'schedule',
      schedule.enabled ? ('ทุกวัน ' + schedule.time) : 'ปิดอยู่',
      schedule.enabled
        ? ((schedule.dry_run ? 'โหมดซ้อม' : 'โหมดจริง') + ' · ' + (schedule.timezone || 'Asia/Bangkok'))
        : 'ค่าเริ่มต้นคือปิด',
      'is-sm'));

    var counts = data.job_counts || {};
    var totals = Object.keys(counts).reduce(function (sum, k) { return sum + counts[k]; }, 0);
    host.appendChild(tile(
      'งานทั้งหมดที่บันทึกไว้', 'history', String(totals),
      (counts.succeeded || 0) + ' สำเร็จ · ' + (counts.failed || 0) + ' ล้มเหลว · ' +
      (counts.blocked || 0) + ' ติดเงื่อนไข'));

    /* Operational warnings, only when the API actually reported them. */
    var warnings = $('#ac-overview-warnings');
    clear(warnings);
    var problems = (data.state && data.state.permission_problems) || [];
    if (problems.length) {
      warnings.appendChild(callout('warning',
        'สิทธิ์ไฟล์ไม่ปลอดภัย',
        problems.length + ' ไฟล์ในไดเรกทอรีสถานะเปิดให้ผู้ใช้อื่นอ่านได้ ' +
        'ควรแก้เป็น 0600 บนเซิร์ฟเวอร์', 'callout-warn'));
    }
    if (data.recovered_jobs) {
      warnings.appendChild(callout('restart_alt',
        'กู้คืนงานค้างหลังรีสตาร์ต',
        data.recovered_jobs + ' งานที่ค้างสถานะ "กำลังทำงาน" ถูกทำเครื่องหมายว่าล้มเหลว ' +
        'ต้องสั่งทำใหม่ด้วยตัวเอง', 'callout-info'));
    }
    if (data.state && data.state.network_disabled) {
      warnings.appendChild(callout('wifi_off',
        'เซิร์ฟเวอร์ปิดการเชื่อมต่อภายนอก',
        'ตั้งค่า STAR_DISABLE_NETWORK=1 อยู่ การทดสอบแบบ live และการเผยแพร่จริงจะถูกปฏิเสธ',
        'callout-info'));
    }
  }

  function callout(glyph, title, body, variant) {
    var node = el('div', 'callout ' + (variant || ''));
    node.appendChild(icon(glyph));
    var wrap = el('div', 'callout-body');
    wrap.appendChild(el('div', 'callout-title', title));
    wrap.appendChild(el('p', null, body));
    node.appendChild(wrap);
    return node;
  }

  /* ══ Providers ══════════════════════════════════════════ */

  function providerCard(provider) {
    var card = el('article', 'panel-card ac-provider');

    var head = el('div', 'panel-card-head');
    var title = el('div', 'ac-provider-head');
    var dot = el('span', 'ac-dot is-' + provider.status);
    dot.setAttribute('aria-hidden', 'true');
    title.appendChild(dot);
    title.appendChild(el('span', 'ac-provider-name', provider.label || provider.provider));
    head.appendChild(title);

    var badge = el('span', 'chip ' + statusChipClass(provider.status));
    badge.appendChild(el('span', null, STATUS_TH[provider.status] || provider.status));
    head.appendChild(badge);
    card.appendChild(head);

    var body = el('div', 'panel-card-body');

    /* The backend's own words, redacted server-side. Never reinterpreted. */
    body.appendChild(el('p', 'ac-provider-detail', provider.detail || ''));

    var meta = el('div', 'ac-provider-meta');
    meta.appendChild(chip('smart_toy', AUTOMATION_TH[provider.automation] || provider.automation));
    if (provider.cost) meta.appendChild(chip('payments', provider.cost));
    if (provider.token_masked) meta.appendChild(chip('key', provider.token_masked));
    if (provider.access_key_masked) meta.appendChild(chip('key', provider.access_key_masked));
    if (provider.client_id_masked) meta.appendChild(chip('badge', provider.client_id_masked));
    if (provider.project_id) meta.appendChild(chip('cloud', provider.project_id));
    if (provider.bucket) meta.appendChild(chip('inventory_2', provider.bucket));
    if (provider.page_id) meta.appendChild(chip('flag', provider.page_id));
    if (provider.key_file_mode) meta.appendChild(chip('lock', provider.key_file_mode));
    if (provider.fallback) meta.appendChild(chip('alt_route', provider.fallback));
    if (meta.childNodes.length) body.appendChild(meta);

    if (provider.prerequisites && provider.prerequisites.length) {
      var list = el('ul', 'ac-prereq');
      provider.prerequisites.forEach(function (item) {
        list.appendChild(el('li', null, item));
      });
      body.appendChild(list);
    }

    if (provider.docs) {
      body.appendChild(el('p', 'ac-field-hint', provider.docs));
    }

    if (provider.redirect_uri) {
      var uri = el('div', 'ac-field');
      uri.appendChild(el('span', 'ac-field-hint', 'Redirect URI ที่ต้องลงทะเบียนใน Google Cloud'));
      var code = el('code', 'inline', provider.redirect_uri);
      uri.appendChild(code);
      body.appendChild(uri);
    }

    /* Configure form — built from the field descriptors the API returned,
       so a new provider field appears here without a frontend change. */
    var formId = 'ac-cfg-' + provider.provider;
    var formWrap = el('div', 'ac-form');
    formWrap.id = formId;
    formWrap.hidden = true;
    if (provider.fields && provider.fields.length) {
      buildConfigForm(formWrap, provider);
    }

    var actions = el('div', 'ac-actions');

    if (provider.fields && provider.fields.length) {
      var configureBtn = el('button', 'btn btn-sm');
      configureBtn.type = 'button';
      configureBtn.setAttribute('aria-expanded', 'false');
      configureBtn.setAttribute('aria-controls', formId);
      configureBtn.appendChild(icon('tune'));
      configureBtn.appendChild(el('span', null,
        provider.configured ? 'แก้ไขการตั้งค่า' : 'ตั้งค่า'));
      configureBtn.addEventListener('click', function () {
        var open = formWrap.hidden;
        formWrap.hidden = !open;
        configureBtn.setAttribute('aria-expanded', String(open));
        if (open) {
          var first = $('input, textarea', formWrap);
          if (first) first.focus();
        }
      });
      actions.appendChild(configureBtn);
    }

    /* YouTube is the only provider with a browser OAuth handshake. */
    if (provider.provider === 'youtube') {
      var connectBtn = el('button', 'btn btn-sm');
      connectBtn.type = 'button';
      connectBtn.appendChild(icon('link'));
      connectBtn.appendChild(el('span', null,
        provider.needs_authorisation === false ? 'เชื่อมต่อใหม่' : 'เชื่อมต่อบัญชี'));
      connectBtn.addEventListener('click', function () {
        startYouTubeOAuth(connectBtn, statusNode);
      });
      actions.appendChild(connectBtn);
    }

    if (provider.automation !== 'manual') {
      var testBtn = el('button', 'btn btn-sm');
      testBtn.type = 'button';
      testBtn.appendChild(icon('network_check'));
      testBtn.appendChild(el('span', null, 'ทดสอบ'));
      testBtn.addEventListener('click', function () {
        runProviderTest(provider.provider, false, testBtn, statusNode);
      });
      actions.appendChild(testBtn);

      /* A live test may call the real platform, so it is a separate,
         explicitly labelled action rather than a hidden default. */
      if (provider.configured) {
        var liveBtn = el('button', 'btn btn-sm btn-quiet');
        liveBtn.type = 'button';
        liveBtn.title = 'เรียก API ของผู้ให้บริการจริง (ไม่สังเคราะห์เสียง ไม่อัปโหลด)';
        liveBtn.appendChild(icon('bolt'));
        liveBtn.appendChild(el('span', null, 'ทดสอบจริง'));
        liveBtn.addEventListener('click', function () {
          runProviderTest(provider.provider, true, liveBtn, statusNode);
        });
        actions.appendChild(liveBtn);
      }
    }

    var statusNode = el('p', 'status-msg');
    statusNode.setAttribute('aria-live', 'polite');

    body.appendChild(formWrap);
    body.appendChild(actions);
    body.appendChild(statusNode);
    card.appendChild(body);
    return card;
  }

  function statusChipClass(status) {
    if (status === 'ready') return 'chip-ok';
    if (status === 'error') return 'chip-danger';
    if (status === 'manual') return 'chip-warn';
    if (status === 'configured') return 'chip-info';
    return '';
  }

  function chip(glyph, text) {
    var node = el('span', 'chip');
    node.appendChild(icon(glyph));
    node.appendChild(el('span', null, String(text)));
    return node;
  }

  function buildConfigForm(host, provider) {
    provider.fields.forEach(function (field) {
      var inputId = 'ac-f-' + provider.provider + '-' + field.name;
      var wrap = el('div', 'ac-field');

      if (field.type === 'boolean') {
        var label = el('label', 'ac-check');
        var box = document.createElement('input');
        box.type = 'checkbox';
        box.id = inputId;
        box.dataset.field = field.name;
        box.dataset.kind = 'boolean';
        label.appendChild(box);
        label.appendChild(el('span', null, field.label || field.name));
        wrap.appendChild(label);
        host.appendChild(wrap);
        return;
      }

      var label2 = el('label', null, field.label || field.name);
      label2.htmlFor = inputId;
      wrap.appendChild(label2);

      var input;
      if (field.type === 'json') {
        input = document.createElement('textarea');
        input.className = 'ac-textarea';
        input.spellcheck = false;
        input.placeholder = '{ "type": "…" }';
      } else {
        input = document.createElement('input');
        input.className = 'ac-input';
        input.type = field.type === 'password' ? 'password' : 'text';
        input.autocomplete = 'off';
        input.spellcheck = false;
      }
      input.id = inputId;
      input.dataset.field = field.name;
      input.dataset.kind = field.type;
      if (field.required) input.required = true;

      /* Write-only fields are never prefilled, not even with a mask: the
         value simply is not available to this page. */
      if (field.write_only) {
        input.placeholder = provider.configured
          ? 'บันทึกไว้แล้ว — กรอกใหม่เฉพาะเมื่อต้องการเปลี่ยน'
          : (input.placeholder || 'วางค่าที่นี่');
      }
      wrap.appendChild(input);

      if (field.write_only) {
        wrap.appendChild(el('span', 'ac-field-hint',
          'ค่านี้ถูกเก็บไว้ที่เซิร์ฟเวอร์เท่านั้น และจะไม่ถูกส่งกลับมาแสดงอีก'));
      }
      wrap.appendChild(el('span', 'ac-field-error'));
      host.appendChild(wrap);
    });

    var save = el('button', 'btn btn-primary btn-sm');
    save.type = 'button';
    save.appendChild(icon('save'));
    save.appendChild(el('span', null, 'บันทึกการตั้งค่า'));
    save.addEventListener('click', function () {
      submitProviderConfig(provider, host, save);
    });

    var row = el('div', 'ac-actions');
    row.appendChild(save);
    host.appendChild(row);
  }

  function submitProviderConfig(provider, formHost, button) {
    var config = {};
    var invalid = null;

    $$('[data-field]', formHost).forEach(function (input) {
      var errorNode = input.parentNode.querySelector('.ac-field-error');
      if (errorNode) errorNode.textContent = '';
      input.removeAttribute('aria-invalid');

      if (input.dataset.kind === 'boolean') {
        config[input.dataset.field] = input.checked;
        return;
      }
      var value = input.value.trim();
      if (!value) {
        /* Empty means "leave whatever is stored alone", so required is only
           enforced when nothing is stored yet. */
        if (input.required && !provider.configured) {
          input.setAttribute('aria-invalid', 'true');
          if (errorNode) errorNode.textContent = 'จำเป็นต้องกรอก';
          if (!invalid) invalid = input;
        }
        return;
      }
      if (input.dataset.kind === 'json') {
        try {
          config[input.dataset.field] = JSON.parse(value);
        } catch (e) {
          input.setAttribute('aria-invalid', 'true');
          if (errorNode) errorNode.textContent = 'ไม่ใช่ JSON ที่ถูกต้อง';
          if (!invalid) invalid = input;
          return;
        }
      } else {
        config[input.dataset.field] = value;
      }
    });

    if (invalid) { invalid.focus(); return; }

    button.disabled = true;
    api('POST', '/api/providers/configure',
        { provider: provider.provider, config: config })
      .then(function () {
        /* Drop the entered credential from the DOM immediately. */
        $$('[data-field]', formHost).forEach(function (input) {
          if (input.dataset.kind !== 'boolean') input.value = '';
        });
        toast('บันทึกการตั้งค่า ' + (provider.label || provider.provider) + ' แล้ว', 'success');
        return loadProviders();
      })
      .catch(function (err) {
        if (err.field) {
          var target = $('[data-field="' + err.field + '"]', formHost);
          if (target) {
            target.setAttribute('aria-invalid', 'true');
            var errorNode = target.parentNode.querySelector('.ac-field-error');
            if (errorNode) errorNode.textContent = err.message;
            target.focus();
          }
        }
        toast(err.message, 'error');
      })
      .finally(function () { button.disabled = false; });
  }

  function runProviderTest(key, live, button, statusNode) {
    button.disabled = true;
    setStatus(statusNode, live ? 'กำลังทดสอบกับผู้ให้บริการจริง…' : 'กำลังตรวจสอบ…', 'info');
    api('POST', '/api/providers/test', { provider: key, live: !!live })
      .then(function (result) {
        var kind = result.status === 'ready' ? 'success'
                 : result.status === 'error' ? 'error' : 'info';
        setStatus(statusNode, result.detail || STATUS_TH[result.status] || result.status, kind);
      })
      .catch(function (err) { setStatus(statusNode, err.message, 'error'); })
      .finally(function () { button.disabled = false; });
  }

  function startYouTubeOAuth(button, statusNode) {
    button.disabled = true;
    setStatus(statusNode, 'กำลังขอลิงก์อนุญาตสิทธิ์…', 'info');
    api('GET', '/api/oauth/youtube/start')
      .then(function (result) {
        setStatus(statusNode,
          'เปิดหน้าต่างอนุญาตสิทธิ์แล้ว — Redirect URI: ' + result.redirect_uri, 'info');
        /* A new tab, because the OAuth callback lands back on this origin. */
        window.open(result.authorization_url, '_blank', 'noopener');
      })
      .catch(function (err) { setStatus(statusNode, err.message, 'error'); })
      .finally(function () { button.disabled = false; });
  }

  function loadProviders() {
    var host = $('#ac-providers');
    var empty = $('#ac-providers-empty');
    return api('GET', '/api/providers')
      .then(function (data) {
        state.providers = data.providers || [];
        clear(host);
        if (!state.providers.length) {
          empty.hidden = false;
          return;
        }
        empty.hidden = true;
        state.providers.forEach(function (provider) {
          host.appendChild(providerCard(provider));
        });
        syncPlatformAvailability();
      })
      .catch(function (err) {
        clear(host);
        empty.hidden = false;
        clear(empty);
        empty.appendChild(icon('cloud_off', 'state-icon'));
        empty.appendChild(el('p', 'state-title', 'โหลดข้อมูลผู้ให้บริการไม่สำเร็จ'));
        empty.appendChild(el('p', 'state-desc', err.message));
      });
  }

  /* Publish targets are drawn from the provider list so the UI can never
     offer a platform the backend does not know about. */
  function syncPlatformAvailability() {
    var host = $('#ac-platforms');
    if (!host) return;
    var checked = selectedValues('platform');
    clear(host);

    state.providers
      .filter(function (p) { return p.provider !== 'claude' && p.provider !== 'google_tts'; })
      .forEach(function (provider) {
        var manual = provider.automation !== 'full_auto';
        var id = 'ac-platform-' + provider.provider;
        var wrap = el('label', 'ac-pill');
        wrap.htmlFor = id;
        var box = document.createElement('input');
        box.type = 'checkbox';
        box.id = id;
        box.name = 'platform';
        box.value = provider.provider;
        box.checked = checked.indexOf(provider.provider) !== -1;
        box.addEventListener('change', updateRunWarning);
        var face = el('span');
        face.appendChild(icon(manual ? 'front_hand' : 'cloud_upload'));
        face.appendChild(el('span', null, provider.label || provider.provider));
        wrap.appendChild(box);
        wrap.appendChild(face);
        wrap.title = manual
          ? (provider.label + ' ต้องอัปโหลดเอง ระบบจะเตรียมไฟล์ให้เท่านั้น')
          : (provider.detail || '');
        host.appendChild(wrap);
      });

    var manualNote = $('#ac-manual-note');
    var manualNames = state.providers
      .filter(function (p) { return p.automation !== 'full_auto'; })
      .map(function (p) { return p.label; });
    if (manualNote) {
      manualNote.textContent = manualNames.length
        ? (manualNames.join(' และ ') +
           ' ไม่มีช่องทางเผยแพร่อัตโนมัติที่ใช้ได้จริง ระบบจะสร้างชุดไฟล์สำหรับอัปโหลดเองไว้ที่ ' +
           'output/<วันที่>/handoff/ และจะไม่รายงานว่าเผยแพร่สำเร็จ')
        : '';
    }
    updateRunWarning();
    syncSchedulePlatforms();
  }

  /* The scheduled-run targets come from /api/automation/overview
     (`platforms.automatable` / `platforms.manual`) — never from a list hard
     coded here — so the form can only ever offer what the backend accepts for
     an unattended run. Manual platforms are rendered disabled rather than
     hidden: the operator should see why TikTok/Shopee are not an option. */
  function providerLabel(key) {
    var found = (state.providers || []).filter(function (p) {
      return p.provider === key;
    })[0];
    return (found && found.label) || key;
  }

  function schedulePlatformMeta() {
    var meta = (state.overview && state.overview.platforms) || {};
    return {
      automatable: meta.automatable || [],
      manual: meta.manual || []
    };
  }

  function syncSchedulePlatforms() {
    var host = $('#ac-sched-platforms');
    if (!host) return;
    var meta = schedulePlatformMeta();

    /* Keep what the operator already ticked; before the first render fall back
       to what the server has stored — which defaults to nothing selected. */
    var selected = host.firstChild
      ? selectedValues('splatform')
      : ((state.schedule && state.schedule.platforms) || []);

    clear(host);
    meta.automatable.forEach(function (key) {
      var id = 'ac-splatform-' + key;
      var wrap = el('label', 'ac-pill');
      wrap.htmlFor = id;
      var box = document.createElement('input');
      box.type = 'checkbox';
      box.id = id;
      box.name = 'splatform';
      box.value = key;
      box.checked = selected.indexOf(key) !== -1;
      box.addEventListener('change', updateScheduleWarning);
      var face = el('span');
      face.appendChild(icon('cloud_upload'));
      face.appendChild(el('span', null, providerLabel(key)));
      wrap.appendChild(box);
      wrap.appendChild(face);
      host.appendChild(wrap);
    });

    meta.manual.forEach(function (key) {
      var label = providerLabel(key);
      var wrap = el('label', 'ac-pill');
      var box = document.createElement('input');
      box.type = 'checkbox';
      /* A distinct name and `disabled`: this control can neither be ticked nor
         be read back by selectedValues('splatform'). */
      box.name = 'splatform-manual';
      box.value = key;
      box.disabled = true;
      box.checked = false;
      var face = el('span');
      face.appendChild(icon('front_hand'));
      face.appendChild(el('span', null, label + ' — อัปโหลดเอง'));
      wrap.appendChild(box);
      wrap.appendChild(face);
      wrap.title = label + ' ไม่มีช่องทางเผยแพร่อัตโนมัติ จึงตั้งเวลาให้เผยแพร่เองไม่ได้';
      wrap.setAttribute('aria-disabled', 'true');
      host.appendChild(wrap);
    });

    var note = $('#ac-sched-manual-note');
    if (note) {
      var manualNames = meta.manual.map(providerLabel);
      note.textContent = manualNames.length
        ? (manualNames.join(' และ ') +
           ' ตั้งเวลาเผยแพร่อัตโนมัติไม่ได้ ต้องสร้างงานครั้งเดียวแล้วอัปโหลดเองจากชุดไฟล์ ' +
           'output/<วันที่>/handoff/')
        : '';
    }
    updateScheduleWarning();
  }

  /* Publish on a schedule needs a target the backend will accept, and the
     backend rejects the combination — say so before the request is sent. */
  function scheduleBlockReason() {
    var stages = selectedValues('sstage');
    if (stages.indexOf('publish') === -1) return null;
    if (selectedValues('splatform').length) return null;
    return 'เลือกขั้นตอน "เผยแพร่" ไว้แต่ยังไม่ได้เลือกปลายทางที่เผยแพร่อัตโนมัติได้ ' +
           'เซิร์ฟเวอร์จะปฏิเสธคำขอนี้';
  }

  function updateScheduleWarning() {
    var reason = scheduleBlockReason();
    setStatus($('#ac-sched-warning'), reason || '', reason ? 'error' : null);
  }

  /* ══ Run form ═══════════════════════════════════════════ */

  function selectedValues(name) {
    return $$('input[name="' + name + '"]:checked').map(function (b) { return b.value; });
  }

  function buildPills(host, items, name, glyphKey, ordered, onChange) {
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
      box.addEventListener('change', onChange || updateRunWarning);
      var face = el('span');
      if (ordered) face.appendChild(el('span', 'ac-pill-order', String(index + 1)));
      if (item[glyphKey]) face.appendChild(icon(item[glyphKey]));
      face.appendChild(el('span', null, item.label));
      wrap.appendChild(box);
      wrap.appendChild(face);
      host.appendChild(wrap);
    });
  }

  function updateRunWarning() {
    var dryRun = $('#ac-dry-run');
    var box = $('#ac-cost');
    if (!box || !dryRun) return;

    var stages = selectedValues('stage');
    var platforms = selectedValues('platform');
    var days = selectedValues('day');
    var from = $('#ac-from-date').value;
    var to = $('#ac-to-date').value || from;

    var dateCount = 1;
    if (from && to) {
      var ms = Date.parse(to + 'T00:00:00Z') - Date.parse(from + 'T00:00:00Z');
      dateCount = isNaN(ms) ? 1 : Math.floor(ms / 86400000) + 1;
    }
    var units = Math.max(0, dateCount) * (days.length || 0);

    /* Publish is only meaningful with a target, and the backend rejects it
       without one — say so before the request is sent. */
    var publishNoTarget = stages.indexOf('publish') !== -1 && !platforms.length;

    clear(box);
    box.className = 'ac-cost ' + (dryRun.checked ? 'is-dry' : 'is-live');
    box.appendChild(icon(dryRun.checked ? 'science' : 'warning'));
    var bodyWrap = el('div', 'ac-cost-body');

    if (dryRun.checked) {
      bodyWrap.appendChild(el('div', 'ac-cost-title', 'โหมดซ้อม (dry run) — ไม่มีการเรียกผู้ให้บริการ'));
      bodyWrap.appendChild(el('p', 'ac-cost-text',
        'ระบบจะตรวจเงื่อนไขและแสดงคำสั่งที่จะรันเท่านั้น ไม่เขียนไฟล์ลงโปรเจกต์ ' +
        'ไม่มีค่าใช้จ่าย และไม่เผยแพร่อะไรทั้งสิ้น'));
    } else {
      bodyWrap.appendChild(el('div', 'ac-cost-title', 'โหมดจริง — มีการเรียกใช้งานจริงและอาจมีค่าใช้จ่าย'));
      var parts = [];
      if (stages.indexOf('script') !== -1) {
        parts.push('เขียนบทด้วย Claude CLI ' + units + ' ครั้ง (ใช้สิทธิ์ subscription ไม่ใช่ API key รายครั้ง)');
      }
      if (stages.indexOf('audio') !== -1) {
        parts.push('สังเคราะห์เสียง ' + units + ' ไฟล์ (Google Cloud TTS คิดค่าบริการตามจำนวนอักขระ)');
      }
      if (stages.indexOf('video') !== -1) {
        parts.push('เรนเดอร์วิดีโอ ' + units + ' ไฟล์ด้วย ffmpeg บนเครื่องนี้');
      }
      if (stages.indexOf('publish') !== -1 && platforms.length) {
        parts.push('เผยแพร่ไปยัง ' + platforms.length + ' ปลายทาง (นับโควตาจริงของแต่ละแพลตฟอร์ม)');
      }
      bodyWrap.appendChild(el('p', 'ac-cost-text',
        parts.length ? parts.join(' · ') : 'เลือกขั้นตอนอย่างน้อยหนึ่งขั้นก่อนเริ่มงาน'));
    }

    if (publishNoTarget) {
      bodyWrap.appendChild(el('p', 'ac-cost-text',
        'เลือกขั้นตอน "เผยแพร่" ไว้แต่ยังไม่ได้เลือกปลายทาง เซิร์ฟเวอร์จะปฏิเสธคำขอนี้'));
    }
    if (units > 0) {
      bodyWrap.appendChild(el('p', 'ac-cost-text',
        'ขอบเขต: ' + dateCount + ' วันปฏิทิน × ' + days.length + ' วันเกิด = ' + units + ' ชิ้นงาน'));
    }
    box.appendChild(bodyWrap);

    var runBtn = $('#ac-run');
    if (runBtn) {
      clear(runBtn);
      runBtn.appendChild(icon(dryRun.checked ? 'science' : 'play_arrow'));
      runBtn.appendChild(el('span', null, dryRun.checked ? 'เริ่มงานแบบซ้อม' : 'เริ่มงานจริง'));
    }
  }

  function submitJob() {
    var button = $('#ac-run');
    var statusNode = $('#ac-run-status');
    var body = {
      from_date: $('#ac-from-date').value,
      to_date: $('#ac-to-date').value || $('#ac-from-date').value,
      days: selectedValues('day'),
      stages: selectedValues('stage'),
      dry_run: $('#ac-dry-run').checked,
      force: $('#ac-force').checked
    };
    var platforms = selectedValues('platform');
    if (platforms.length) body.platforms = platforms;

    button.disabled = true;
    setStatus(statusNode, 'กำลังส่งงานเข้าคิว…', 'info');

    api('POST', '/api/jobs', body)
      .then(function (job) {
        setStatus(statusNode, '', null);
        toast('สร้างงานแล้ว', 'success');
        followJob(job);
        loadJobs();
        var progress = $('#ac-progress-section');
        if (progress) progress.scrollIntoView({ behavior: 'smooth', block: 'start' });
      })
      .catch(function (err) {
        if (err.status === 409 && err.payload && err.payload.active_job) {
          var active = err.payload.active_job;
          setStatus(statusNode,
            'มีงานทำงานอยู่แล้ว (' + (JOB_STATUS_TH[active.status] || active.status) +
            ') — ยกเลิกงานเดิมก่อนจึงจะเริ่มงานใหม่ได้', 'error');
          followJob(active);
        } else {
          setStatus(statusNode, err.message, 'error');
        }
      })
      .finally(function () { button.disabled = false; });
  }

  /* ══ Job progress ═══════════════════════════════════════ */

  function followJob(job) {
    state.job = job;
    state.lastEventId = 0;
    clear($('#ac-log'));
    renderJob(job);
    schedulePoll(0);
  }

  function schedulePoll(delay) {
    if (state.pollTimer) clearTimeout(state.pollTimer);
    if (!state.job) return;
    state.pollTimer = setTimeout(pollJob, delay == null ? POLL_ACTIVE : delay);
  }

  function pollJob() {
    if (!state.job) return;
    var id = state.job.id;
    api('GET', '/api/jobs/' + encodeURIComponent(id) +
               '?events=200&after_id=' + state.lastEventId)
      .then(function (job) {
        state.job = job;
        renderJob(job);
        appendEvents(job.events || []);
        if (job.status === 'queued' || job.status === 'running') {
          schedulePoll(POLL_ACTIVE);
        } else {
          state.pollTimer = null;
          loadJobs();
          loadOverview();
        }
      })
      .catch(function (err) {
        setStatus($('#ac-progress-status'),
                  'ติดตามสถานะงานไม่สำเร็จ: ' + err.message, 'error');
        state.pollTimer = null;
      });
  }

  function renderJob(job) {
    var section = $('#ac-progress-section');
    var empty = $('#ac-progress-empty');
    var panel = $('#ac-progress-panel');
    if (!section) return;
    empty.hidden = true;
    panel.hidden = false;

    $('#ac-job-id').textContent = job.id;
    var input = job.input || {};
    $('#ac-job-scope').textContent =
      (input.from_date || '?') +
      (input.to_date && input.to_date !== input.from_date ? ' – ' + input.to_date : '') +
      ' · ' + ((input.days || []).length) + ' วันเกิด' +
      ' · ' + (input.dry_run ? 'โหมดซ้อม' : 'โหมดจริง');

    var chipNode = $('#ac-job-status');
    clear(chipNode);
    chipNode.className = 'chip ac-chip-' + job.status;
    chipNode.appendChild(icon(JOB_STATUS_ICON[job.status] || 'help'));
    chipNode.appendChild(el('span', null, JOB_STATUS_TH[job.status] || job.status));

    var pct = typeof job.progress === 'number' ? job.progress : 0;
    var bar = $('#ac-bar-fill');
    bar.style.width = pct + '%';
    bar.className = 'ac-bar-fill is-' + job.status;
    var meter = $('#ac-bar');
    meter.setAttribute('aria-valuenow', String(pct));
    meter.setAttribute('aria-valuetext', pct + ' เปอร์เซ็นต์ — ' +
                       (JOB_STATUS_TH[job.status] || job.status));
    $('#ac-progress-pct').textContent = pct + '%';

    /* Stage track reflects the stages this job actually requested. */
    var track = $('#ac-track');
    clear(track);
    (input.stages || []).forEach(function (key) {
      var item = el('li');
      var done = job.status === 'succeeded' ||
                 (input.stages.indexOf(job.current_stage) > input.stages.indexOf(key));
      if (job.current_stage === key) item.className = 'is-active';
      else if (done) item.className = 'is-done';
      item.appendChild(icon(job.current_stage === key ? 'progress_activity'
                            : done ? 'check' : 'radio_button_unchecked'));
      item.appendChild(el('span', null, STAGE_LABEL[key] || key));
      track.appendChild(item);
    });

    var statusNode = $('#ac-progress-status');
    if (job.status === 'blocked') {
      setStatus(statusNode, 'ติดเงื่อนไข: ' + (job.safe_error || 'ไม่ระบุ'), 'error');
    } else if (job.status === 'failed') {
      setStatus(statusNode, 'ล้มเหลว: ' + (job.safe_error || 'ไม่ระบุ'), 'error');
    } else if (job.status === 'cancelled') {
      setStatus(statusNode, job.safe_error || 'ยกเลิกแล้ว', 'info');
    } else if (job.status === 'succeeded') {
      setStatus(statusNode, input.dry_run
        ? 'ซ้อมเสร็จแล้ว — ไม่มีการเรียกผู้ให้บริการและไม่มีการเผยแพร่'
        : 'งานเสร็จสมบูรณ์', 'success');
    } else {
      setStatus(statusNode, '', null);
    }

    var active = job.status === 'queued' || job.status === 'running';
    $('#ac-cancel').hidden = !active;
    $('#ac-cancel').disabled = !active || job.cancel_requested;
    $('#ac-retry').hidden = active;

    renderResult(job);
  }

  /* The dry-run plan and the production result both live in job.result. */
  function renderResult(job) {
    var host = $('#ac-result');
    clear(host);
    if (!job.result) { host.hidden = true; return; }
    host.hidden = false;

    var result = job.result;
    if (result.dry_run) {
      var unmet = result.unmet_prerequisites || [];
      if (unmet.length) {
        var warn = el('ul', 'ac-prereq');
        unmet.forEach(function (item) { warn.appendChild(el('li', null, item)); });
        var box = callout('report', 'เงื่อนไขที่ยังไม่ครบ',
          'ถ้ารันโหมดจริงตอนนี้ งานจะหยุดที่สถานะ "ติดเงื่อนไข"', 'callout-warn');
        box.querySelector('.callout-body').appendChild(warn);
        host.appendChild(box);
      }
      (result.stages || []).forEach(function (stage) {
        var card = el('div', 'panel-card is-flush');
        var head = el('div', 'panel-card-head');
        head.appendChild(el('h3', null, STAGE_LABEL[stage.stage] || stage.stage));
        head.appendChild(el('span', 'chip', (stage.planned || []).length + ' ขั้นตอน'));
        card.appendChild(head);
        var bodyNode = el('div', 'panel-card-body');
        var list = el('div', 'ac-log');
        (stage.planned || []).forEach(function (item) {
          var line = el('div', 'ac-log-line');
          var text = el('span', 'ac-log-text',
            item.description + (item.command ? '  →  ' + item.command : '') +
            (item.output ? '  ⟶  ' + item.output : ''));
          line.appendChild(text);
          list.appendChild(line);
        });
        if (!(stage.planned || []).length) {
          list.appendChild(el('p', 'ac-log-empty', 'ไม่มีขั้นตอนที่ต้องทำ'));
        }
        bodyNode.appendChild(list);
        card.appendChild(bodyNode);
        host.appendChild(card);
      });
      if (result.note) host.appendChild(el('p', 'ac-field-hint', result.note));
      return;
    }

    /* Production result: report exactly what each stage returned. */
    (result.stages || []).forEach(function (stage) {
      var card = el('div', 'panel-card is-flush');
      var head = el('div', 'panel-card-head');
      head.appendChild(el('h3', null, STAGE_LABEL[stage.stage] || stage.stage));
      card.appendChild(head);
      var bodyNode = el('div', 'panel-card-body');
      var publish = stage.result && stage.result.publish;
      if (publish) {
        Object.keys(publish).forEach(function (platform) {
          var outcome = publish[platform];
          var row = el('div', 'ac-job');
          var main = el('div', 'ac-job-main');
          main.appendChild(el('div', 'ac-job-title', platform));
          main.appendChild(el('div', 'ac-job-meta',
            outcome.published ? 'เผยแพร่แล้ว' : (outcome.note || 'ยังไม่เผยแพร่')));
          row.appendChild(main);
          row.appendChild(chip(outcome.published ? 'check_circle' : 'front_hand',
                               outcome.published ? 'published' : 'manual_handoff'));
          bodyNode.appendChild(row);
        });
      } else {
        var pre = el('pre', 'ac-log');
        pre.textContent = JSON.stringify(stage.result || {}, null, 2);
        bodyNode.appendChild(pre);
      }
      card.appendChild(bodyNode);
      host.appendChild(card);
    });
  }

  function appendEvents(events) {
    if (!events.length) return;
    var log = $('#ac-log');
    var placeholder = $('.ac-log-empty', log);
    if (placeholder) placeholder.remove();

    events.forEach(function (event) {
      if (event.id > state.lastEventId) state.lastEventId = event.id;
      var line = el('div', 'ac-log-line' +
        (event.level === 'error' ? ' is-error' : event.level === 'warn' ? ' is-warn' : ''));
      line.appendChild(el('span', 'ac-log-time', fmtClock(event.ts)));
      line.appendChild(el('span', 'ac-log-text',
        (event.stage ? '[' + event.stage + '] ' : '') + event.message));
      log.appendChild(line);
    });

    while (log.childNodes.length > MAX_LOG_LINES) log.removeChild(log.firstChild);
    if ($('#ac-log-follow').checked) log.scrollTop = log.scrollHeight;
  }

  function cancelJob() {
    if (!state.job) return;
    var button = $('#ac-cancel');
    button.disabled = true;
    api('POST', '/api/jobs/' + encodeURIComponent(state.job.id) + '/cancel')
      .then(function (job) {
        state.job = job;
        renderJob(job);
        toast('ส่งคำสั่งยกเลิกแล้ว', 'info');
        schedulePoll(300);
      })
      .catch(function (err) {
        toast(err.message, 'error');
        button.disabled = false;
      });
  }

  /* Manual only, on purpose: nothing in this system retries by itself. */
  function retryJob() {
    if (!state.job) return;
    var button = $('#ac-retry');
    button.disabled = true;
    api('POST', '/api/jobs/' + encodeURIComponent(state.job.id) + '/retry')
      .then(function (job) {
        toast('สร้างงานใหม่จากงานเดิมแล้ว', 'success');
        followJob(job);
        loadJobs();
      })
      .catch(function (err) { toast(err.message, 'error'); })
      .finally(function () { button.disabled = false; });
  }

  /* ══ Job history ════════════════════════════════════════ */

  function loadJobs() {
    var host = $('#ac-jobs');
    var empty = $('#ac-jobs-empty');
    return api('GET', '/api/jobs?limit=20')
      .then(function (data) {
        var jobs = data.jobs || [];
        clear(host);
        if (!jobs.length) {
          empty.hidden = false;
          return;
        }
        empty.hidden = true;
        jobs.forEach(function (job) { host.appendChild(jobRow(job)); });
      })
      .catch(function (err) {
        clear(host);
        empty.hidden = false;
        clear(empty);
        empty.appendChild(icon('cloud_off', 'state-icon'));
        empty.appendChild(el('p', 'state-title', 'โหลดประวัติงานไม่สำเร็จ'));
        empty.appendChild(el('p', 'state-desc', err.message));
      });
  }

  function jobRow(job) {
    var row = el('div', 'ac-job');
    var main = el('div', 'ac-job-main');
    var input = job.input || {};
    main.appendChild(el('div', 'ac-job-title',
      (input.from_date || '?') +
      (input.to_date && input.to_date !== input.from_date ? ' – ' + input.to_date : '') +
      ' · ' + (input.stages || []).map(function (s) { return STAGE_LABEL[s] || s; }).join(', ')));
    var meta = fmtTime(job.created_at) +
      ' · ' + (input.dry_run ? 'ซ้อม' : 'จริง') +
      (job.origin && job.origin !== 'manual' ? ' · ' + job.origin : '') +
      (job.parent_id ? ' · ทำใหม่จากงานก่อนหน้า' : '');
    main.appendChild(el('div', 'ac-job-meta', meta));
    if (job.safe_error) main.appendChild(el('div', 'ac-job-meta', job.safe_error));
    row.appendChild(main);

    var statusChip = el('span', 'chip ac-chip-' + job.status);
    statusChip.appendChild(icon(JOB_STATUS_ICON[job.status] || 'help'));
    statusChip.appendChild(el('span', null, JOB_STATUS_TH[job.status] || job.status));
    row.appendChild(statusChip);

    var open = el('button', 'btn btn-sm btn-quiet');
    open.type = 'button';
    open.appendChild(icon('visibility'));
    open.appendChild(el('span', null, 'ดูรายละเอียด'));
    open.setAttribute('aria-label', 'ดูรายละเอียดงาน ' + job.id);
    open.addEventListener('click', function () {
      followJob(job);
      $('#ac-progress-section').scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
    row.appendChild(open);
    return row;
  }

  /* ══ Schedule ═══════════════════════════════════════════ */

  function loadSchedule() {
    return api('GET', '/api/schedule')
      .then(function (config) {
        state.schedule = config;
        $('#ac-sched-enabled').checked = !!config.enabled;
        $('#ac-sched-time').value = config.time || '05:30';
        $('#ac-sched-offset').value = String(config.date_offset_days != null
                                             ? config.date_offset_days : 0);
        $('#ac-sched-dry').checked = config.dry_run !== false;
        applySelection('sday', config.days || []);
        applySelection('sstage', config.stages || []);
        /* Renders the target pills and ticks exactly what the server stored —
           for an untouched install that is nothing at all. */
        syncSchedulePlatforms();
        applySelection('splatform', config.platforms || []);
        updateScheduleWarning();
        $('#ac-sched-tz').textContent = config.timezone || 'Asia/Bangkok';
        $('#ac-sched-last').textContent = config.last_run_date
          ? ('รันล่าสุด ' + config.last_run_date) : 'ยังไม่เคยรัน';
        setStatus($('#ac-sched-status'), '', null);
      })
      .catch(function (err) {
        setStatus($('#ac-sched-status'), 'โหลดตารางเวลาไม่สำเร็จ: ' + err.message, 'error');
      });
  }

  function applySelection(name, values) {
    $$('input[name="' + name + '"]').forEach(function (box) {
      box.checked = values.indexOf(box.value) !== -1;
    });
  }

  function saveSchedule() {
    var button = $('#ac-sched-save');
    var statusNode = $('#ac-sched-status');
    var blocked = scheduleBlockReason();
    if (blocked) {
      setStatus(statusNode, blocked, 'error');
      updateScheduleWarning();
      return;
    }
    var body = {
      enabled: $('#ac-sched-enabled').checked,
      time: $('#ac-sched-time').value,
      date_offset_days: parseInt($('#ac-sched-offset').value, 10) || 0,
      days: selectedValues('sday'),
      stages: selectedValues('sstage'),
      dry_run: $('#ac-sched-dry').checked
    };
    /* Only the automatable keys the backend advertised can be here: the manual
       pills are disabled and carry a different name. Filter anyway so a stale
       render can never submit a target the schedule endpoint rejects. */
    var automatable = schedulePlatformMeta().automatable;
    var platforms = selectedValues('splatform').filter(function (key) {
      return automatable.indexOf(key) !== -1;
    });
    if (platforms.length) body.platforms = platforms;

    button.disabled = true;
    setStatus(statusNode, 'กำลังบันทึก…', 'info');
    api('PUT', '/api/schedule', body)
      .then(function (config) {
        state.schedule = config;
        /* Reflect what was actually stored, not what was submitted. */
        $('#ac-sched-enabled').checked = !!config.enabled;
        $('#ac-sched-time').value = config.time;
        applySelection('sday', config.days || []);
        applySelection('sstage', config.stages || []);
        applySelection('splatform', config.platforms || []);
        updateScheduleWarning();
        setStatus(statusNode, config.enabled
          ? ('เปิดใช้งานแล้ว — จะรันทุกวันเวลา ' + config.time + ' ตามเวลากรุงเทพฯ')
          : 'บันทึกแล้ว — ตารางเวลาปิดอยู่', 'success');
        loadOverview();
      })
      .catch(function (err) { setStatus(statusNode, err.message, 'error'); })
      .finally(function () { button.disabled = false; });
  }

  /* ══ Boot ═══════════════════════════════════════════════ */

  function loadOverview() {
    var host = $('#ac-overview');
    return api('GET', '/api/automation/overview')
      .then(function (data) {
        state.overview = data;
        if (data.limits) state.limits = data.limits;
        renderOverview(data);
        /* The schedule targets are derived from data.platforms, so re-render
           them as soon as that metadata lands. */
        syncSchedulePlatforms();
        $('#ac-overview-error').hidden = true;
        var note = $('#ac-range-note');
        if (note && data.limits) {
          note.textContent = 'ช่วงวันสูงสุด ' + data.limits.max_range_days +
            ' วันต่อหนึ่งงาน และรันได้ครั้งละ ' + data.limits.max_concurrent_jobs + ' งาน';
        }
        /* Adopt an already-running job so a page reload keeps following it. */
        if (data.active_job && !state.job) followJob(data.active_job);
      })
      .catch(function (err) {
        clear(host);
        var box = $('#ac-overview-error');
        box.hidden = false;
        clear(box);
        box.appendChild(icon('cloud_off', 'state-icon'));
        box.appendChild(el('p', 'state-title', 'เชื่อมต่อ API ไม่ได้'));
        box.appendChild(el('p', 'state-desc', err.message));
        var retry = el('button', 'btn btn-sm');
        retry.type = 'button';
        retry.appendChild(icon('refresh'));
        retry.appendChild(el('span', null, 'ลองใหม่'));
        retry.addEventListener('click', boot);
        box.appendChild(retry);
      });
  }

  function boot() {
    return Promise.all([loadOverview(), loadProviders(), loadJobs(), loadSchedule()]);
  }

  function init() {
    if (!$('#ac-overview')) return;   /* not this page */

    buildPills($('#ac-days'), DAYS, 'day', null, false);
    buildPills($('#ac-stages'), STAGES, 'stage', 'icon', true);
    buildPills($('#ac-sched-days'), DAYS, 'sday', null, false, updateScheduleWarning);
    buildPills($('#ac-sched-stages'), STAGES, 'sstage', 'icon', true, updateScheduleWarning);

    /* Sensible, safe defaults: today, every birth-day, no publish stage. */
    var today = todayISO();
    $('#ac-from-date').value = today;
    $('#ac-to-date').value = today;
    applySelection('day', DAYS.map(function (d) { return d.key; }));
    applySelection('stage', ['astro', 'script', 'audio', 'video']);
    applySelection('sday', DAYS.map(function (d) { return d.key; }));
    applySelection('sstage', ['astro', 'script', 'audio', 'video']);

    $('#ac-dry-run').addEventListener('change', updateRunWarning);
    $('#ac-from-date').addEventListener('change', updateRunWarning);
    $('#ac-to-date').addEventListener('change', updateRunWarning);
    $('#ac-run').addEventListener('click', submitJob);
    $('#ac-cancel').addEventListener('click', cancelJob);
    $('#ac-retry').addEventListener('click', retryJob);
    $('#ac-sched-save').addEventListener('click', saveSchedule);
    $('#ac-refresh').addEventListener('click', function () {
      toast('กำลังโหลดข้อมูลใหม่', 'info');
      boot();
    });

    $('#ac-select-all-days').addEventListener('click', function () {
      applySelection('day', DAYS.map(function (d) { return d.key; }));
      updateRunWarning();
    });
    $('#ac-clear-days').addEventListener('click', function () {
      applySelection('day', []);
      updateRunWarning();
    });

    updateRunWarning();
    boot();

    window.addEventListener('beforeunload', function () {
      if (state.pollTimer) clearTimeout(state.pollTimer);
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
