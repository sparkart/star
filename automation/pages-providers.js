/* ══════════════════════════════════════════════════════════
   Star — Provider connections (/automation/providers/)

   The only page that can write a credential, and it treats every one of them
   as write-only:

     · a configure form is built from the field descriptors /api/providers
       returned, so a new backend field appears here with no frontend change;
     · nothing is ever prefilled — not even with a mask — because the value is
       simply not available to this page;
     · the entered value is cleared out of the DOM the moment the server has
       accepted it, so a credential never outlives its request.

   A "live" test may reach the real platform, so it is a separate, explicitly
   labelled button rather than a hidden default of the ordinary test.
   ══════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  var AC = window.StarAC;
  if (!AC) return;

  var $ = AC.$, $$ = AC.$$, el = AC.el, icon = AC.icon, clear = AC.clear;
  var STATUS_TH = AC.STATUS_TH, AUTOMATION_TH = AC.AUTOMATION_TH;
  var setStatus = AC.setStatus, toast = AC.toast, api = AC.api;

  function statusChipClass(status) {
    if (status === 'ready') return 'chip-ok';
    if (status === 'error') return 'chip-danger';
    if (status === 'manual') return 'chip-warn';
    if (status === 'configured') return 'chip-info';
    return '';
  }

  /* ── one provider card ─────────────────────────────────── */

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
    meta.appendChild(AC.chip('smart_toy',
      AUTOMATION_TH[provider.automation] || provider.automation));
    if (provider.cost) meta.appendChild(AC.chip('payments', provider.cost));
    if (provider.token_masked) meta.appendChild(AC.chip('key', provider.token_masked));
    if (provider.access_key_masked) meta.appendChild(AC.chip('key', provider.access_key_masked));
    if (provider.client_id_masked) meta.appendChild(AC.chip('badge', provider.client_id_masked));
    if (provider.project_id) meta.appendChild(AC.chip('cloud', provider.project_id));
    if (provider.bucket) meta.appendChild(AC.chip('inventory_2', provider.bucket));
    if (provider.page_id) meta.appendChild(AC.chip('flag', provider.page_id));
    if (provider.key_file_mode) meta.appendChild(AC.chip('lock', provider.key_file_mode));
    if (provider.selected_voice_name) {
      meta.appendChild(AC.chip('record_voice_over', provider.selected_voice_name));
    }
    if (provider.fallback) meta.appendChild(AC.chip('alt_route', provider.fallback));
    if (meta.childNodes.length) body.appendChild(meta);

    if (provider.prerequisites && provider.prerequisites.length) {
      var list = el('ul', 'ac-prereq');
      provider.prerequisites.forEach(function (item) {
        list.appendChild(el('li', null, item));
      });
      body.appendChild(list);
    }

    if (provider.docs) body.appendChild(el('p', 'ac-field-hint', provider.docs));

    if (provider.redirect_uri) {
      var uri = el('div', 'ac-field');
      uri.appendChild(el('span', 'ac-field-hint',
        'Redirect URI ที่ต้องลงทะเบียนใน Google Cloud'));
      uri.appendChild(el('code', 'inline', provider.redirect_uri));
      body.appendChild(uri);
    }

    /* Declared before the buttons that close over it. */
    var statusNode = el('p', 'status-msg');
    statusNode.setAttribute('aria-live', 'polite');

    var formId = 'ac-cfg-' + provider.provider;
    var formWrap = el('div', 'ac-form');
    formWrap.id = formId;
    formWrap.hidden = true;
    if (provider.fields && provider.fields.length) buildConfigForm(formWrap, provider);

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
        } else {
          /* Collapsing discards anything typed but not saved, rather than
             leaving a credential sitting in a hidden field. */
          clearSecretInputs(formWrap);
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
    } else {
      /* Manual providers have no endpoint to test: say why instead of
         offering a button that could only ever fail. */
      body.appendChild(AC.callout('front_hand', 'ต้องอัปโหลดเอง',
        (provider.label || provider.provider) +
        ' ไม่มีช่องทางเผยแพร่อัตโนมัติที่ใช้ได้จริง ระบบจะเตรียมชุดไฟล์ไว้ที่ ' +
        'output/<วันที่>/handoff/ ให้อัปโหลดเอง และจะไม่รายงานว่าเผยแพร่สำเร็จ',
        'callout-warn'));
    }

    body.appendChild(formWrap);
    body.appendChild(actions);
    body.appendChild(statusNode);
    card.appendChild(body);
    return card;
  }

  /* ── configure form ────────────────────────────────────── */

  function clearSecretInputs(formHost) {
    $$('[data-field]', formHost).forEach(function (input) {
      /* A select holds a stored, non-secret choice rather than a typed
         credential: blanking it would leave the form with no valid value. */
      if (input.dataset.kind === 'boolean' || input.dataset.kind === 'select') return;
      input.value = '';
    });
  }

  function buildConfigForm(host, provider) {
    provider.fields.forEach(function (field) {
      var inputId = 'ac-f-' + provider.provider + '-' + field.name;
      var wrap = el('div', 'ac-field');

      if (field.type === 'boolean') {
        var check = el('label', 'ac-check');
        var box = document.createElement('input');
        box.type = 'checkbox';
        box.id = inputId;
        box.dataset.field = field.name;
        box.dataset.kind = 'boolean';
        check.appendChild(box);
        check.appendChild(el('span', null, field.label || field.name));
        wrap.appendChild(check);
        host.appendChild(wrap);
        return;
      }

      var label = el('label', null, field.label || field.name);
      label.htmlFor = inputId;
      wrap.appendChild(label);

      var input;
      if (field.type === 'json') {
        input = document.createElement('textarea');
        input.className = 'ac-textarea';
        input.spellcheck = false;
        input.placeholder = '{ "type": "…" }';
      } else if (field.type === 'select') {
        input = document.createElement('select');
        input.className = 'ac-select';
        (field.options || []).forEach(function (option) {
          var choice = document.createElement('option');
          choice.value = option.value;
          choice.textContent = option.label || option.value;
          /* The current choice is marked on its own option rather than
             assigned into the form, so no field is ever prefilled here. */
          if (option.value === (field.selected || field.default)) {
            choice.selected = true;
          }
          input.appendChild(choice);
        });
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

      if (field.hint) wrap.appendChild(el('span', 'ac-field-hint', field.hint));
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
        clearSecretInputs(formHost);
        toast('บันทึกการตั้งค่า ' + (provider.label || provider.provider) + ' แล้ว',
              'success');
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

  /* ── tests and OAuth ───────────────────────────────────── */

  function runProviderTest(key, live, button, statusNode) {
    button.disabled = true;
    setStatus(statusNode, live ? 'กำลังทดสอบกับผู้ให้บริการจริง…' : 'กำลังตรวจสอบ…', 'info');
    api('POST', '/api/providers/test', { provider: key, live: !!live })
      .then(function (result) {
        var kind = result.status === 'ready' ? 'success'
                 : result.status === 'error' ? 'error' : 'info';
        setStatus(statusNode,
                  result.detail || STATUS_TH[result.status] || result.status, kind);
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

  /* ── list ──────────────────────────────────────────────── */

  function loadProviders() {
    var host = $('#ac-providers');
    var empty = $('#ac-providers-empty');
    return AC.fetchProviders()
      .then(function (providers) {
        AC.settled(host);
        clear(host);
        if (!providers.length) {
          clear(empty);
          empty.hidden = false;
          empty.appendChild(icon('hourglass_empty', 'state-icon'));
          empty.appendChild(el('p', 'state-title', 'ยังไม่มีผู้ให้บริการในระบบ'));
          empty.appendChild(el('p', 'state-desc',
            'เซิร์ฟเวอร์ยังไม่ได้ประกาศรายการผู้ให้บริการใด ๆ'));
          return;
        }
        empty.hidden = true;
        providers.forEach(function (provider) {
          host.appendChild(providerCard(provider));
        });
        renderSummary(providers);
      })
      .catch(function (err) {
        AC.settled(host);
        clear(host);
        clear($('#ac-providers-summary'));
        AC.errorState(empty, 'โหลดข้อมูลผู้ให้บริการไม่สำเร็จ', err.message, loadProviders);
      });
  }

  function renderSummary(providers) {
    var host = $('#ac-providers-summary');
    if (!host) return;
    AC.settled(host);
    clear(host);
    var counts = { ready: 0, configured: 0, manual: 0, pending: 0, error: 0 };
    providers.forEach(function (p) {
      if (p.status === 'ready') counts.ready++;
      else if (p.status === 'configured') counts.configured++;
      else if (p.status === 'manual') counts.manual++;
      else if (p.status === 'error') counts.error++;
      else counts.pending++;
    });
    host.appendChild(AC.tile('พร้อมใช้งาน', 'check_circle', String(counts.ready),
                             STATUS_TH.ready));
    host.appendChild(AC.tile('ตั้งค่าแล้ว รอยืนยัน', 'pending', String(counts.configured),
                             'กด "ทดสอบ" เพื่อยืนยัน'));
    host.appendChild(AC.tile('ต้องทำเอง', 'front_hand', String(counts.manual),
                             'ไม่มีช่องทางเผยแพร่อัตโนมัติ'));
    host.appendChild(AC.tile('ยังไม่ได้ตั้งค่า', 'error', String(counts.pending + counts.error),
                             counts.error + ' รายการรายงานปัญหา'));
  }

  AC.page('providers', function () {
    AC.skeleton($('#ac-providers-summary'), 4, 'ac-tile ac-sk-card');
    AC.skeleton($('#ac-providers'), 3, 'ac-sk-card');
    AC.onRefresh(loadProviders);
    loadProviders();
  });
})();
