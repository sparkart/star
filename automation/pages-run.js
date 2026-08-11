/* ══════════════════════════════════════════════════════════
   Star — Create a job (/automation/run/)

   Owns the run form and the plan preview, and nothing else. When the server
   accepts a job this page hands it over to /automation/jobs/?job=… rather
   than growing a second progress surface.

   Dry run is the default here and stays the default: the button only says
   "เริ่มงานจริง" after the operator has deliberately unticked it.
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
      bodyWrap.appendChild(el('p', 'ac-cost-text', runBlockReason()));
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
    var blocked = runBlockReason();
    if (blocked) {
      setStatus(statusNode, blocked, 'error');
      updateRunWarning();
      return;
    }
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
      })
      .finally(function () { button.disabled = false; });
  }

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

    AC.onRefresh(load);
    updateRunWarning();
    load();
  });
})();
