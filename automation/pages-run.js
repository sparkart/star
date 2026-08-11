/* ══════════════════════════════════════════════════════════
   Star — Create a job (/automation/run/)

   Owns the run form and the plan preview, and nothing else. When the server
   accepts a job this page hands it over to /automation/jobs/?job=… rather
   than growing a second progress surface.

   Dry run is the default here and stays the default: the button only says
   "เริ่มงานจริง" after the operator has deliberately unticked it.

   The background image follows the same rule. A chosen file stays in the
   browser until a real run is started: dry run never uploads, never sends
   background_asset_id, and says so on screen. A live run uploads the bytes
   first and only then creates the job, so a job can never reference an asset
   the server does not have.
   ══════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  var AC = window.StarAC;
  if (!AC) return;

  var $ = AC.$, el = AC.el, icon = AC.icon, clear = AC.clear;
  var state = AC.state;
  var DAYS = AC.DAYS, STAGES = AC.STAGES;
  var JOB_STATUS_TH = AC.JOB_STATUS_TH;
  var selectedValues = AC.selectedValues, applySelection = AC.applySelection;
  var setStatus = AC.setStatus, toast = AC.toast, api = AC.api;

  var UPLOAD_PATH = '/api/assets/background';
  /* Bigger than the JSON timeout in automation.js on purpose: this request
     carries up to 12 MiB, the others carry a few hundred bytes. */
  var UPLOAD_TIMEOUT = 60000;

  /* Mirrors star_assets: the same three types and the same ceiling, checked
     here only so an obvious mistake costs nothing. The server checks the
     bytes themselves and remains the authority. */
  var MAX_BG_BYTES = 12 * 1024 * 1024;
  var ACCEPTED_TYPES = ['image/jpeg', 'image/png', 'image/webp'];
  var TYPE_LABEL = { 'image/jpeg': 'JPEG', 'image/png': 'PNG', 'image/webp': 'WebP' };

  /* star_jobs.MAX_CUSTOM_OVERLAY_TEXT. */
  var MAX_OVERLAY = 220;

  /* star_api.INTENT_VALUE. api() sets this header itself; the raw upload
     below builds its own request, so it needs the value here too. */
  var INTENT_VALUE = 'automation-control';

  /* The chosen image, and — separately — what the server has already been
     given. `assetFile` is the exact File the stored id was made from, so a
     retry after a failed job creation reuses the upload while any new
     selection invalidates it. */
  var background = {
    file: null,
    url: null,
    dims: '',
    asset: null,
    assetFile: null
  };

  var submitting = false;
  var announceTimer = null;

  /* ── background image ──────────────────────────────────── */

  function fmtBytes(bytes) {
    if (typeof bytes !== 'number' || !isFinite(bytes) || bytes < 0) return '';
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(0) + ' KB';
    return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
  }

  /* The id is only usable while it still belongs to the file on screen. */
  function cachedAssetId() {
    if (!background.asset || !background.file) return null;
    if (background.assetFile !== background.file) return null;
    return background.asset.id || null;
  }

  function bgError(message) {
    var node = $('#ac-bg-error');
    if (node) node.textContent = message || '';
  }

  function describeSelection() {
    var parts = [];
    var file = background.file;
    if (!file) return '';
    parts.push(TYPE_LABEL[file.type] || file.type || 'ไม่ทราบชนิดไฟล์');
    var size = fmtBytes(file.size);
    if (size) parts.push(size);
    if (background.dims) parts.push(background.dims + ' พิกเซล');
    if (cachedAssetId()) parts.push('รหัสภาพ ' + cachedAssetId().slice(0, 8) + '…');
    return parts.join(' · ');
  }

  /* Every exit from a selection goes through here, so an object URL is never
     left behind and a stale asset id can never outlive the file it came
     from. */
  function releaseSelection() {
    if (background.url) {
      URL.revokeObjectURL(background.url);
      background.url = null;
    }
    background.file = null;
    background.dims = '';
    background.asset = null;
    background.assetFile = null;
    var input = $('#ac-bg-file');
    if (input) input.value = '';
    var thumb = $('#ac-bg-thumb');
    if (thumb) thumb.removeAttribute('src');
  }

  function rejectFile(file) {
    if (ACCEPTED_TYPES.indexOf(file.type) === -1) {
      return 'รองรับเฉพาะไฟล์ JPEG, PNG และ WebP เท่านั้น' +
        (file.type ? ' (ไฟล์ที่เลือกเป็น ' + file.type + ')' : ' — ไฟล์ที่เลือกไม่ระบุชนิด');
    }
    if (file.size > MAX_BG_BYTES) {
      return 'ไฟล์ใหญ่เกินไป ' + fmtBytes(file.size) + ' — ขนาดสูงสุดคือ 12 MiB';
    }
    if (!file.size) return 'ไฟล์ว่าง เลือกไฟล์ภาพอื่น';
    return null;
  }

  function selectFile(file) {
    if (!file) return;
    var problem = rejectFile(file);
    if (problem) {
      /* A bad drop must not silently discard a good earlier choice. */
      bgError(problem);
      var input = $('#ac-bg-file');
      if (input) input.value = '';
      return;
    }
    releaseSelection();
    bgError('');
    background.file = file;
    background.url = URL.createObjectURL(file);
    renderBackground();
  }

  function renderBackground() {
    var drop = $('#ac-bg-drop');
    var preview = $('#ac-bg-preview');
    if (!drop || !preview) return;

    if (!background.file) {
      drop.hidden = false;
      preview.hidden = true;
      setStatus($('#ac-bg-status'), '');
      updateRunWarning();
      return;
    }

    drop.hidden = true;
    preview.hidden = false;

    var thumb = $('#ac-bg-thumb');
    if (thumb && background.url && thumb.getAttribute('src') !== background.url) {
      thumb.src = background.url;
    }
    var name = $('#ac-bg-name');
    if (name) name.textContent = background.file.name || 'ภาพที่เลือกไว้';
    var meta = $('#ac-bg-meta');
    if (meta) meta.textContent = describeSelection();

    setStatus($('#ac-bg-status'),
      cachedAssetId()
        ? 'อัปโหลดภาพนี้ไว้แล้ว การสั่งงานอีกครั้งจะใช้รหัสเดิมโดยไม่อัปโหลดซ้ำ'
        : '',
      'success');

    updateRunWarning();
  }

  /* What happens to this image when the button is pressed, said next to the
     image itself rather than only in the summary at the bottom. */
  function updateBackgroundNote() {
    var note = $('#ac-bg-note');
    var dryRunBox = $('#ac-dry-run');
    if (!note || !dryRunBox) return;
    if (!background.file) {
      note.textContent = '';
      return;
    }
    if (dryRunBox.checked) {
      note.textContent = 'โหมดซ้อมจะไม่ส่งภาพนี้ขึ้นเซิร์ฟเวอร์ ' +
        'ภาพยังอยู่ในหน้านี้และยังเลือกไว้ให้ แต่งานซ้อมจะรันด้วยพื้นหลังมาตรฐาน ' +
        'ภาพจะถูกอัปโหลดเมื่อสั่งงานจริงเท่านั้น';
      return;
    }
    note.textContent = cachedAssetId()
      ? 'ภาพนี้อยู่บนเซิร์ฟเวอร์แล้ว งานที่สั่งต่อจากนี้จะอ้างถึงรหัสเดิมโดยไม่อัปโหลดซ้ำ'
      : 'เมื่อกด "เริ่มงานจริง" ระบบจะอัปโหลดภาพนี้ก่อน แล้วจึงส่งงานพร้อมรหัสภาพที่ได้กลับมา';
  }

  /* The one request on this page that is not JSON. It uses fetch directly —
     same origin, same credentials, same intent header, same error shape as
     api() — because the body is the file itself. */
  function uploadBackground(file) {
    var controller = new AbortController();
    var timer = setTimeout(function () { controller.abort(); }, UPLOAD_TIMEOUT);
    var headers = { Accept: 'application/json', 'Content-Type': file.type };
    headers[AC.INTENT_HEADER] = INTENT_VALUE;

    return fetch(UPLOAD_PATH, {
      method: 'POST',
      headers: headers,
      cache: 'no-store',
      credentials: 'same-origin',
      signal: controller.signal,
      body: file
    }).catch(function (err) {
      throw new AC.ApiError(
        err && err.name === 'AbortError'
          ? 'หมดเวลาอัปโหลดภาพ (' + (UPLOAD_TIMEOUT / 1000) + ' วินาที)'
          : 'เชื่อมต่อเซิร์ฟเวอร์ไม่ได้', 0);
    }).then(function (res) {
      var type = res.headers.get('content-type') || '';
      var asJson = type.indexOf('json') !== -1
        ? res.json().catch(function () { return null; })
        : Promise.resolve(null);
      return asJson.then(function (payload) {
        if (!res.ok) {
          var error = new AC.ApiError(
            (payload && payload.error) || ('เซิร์ฟเวอร์ตอบกลับ HTTP ' + res.status),
            res.status, payload && payload.field);
          error.payload = payload;
          throw error;
        }
        /* The route answers {ok, asset:{…}} today; a bare metadata object is
           accepted too, so a flattened response would not break the page. */
        var asset = (payload && payload.asset) || payload;
        if (!asset || typeof asset.id !== 'string' || !asset.id) {
          throw new AC.ApiError('เซิร์ฟเวอร์ไม่ได้ส่งรหัสภาพกลับมา', res.status);
        }
        return asset;
      });
    }).finally(function () { clearTimeout(timer); });
  }

  /* ── overlay text ──────────────────────────────────────── */

  function overlayMode() {
    var picked = document.querySelector('input[name="overlay_text_mode"]:checked');
    return (picked && picked.value) === 'custom' ? 'custom' : 'auto';
  }

  function overlayText() {
    var box = $('#ac-overlay-text');
    return box ? box.value.trim() : '';
  }

  function overlayError(message) {
    var node = $('#ac-overlay-error');
    var box = $('#ac-overlay-text');
    if (node) node.textContent = message || '';
    if (box) {
      if (message) box.setAttribute('aria-invalid', 'true');
      else box.removeAttribute('aria-invalid');
    }
  }

  /* The visible counter updates on every keystroke; the spoken one waits for
     a pause, so a screen reader is not read a number per character. */
  function updateCounter() {
    var box = $('#ac-overlay-text');
    var counter = $('#ac-overlay-count');
    if (!box || !counter) return;
    var used = box.value.length;
    counter.textContent = used + ' / ' + MAX_OVERLAY;
    counter.classList.toggle('is-full', used >= MAX_OVERLAY);

    if (announceTimer) clearTimeout(announceTimer);
    announceTimer = setTimeout(function () {
      var live = $('#ac-overlay-count-sr');
      if (live) {
        live.textContent = 'ใช้ไป ' + used + ' ตัวอักษร เหลืออีก ' +
          (MAX_OVERLAY - used) + ' ตัวอักษร';
      }
    }, 800);
  }

  /* Custom mode is the only mode where the textarea exists for the user or
     for the form: hidden and disabled otherwise, so it can never be tabbed
     into or read while it has no effect. */
  function renderOverlay() {
    var custom = overlayMode() === 'custom';
    var wrap = $('#ac-overlay-custom');
    var box = $('#ac-overlay-text');
    if (wrap) wrap.hidden = !custom;
    if (box) box.disabled = !custom;
    if (!custom) overlayError('');
    updateCounter();
    updateRunWarning();
  }

  /* ── plan summary ──────────────────────────────────────── */

  /* Publish targets are drawn from the provider list so the UI can never
     offer a platform the backend does not know about. */
  function syncPlatformAvailability() {
    var host = $('#ac-platforms');
    if (!host) return;
    var checked = selectedValues('platform');
    AC.settled(host);
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
  }

  /* Publish needs a target and the backend rejects the combination — say so
     before the request is sent, exactly as the schedule form does. */
  function runBlockReason() {
    var stages = selectedValues('stage');
    if (stages.indexOf('publish') === -1) return null;
    if (selectedValues('platform').length) return null;
    return 'เลือกขั้นตอน "เผยแพร่" ไว้แต่ยังไม่ได้เลือกปลายทาง เซิร์ฟเวอร์จะปฏิเสธคำขอนี้';
  }

  function updateRunWarning() {
    var dryRun = $('#ac-dry-run');
    var box = $('#ac-cost');
    var fromNode = $('#ac-from-date'), toNode = $('#ac-to-date');
    if (!box || !dryRun || !fromNode || !toNode) return;

    var stages = selectedValues('stage');
    var platforms = selectedValues('platform');
    var days = selectedValues('day');
    var from = fromNode.value;
    var to = toNode.value || from;

    var dateCount = 1;
    if (from && to) {
      var ms = Date.parse(to + 'T00:00:00Z') - Date.parse(from + 'T00:00:00Z');
      dateCount = isNaN(ms) ? 1 : Math.floor(ms / 86400000) + 1;
    }
    var units = Math.max(0, dateCount) * (days.length || 0);

    var publishNoTarget = runBlockReason() !== null;

    clear(box);
    box.className = 'ac-cost ' + (dryRun.checked ? 'is-dry' : 'is-live');
    box.appendChild(icon(dryRun.checked ? 'science' : 'warning'));
    var bodyWrap = el('div', 'ac-cost-body');

    if (dryRun.checked) {
      bodyWrap.appendChild(el('div', 'ac-cost-title', 'โหมดซ้อม (dry run) — ไม่มีการเรียกผู้ให้บริการ'));
      bodyWrap.appendChild(el('p', 'ac-cost-text',
        'ระบบจะตรวจเงื่อนไขและแสดงคำสั่งที่จะรันเท่านั้น ไม่เขียนไฟล์ลงโปรเจกต์ ' +
        'ไม่มีค่าใช้จ่าย และไม่เผยแพร่อะไรทั้งสิ้น'));
      if (background.file) {
        bodyWrap.appendChild(el('p', 'ac-cost-text',
          'ภาพพื้นหลังที่เลือกไว้จะไม่ถูกส่งขึ้นเซิร์ฟเวอร์ในโหมดซ้อม ' +
          'ภาพยังคงถูกเลือกไว้ในหน้านี้ และจะถูกอัปโหลดก็ต่อเมื่อสั่งงานจริง'));
      }
    } else {
      bodyWrap.appendChild(el('div', 'ac-cost-title', 'โหมดจริง — มีการเรียกใช้งานจริงและอาจมีค่าใช้จ่าย'));
      var parts = [];
      if (background.file) {
        parts.push(cachedAssetId()
          ? 'ใช้ภาพพื้นหลังที่อัปโหลดไว้แล้ว (ไม่อัปโหลดซ้ำ)'
          : 'อัปโหลดภาพพื้นหลัง 1 ไฟล์ก่อนสร้างงาน');
      }
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

    bodyWrap.appendChild(el('p', 'ac-cost-text',
      overlayMode() === 'custom'
        ? 'ข้อความบนวิดีโอ: ข้อความที่กำหนดเอง ใช้ชุดเดียวกันกับทุกคลิปในงานนี้'
        : 'ข้อความบนวิดีโอ: บทที่ระบบสร้างขึ้นจริงของแต่ละคลิป แยกกันทุกวันที่และทุกวันเกิด'));

    if (publishNoTarget) {
      bodyWrap.appendChild(el('p', 'ac-cost-text', runBlockReason()));
    }
    if (units > 0) {
      bodyWrap.appendChild(el('p', 'ac-cost-text',
        'ขอบเขต: ' + dateCount + ' วันปฏิทิน × ' + days.length + ' วันเกิด = ' + units + ' ชิ้นงาน'));
    }
    box.appendChild(bodyWrap);

    /* While a submit is in flight the button reports the stage it is on, so
       the mode label must not overwrite it. */
    var runBtn = $('#ac-run');
    if (runBtn && !submitting) {
      clear(runBtn);
      runBtn.appendChild(icon(dryRun.checked ? 'science' : 'play_arrow'));
      runBtn.appendChild(el('span', null, dryRun.checked ? 'เริ่มงานแบบซ้อม' : 'เริ่มงานจริง'));
    }

    updateBackgroundNote();
  }

  /* ── submit ────────────────────────────────────────────── */

  /* One in-flight submit at a time, and every control that could change what
     is being sent is locked for its duration. */
  function setSubmitting(on, stage) {
    submitting = on;
    var button = $('#ac-run');
    if (button) {
      button.disabled = on;
      if (on) {
        button.setAttribute('aria-busy', 'true');
        clear(button);
        button.appendChild(icon('progress_activity', 'ac-spin'));
        button.appendChild(el('span', null,
          stage === 'upload' ? 'กำลังอัปโหลดภาพ…' : 'กำลังส่งงาน…'));
      } else {
        button.removeAttribute('aria-busy');
      }
    }
    ['#ac-bg-pick', '#ac-bg-change', '#ac-bg-remove', '#ac-bg-file']
      .forEach(function (sel) {
        var node = $(sel);
        if (node) node.disabled = on;
      });
    var drop = $('#ac-bg-drop');
    if (drop) drop.classList.toggle('is-locked', on);
    if (!on) updateRunWarning();
  }

  function buildBody(dryRun, mode, text, assetId) {
    var body = {
      from_date: $('#ac-from-date').value,
      to_date: $('#ac-to-date').value || $('#ac-from-date').value,
      days: selectedValues('day'),
      stages: selectedValues('stage'),
      dry_run: dryRun,
      force: $('#ac-force').checked,
      overlay_text_mode: mode
    };
    /* The backend rejects custom_overlay_text outside custom mode, and it is
       meaningless there anyway. */
    if (mode === 'custom') body.custom_overlay_text = text;
    var platforms = selectedValues('platform');
    if (platforms.length) body.platforms = platforms;
    /* Dry run never carries an asset id, even when one is already cached. */
    if (!dryRun && assetId) body.background_asset_id = assetId;
    return body;
  }

  function submitJob() {
    if (submitting) return;
    var statusNode = $('#ac-run-status');

    var blocked = runBlockReason();
    if (blocked) {
      setStatus(statusNode, blocked, 'error');
      updateRunWarning();
      return;
    }

    /* Everything that can be judged locally is judged before the first
       request goes out. */
    var mode = overlayMode();
    var text = overlayText();
    if (mode === 'custom') {
      if (!text) {
        overlayError('พิมพ์ข้อความที่จะแสดงบนคลิปก่อน หรือกลับไปเลือกโหมดอัตโนมัติ');
        setStatus(statusNode, 'โหมดกำหนดเองยังไม่มีข้อความ ยังไม่ได้ส่งอะไรไปที่เซิร์ฟเวอร์', 'error');
        var box = $('#ac-overlay-text');
        if (box) box.focus();
        return;
      }
      if (text.length > MAX_OVERLAY) {
        overlayError('ข้อความยาว ' + text.length + ' ตัวอักษร เกินขีดจำกัด ' + MAX_OVERLAY);
        setStatus(statusNode, 'ข้อความบนวิดีโอยาวเกินกำหนด ยังไม่ได้ส่งอะไรไปที่เซิร์ฟเวอร์', 'error');
        return;
      }
      overlayError('');
    }

    var dryRun = $('#ac-dry-run').checked;
    var needsUpload = !dryRun && !!background.file && !cachedAssetId();

    setSubmitting(true, needsUpload ? 'upload' : 'job');
    setStatus(statusNode,
      needsUpload ? 'ขั้นที่ 1 จาก 2 — กำลังอัปโหลดภาพพื้นหลัง…' : 'กำลังส่งงานเข้าคิว…',
      'info');

    var ready = needsUpload
      ? uploadBackground(background.file).then(function (asset) {
          /* Cached against the exact File, so retrying a failed job creation
             reuses these bytes instead of sending them again. */
          background.asset = asset;
          background.assetFile = background.file;
          if (asset.width && asset.height) {
            background.dims = asset.width + '×' + asset.height;
          }
          renderBackground();
          setSubmitting(true, 'job');
          setStatus(statusNode, 'ขั้นที่ 2 จาก 2 — อัปโหลดภาพแล้ว กำลังส่งงานเข้าคิว…', 'info');
          return asset.id;
        }, function (err) {
          bgError('อัปโหลดภาพพื้นหลังไม่สำเร็จ: ' + err.message);
          err.message = 'อัปโหลดภาพพื้นหลังไม่สำเร็จ จึงยังไม่ได้สร้างงาน: ' + err.message;
          throw err;
        })
      : Promise.resolve(dryRun ? null : cachedAssetId());

    ready
      .then(function (assetId) {
        return api('POST', '/api/jobs', buildBody(dryRun, mode, text, assetId));
      })
      .then(function (job) {
        state.job = job;
        toast('สร้างงานแล้ว', 'success');
        setStatus(statusNode, 'สร้างงานแล้ว — ติดตามความคืบหน้าและบันทึกได้ที่หน้างาน', 'success');
        AC.statusLink(statusNode, AC.jobHref(job.id), 'เปิดหน้างาน');
      })
      .catch(function (err) {
        if (err.status === 409 && err.payload && err.payload.active_job) {
          var active = err.payload.active_job;
          setStatus(statusNode,
            'มีงานทำงานอยู่แล้ว (' + (JOB_STATUS_TH[active.status] || active.status) +
            ') — ยกเลิกงานเดิมก่อนจึงจะเริ่มงานใหม่ได้', 'error');
          AC.statusLink(statusNode, AC.jobHref(active.id), 'เปิดงานที่ทำงานอยู่');
        } else {
          setStatus(statusNode, err.message, 'error');
        }
        /* The uploaded asset is deliberately kept: the image reached the
           server, only the job did not, so a retry must not send it twice. */
      })
      .finally(function () { setSubmitting(false); });
  }

  /* ── wiring ────────────────────────────────────────────── */

  function wireBackground() {
    var input = $('#ac-bg-file');
    var drop = $('#ac-bg-drop');
    var pick = $('#ac-bg-pick');
    if (!input || !drop) return;

    function openPicker() {
      if (submitting) return;
      input.click();
    }

    if (pick) pick.addEventListener('click', openPicker);
    var change = $('#ac-bg-change');
    if (change) change.addEventListener('click', openPicker);

    var remove = $('#ac-bg-remove');
    if (remove) {
      remove.addEventListener('click', function () {
        if (submitting) return;
        releaseSelection();
        bgError('');
        renderBackground();
        if (pick) pick.focus();
      });
    }

    input.addEventListener('change', function () {
      var file = input.files && input.files[0];
      if (file) selectFile(file);
    });

    /* Dimensions come from the decoded preview, so the metadata line is real
       rather than a guess from the file name. */
    var thumb = $('#ac-bg-thumb');
    if (thumb) {
      thumb.addEventListener('load', function () {
        if (!background.file) return;
        background.dims = thumb.naturalWidth + '×' + thumb.naturalHeight;
        var meta = $('#ac-bg-meta');
        if (meta) meta.textContent = describeSelection();
      });
      thumb.addEventListener('error', function () {
        if (background.file) bgError('เปิดไฟล์ภาพนี้ไม่ได้ ลองเลือกไฟล์อื่น');
      });
    }

    ['dragenter', 'dragover'].forEach(function (name) {
      drop.addEventListener(name, function (event) {
        if (submitting) return;
        event.preventDefault();
        if (event.dataTransfer) event.dataTransfer.dropEffect = 'copy';
        drop.classList.add('is-dragover');
      });
    });
    ['dragleave', 'dragend'].forEach(function (name) {
      drop.addEventListener(name, function (event) {
        /* Moving over a child fires dragleave on the zone; only a pointer
           that has actually left it should clear the highlight. */
        if (name === 'dragleave' && event.relatedTarget &&
            drop.contains(event.relatedTarget)) return;
        drop.classList.remove('is-dragover');
      });
    });
    drop.addEventListener('drop', function (event) {
      event.preventDefault();
      drop.classList.remove('is-dragover');
      if (submitting) return;
      var files = event.dataTransfer && event.dataTransfer.files;
      if (files && files.length) selectFile(files[0]);
    });

    /* An object URL outlives the document unless it is handed back. */
    window.addEventListener('pagehide', function () {
      if (background.url) URL.revokeObjectURL(background.url);
    });
  }

  function wireOverlay() {
    AC.$$('input[name="overlay_text_mode"]').forEach(function (radio) {
      radio.addEventListener('change', function () {
        renderOverlay();
        if (radio.value === 'custom' && radio.checked) {
          var box = $('#ac-overlay-text');
          if (box) box.focus();
        }
      });
    });
    var box = $('#ac-overlay-text');
    if (box) {
      box.addEventListener('input', function () {
        updateCounter();
        if (box.value.trim()) overlayError('');
      });
    }
  }

  /* ── data ──────────────────────────────────────────────── */

  function loadPlatforms() {
    var host = $('#ac-platforms');
    var errorBox = $('#ac-platforms-error');
    AC.skeleton(host, 4, 'ac-sk-pill');
    return AC.fetchProviders()
      .then(function () {
        if (errorBox) errorBox.hidden = true;
        syncPlatformAvailability();
      })
      .catch(function (err) {
        AC.settled(host);
        clear(host);
        AC.errorState(errorBox, 'โหลดปลายทางการเผยแพร่ไม่สำเร็จ', err.message, loadPlatforms);
      });
  }

  function loadLimits() {
    var note = $('#ac-range-note');
    return AC.fetchOverview()
      .then(function (data) {
        if (note && data.limits) {
          note.textContent = 'ช่วงวันสูงสุด ' + data.limits.max_range_days +
            ' วันต่อหนึ่งงาน และรันได้ครั้งละ ' + data.limits.max_concurrent_jobs + ' งาน';
        }
        /* A job already running means this form cannot start another one;
           say so where the operator is about to press the button. */
        var busy = $('#ac-run-busy');
        if (!busy) return;
        clear(busy);
        if (data.active_job) {
          busy.hidden = false;
          var note2 = AC.callout('sync', 'มีงานทำงานอยู่ตอนนี้',
            'เซิร์ฟเวอร์รับงานพร้อมกันได้จำกัด งานใหม่จะถูกปฏิเสธจนกว่างานเดิมจะจบหรือถูกยกเลิก',
            'callout-info');
          var link = el('a', 'ac-inline-link', 'เปิดงานที่ทำงานอยู่');
          link.href = AC.jobHref(data.active_job.id);
          note2.querySelector('.callout-body').appendChild(link);
          busy.appendChild(note2);
        } else {
          busy.hidden = true;
        }
      })
      .catch(function (err) {
        if (note) note.textContent = 'อ่านข้อจำกัดของเซิร์ฟเวอร์ไม่ได้: ' + err.message;
      });
  }

  function load() {
    return Promise.all([loadLimits(), loadPlatforms()]);
  }

  AC.page('run', function () {
    AC.buildPills($('#ac-days'), DAYS, 'day', null, false, updateRunWarning);
    AC.buildPills($('#ac-stages'), STAGES, 'stage', 'icon', true, updateRunWarning);

    /* Sensible, safe defaults: today, every birth-day, no publish stage. */
    var today = AC.todayISO();
    $('#ac-from-date').value = today;
    $('#ac-to-date').value = today;
    applySelection('day', DAYS.map(function (d) { return d.key; }));
    applySelection('stage', ['astro', 'script', 'audio', 'video']);

    $('#ac-dry-run').addEventListener('change', updateRunWarning);
    $('#ac-force').addEventListener('change', updateRunWarning);
    $('#ac-from-date').addEventListener('change', updateRunWarning);
    $('#ac-to-date').addEventListener('change', updateRunWarning);
    $('#ac-run').addEventListener('click', submitJob);

    $('#ac-select-all-days').addEventListener('click', function () {
      applySelection('day', DAYS.map(function (d) { return d.key; }));
      updateRunWarning();
    });
    $('#ac-clear-days').addEventListener('click', function () {
      applySelection('day', []);
      updateRunWarning();
    });

    wireBackground();
    wireOverlay();

    AC.onRefresh(load);
    renderBackground();
    renderOverlay();
    updateRunWarning();
    load();
  });
})();
