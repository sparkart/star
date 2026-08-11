/* ══════════════════════════════════════════════════════════
   Star — Jobs: queue, history, detail, events (/automation/jobs/)

   Everything about a job after it exists: the list, the one being followed,
   its event stream, and the two manual controls — cancel and retry. Nothing
   here retries by itself; a retry is always an operator's deliberate act, so
   a failing pipeline can never spin into a paid loop.

   The page can be deep-linked as /automation/jobs/?job=<id>, which is how the
   run page hands work over.
   ══════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  var AC = window.StarAC;
  if (!AC) return;

  var $ = AC.$, el = AC.el, icon = AC.icon, clear = AC.clear;
  var state = AC.state;
  var STAGE_LABEL = AC.STAGE_LABEL;
  var JOB_STATUS_TH = AC.JOB_STATUS_TH, JOB_STATUS_ICON = AC.JOB_STATUS_ICON;
  var setStatus = AC.setStatus, toast = AC.toast, api = AC.api;
  var previewReturnFocus = null;
  var previewAbort = null;

  var DAY_LABEL = {
    sun: 'อาทิตย์', mon: 'จันทร์', tue: 'อังคาร', wed: 'พุธ',
    thu: 'พฤหัสบดี', fri: 'ศุกร์', sat: 'เสาร์'
  };
  var ARTIFACT_LABEL = {
    image: 'ภาพ', video: 'วิดีโอ', audio: 'เสียง', text: 'ข้อความ', json: 'ข้อมูล JSON'
  };
  var ARTIFACT_ICON = {
    image: 'image', video: 'movie', audio: 'graphic_eq',
    text: 'description', json: 'data_object'
  };

  /* ══ Following one job ══════════════════════════════════ */

  function followJob(job) {
    closePreview();
    state.job = job;
    state.lastEventId = 0;
    clear($('#ac-log'));
    renderJob(job);
    schedulePoll(0);
  }

  /* ══ Generated artifacts ════════════════════════════════ */

  /* The backend mints this exact job-scoped shape. Do not accept a URL merely
     because URL() says it is same-origin: a different API path is not an
     artifact capability, and absolute/data/javascript URLs never belong here. */
  function isSafeArtifactUrl(value, jobId) {
    if (typeof value !== 'string' || typeof jobId !== 'string' ||
        !/^[0-9a-f]{32}$/.test(jobId)) return false;
    var prefix = '/api/jobs/' + jobId + '/artifacts/';
    if (value.indexOf(prefix) !== 0 ||
        !/^(0|[1-9][0-9]{0,2})$/.test(value.slice(prefix.length))) return false;
    try {
      var parsed = new URL(value, window.location.origin);
      return parsed.origin === window.location.origin &&
        parsed.pathname === value && !parsed.search && !parsed.hash;
    } catch (err) {
      return false;
    }
  }

  function extractArtifacts(job) {
    var result = job && job.result;
    if (!result || result.dry_run || !Array.isArray(result.artifacts)) return [];
    return result.artifacts.filter(function (artifact) {
      return artifact && ARTIFACT_LABEL[artifact.kind] &&
        isSafeArtifactUrl(artifact.url, job.id);
    });
  }

  function formatBytes(value) {
    if (typeof value !== 'number' || value < 0 || !isFinite(value)) return '';
    if (value < 1024) return value + ' ไบต์';
    if (value < 1024 * 1024) return (value / 1024).toFixed(value < 10240 ? 1 : 0) + ' KB';
    return (value / (1024 * 1024)).toFixed(value < 10 * 1024 * 1024 ? 1 : 0) + ' MB';
  }

  function artifactTitle(artifact) {
    return (typeof artifact.name === 'string' && artifact.name) ||
      (ARTIFACT_LABEL[artifact.kind] || 'ไฟล์ที่สร้าง');
  }

  function artifactMeta(artifact) {
    var parts = [];
    if (artifact.date) parts.push(artifact.date);
    if (artifact.day) parts.push('วัน' + (DAY_LABEL[artifact.day] || artifact.day));
    var size = formatBytes(artifact.bytes);
    if (size) parts.push(size);
    return parts.join(' · ') || (artifact.content_type || ARTIFACT_LABEL[artifact.kind]);
  }

  function mediaFallback(host, artifact) {
    host.classList.remove('sk', 'is-loading');
    host.classList.add('is-unavailable');
    clear(host);
    host.appendChild(icon('broken_image'));
    host.appendChild(el('span', null, 'โหลดภาพตัวอย่างไม่ได้'));
    host.setAttribute('aria-label', 'เปิดตัวอย่าง ' + artifactTitle(artifact));
  }

  function artifactVisual(artifact, onOpen) {
    var button = el('button', 'ac-artifact-visual is-' + artifact.kind);
    button.type = 'button';
    button.setAttribute('aria-label', 'เปิดตัวอย่าง ' + artifactTitle(artifact));
    button.addEventListener('click', onOpen);

    if (artifact.kind === 'image') {
      button.classList.add('sk', 'is-loading');
      var imageNode = document.createElement('img');
      imageNode.alt = '';
      imageNode.loading = 'lazy';
      imageNode.decoding = 'async';
      imageNode.src = artifact.url;
      imageNode.addEventListener('load', function () {
        button.classList.remove('sk', 'is-loading');
        button.classList.add('is-ready');
      });
      imageNode.addEventListener('error', function () { mediaFallback(button, artifact); });
      button.appendChild(imageNode);
      button.appendChild(icon('zoom_in', 'ac-artifact-cue'));
      return button;
    }

    if (artifact.kind === 'video') {
      button.classList.add('sk', 'is-loading');
      var videoNode = document.createElement('video');
      videoNode.preload = 'metadata';
      videoNode.muted = true;
      videoNode.playsInline = true;
      videoNode.setAttribute('aria-hidden', 'true');
      videoNode.src = artifact.url;
      videoNode.addEventListener('loadeddata', function () {
        button.classList.remove('sk', 'is-loading');
        button.classList.add('is-ready');
      });
      videoNode.addEventListener('error', function () { mediaFallback(button, artifact); });
      button.appendChild(videoNode);
      button.appendChild(icon('play_arrow', 'ac-artifact-cue'));
      return button;
    }

    button.classList.add('is-document');
    button.appendChild(icon(ARTIFACT_ICON[artifact.kind] || 'draft'));
    button.appendChild(el('span', 'ac-artifact-document-label',
      artifact.kind === 'json' ? '{ JSON }' : 'TXT'));
    button.appendChild(icon('visibility', 'ac-artifact-cue'));
    return button;
  }

  function artifactCard(artifact) {
    var card = el('article', 'ac-artifact-card is-' + artifact.kind);
    var open = function () { openPreview(artifact, document.activeElement); };

    if (artifact.kind === 'audio') {
      var audioWrap = el('div', 'ac-artifact-audio');
      audioWrap.appendChild(icon(ARTIFACT_ICON.audio));
      var audioNode = document.createElement('audio');
      audioNode.controls = true;
      audioNode.preload = 'metadata';
      audioNode.src = artifact.url;
      audioNode.setAttribute('aria-label', 'ฟัง ' + artifactTitle(artifact));
      audioWrap.appendChild(audioNode);
      card.appendChild(audioWrap);
    } else {
      card.appendChild(artifactVisual(artifact, open));
    }

    var info = el('div', 'ac-artifact-info');
    var titleRow = el('div', 'ac-artifact-title-row');
    titleRow.appendChild(el('h4', 'ac-artifact-title', artifactTitle(artifact)));
    titleRow.appendChild(el('span', 'chip ac-artifact-kind', ARTIFACT_LABEL[artifact.kind]));
    info.appendChild(titleRow);
    info.appendChild(el('p', 'ac-artifact-meta', artifactMeta(artifact)));

    var preview = el('button', 'btn btn-sm btn-quiet ac-artifact-open');
    preview.type = 'button';
    preview.appendChild(icon('visibility'));
    preview.appendChild(el('span', null, 'ดูตัวอย่าง'));
    preview.setAttribute('aria-label', 'ดูตัวอย่าง ' + artifactTitle(artifact));
    preview.addEventListener('click', open);
    info.appendChild(preview);
    card.appendChild(info);

    /* The quiet area of a card is clickable too; native controls and buttons
       keep their own behaviour and never trigger the dialog twice. */
    card.addEventListener('click', function (event) {
      if (event.target.closest('button, audio, video, a')) return;
      openPreview(artifact, card.querySelector('.ac-artifact-open'));
    });
    return card;
  }

  function renderArtifacts(host, job) {
    var artifacts = extractArtifacts(job);
    var section = el('section', 'ac-artifacts');
    var head = el('div', 'ac-artifacts-head');
    head.appendChild(el('h3', null, 'ไฟล์ที่สร้างจากงานนี้'));
    if (artifacts.length) head.appendChild(el('span', 'chip', artifacts.length + ' ไฟล์'));
    section.appendChild(head);

    if (!artifacts.length) {
      var empty = el('div', 'ac-artifact-empty');
      empty.appendChild(icon('inventory_2'));
      var copy = el('div');
      copy.appendChild(el('p', 'ac-artifact-empty-title', 'งานนี้ไม่มีไฟล์ตัวอย่างที่เปิดได้'));
      copy.appendChild(el('p', 'ac-artifact-empty-desc',
        'ผลสรุปของแต่ละขั้นตอนยังแสดงอยู่ด้านล่าง หากไฟล์ถูกย้ายหรือลบ ระบบจะไม่สร้างลิงก์ให้'));
      empty.appendChild(copy);
      section.appendChild(empty);
      host.appendChild(section);
      return;
    }

    var grid = el('div', 'ac-artifact-grid');
    artifacts.forEach(function (artifact) { grid.appendChild(artifactCard(artifact)); });
    section.appendChild(grid);
    host.appendChild(section);
  }

  /* ══ Accessible preview dialog ══════════════════════════ */

  function previewFailure(message) {
    var body = $('#ac-preview-body');
    clear(body);
    body.appendChild(AC.callout('error', 'เปิดตัวอย่างไม่ได้', message, 'callout-warn'));
    body.removeAttribute('aria-busy');
    toast(message, 'error');
  }

  function previewText(artifact) {
    var body = $('#ac-preview-body');
    body.setAttribute('aria-busy', 'true');
    var loading = el('div', 'ac-preview-loading');
    loading.appendChild(el('span', 'sk ac-preview-sk-line'));
    loading.appendChild(el('span', 'sk ac-preview-sk-line is-short'));
    loading.appendChild(el('span', 'sk ac-preview-sk-line'));
    body.appendChild(loading);

    previewAbort = new AbortController();
    fetch(artifact.url, {
      method: 'GET', cache: 'no-store', credentials: 'same-origin',
      headers: { Accept: artifact.kind === 'json' ? 'application/json' : 'text/plain' },
      signal: previewAbort.signal
    }).then(function (response) {
      if (!response.ok) throw new Error('เซิร์ฟเวอร์ตอบกลับ HTTP ' + response.status);
      return response.text();
    }).then(function (text) {
      if (artifact.kind === 'json') {
        try { text = JSON.stringify(JSON.parse(text), null, 2); } catch (err) { /* keep source */ }
      }
      clear(body);
      var pre = el('pre', 'ac-preview-text');
      pre.textContent = text || 'ไฟล์นี้ไม่มีข้อความ';
      body.appendChild(pre);
      body.removeAttribute('aria-busy');
    }).catch(function (err) {
      if (err && err.name === 'AbortError') return;
      previewFailure('โหลดไฟล์ไม่สำเร็จ ลองปิดแล้วเปิดตัวอย่างอีกครั้ง');
    }).finally(function () { previewAbort = null; });
  }

  function openPreview(artifact, trigger) {
    var dialog = $('#ac-preview-dialog');
    if (!dialog || !state.job || !isSafeArtifactUrl(artifact.url, state.job.id)) {
      toast('ลิงก์ไฟล์นี้ไม่ผ่านการตรวจสอบ จึงไม่เปิดตัวอย่าง', 'error');
      return;
    }
    if (!ARTIFACT_LABEL[artifact.kind]) {
      toast('ไฟล์ชนิดนี้ยังไม่มีตัวแสดงตัวอย่าง', 'error');
      return;
    }
    if (dialog.open) closePreview();
    previewReturnFocus = trigger || document.activeElement;

    $('#ac-preview-title').textContent = artifactTitle(artifact);
    $('#ac-preview-meta').textContent = ARTIFACT_LABEL[artifact.kind] + ' · ' + artifactMeta(artifact);
    var original = $('#ac-preview-original');
    original.href = artifact.url;
    original.setAttribute('aria-label', 'เปิดไฟล์ต้นฉบับ ' + artifactTitle(artifact));

    var body = $('#ac-preview-body');
    clear(body);
    body.className = 'ac-preview-body is-' + artifact.kind;
    body.removeAttribute('aria-busy');

    if (artifact.kind === 'image') {
      var imageNode = document.createElement('img');
      imageNode.alt = 'ตัวอย่าง ' + artifactTitle(artifact);
      imageNode.src = artifact.url;
      imageNode.addEventListener('error', function () {
        previewFailure('โหลดภาพไม่สำเร็จ ไฟล์อาจถูกย้ายหรือลบแล้ว');
      }, { once: true });
      body.appendChild(imageNode);
    } else if (artifact.kind === 'video') {
      var videoNode = document.createElement('video');
      videoNode.controls = true;
      videoNode.preload = 'metadata';
      videoNode.playsInline = true;
      videoNode.src = artifact.url;
      videoNode.setAttribute('aria-label', 'ตัวอย่าง ' + artifactTitle(artifact));
      videoNode.addEventListener('error', function () {
        previewFailure('โหลดวิดีโอไม่สำเร็จ ไฟล์อาจถูกย้ายหรือลบแล้ว');
      }, { once: true });
      body.appendChild(videoNode);
    } else if (artifact.kind === 'audio') {
      var audioNode = document.createElement('audio');
      audioNode.controls = true;
      audioNode.preload = 'metadata';
      audioNode.src = artifact.url;
      audioNode.setAttribute('aria-label', 'ตัวอย่าง ' + artifactTitle(artifact));
      audioNode.addEventListener('error', function () {
        previewFailure('โหลดเสียงไม่สำเร็จ ไฟล์อาจถูกย้ายหรือลบแล้ว');
      }, { once: true });
      body.appendChild(audioNode);
    } else {
      previewText(artifact);
    }

    if (typeof dialog.showModal !== 'function') {
      toast('เบราว์เซอร์นี้ไม่รองรับหน้าต่างตัวอย่าง', 'error');
      return;
    }
    dialog.showModal();
    window.requestAnimationFrame(function () { $('#ac-preview-close').focus(); });
  }

  function closePreview() {
    var dialog = $('#ac-preview-dialog');
    if (!dialog || !dialog.open) return;
    if (previewAbort) {
      previewAbort.abort();
      previewAbort = null;
    }
    AC.$$('audio, video', dialog).forEach(function (media) { media.pause(); });
    dialog.close();
  }

  function trapPreviewFocus(event) {
    if (event.key !== 'Tab') return;
    var dialog = $('#ac-preview-dialog');
    var focusable = AC.$$('button:not([disabled]), a[href], audio[controls], video[controls], ' +
      '[tabindex]:not([tabindex="-1"])', dialog).filter(function (node) {
        return !node.hidden;
      });
    if (!focusable.length) {
      event.preventDefault();
      dialog.focus();
      return;
    }
    var first = focusable[0], last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  function wirePreviewDialog() {
    var dialog = $('#ac-preview-dialog');
    $('#ac-preview-close').addEventListener('click', closePreview);
    dialog.addEventListener('click', function (event) {
      if (event.target === dialog) closePreview();
    });
    dialog.addEventListener('keydown', function (event) {
      if (event.key === 'Escape') {
        event.preventDefault();
        closePreview();
        return;
      }
      trapPreviewFocus(event);
    });
    dialog.addEventListener('cancel', function (event) {
      event.preventDefault();
      closePreview();
    });
    dialog.addEventListener('close', function () {
      clear($('#ac-preview-body'));
      $('#ac-preview-original').removeAttribute('href');
      if (previewReturnFocus && previewReturnFocus.isConnected) previewReturnFocus.focus();
      previewReturnFocus = null;
    });
  }

  function schedulePoll(delay) {
    if (state.pollTimer) clearTimeout(state.pollTimer);
    if (!state.job) return;
    state.pollTimer = setTimeout(pollJob, delay == null ? AC.POLL_ACTIVE : delay);
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
          schedulePoll(AC.POLL_ACTIVE);
        } else {
          state.pollTimer = null;
          loadJobs();
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
    $('#ac-job-scope').textContent = AC.jobScopeText(job);

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
        var box = AC.callout('report', 'เงื่อนไขที่ยังไม่ครบ',
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

    /* Production result: artifacts lead; raw stage details remain available as
       the exact audit record beneath them. */
    renderArtifacts(host, job);
    if ((result.stages || []).length) {
      host.appendChild(el('h3', 'ac-result-subhead', 'สรุปผลแต่ละขั้นตอน'));
    }
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
          row.appendChild(AC.chip(outcome.published ? 'check_circle' : 'front_hand',
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
    var placeholder = AC.$('.ac-log-empty', log);
    if (placeholder) placeholder.remove();

    events.forEach(function (event) {
      if (event.id > state.lastEventId) state.lastEventId = event.id;
      var line = el('div', 'ac-log-line' +
        (event.level === 'error' ? ' is-error' : event.level === 'warn' ? ' is-warn' : ''));
      line.appendChild(el('span', 'ac-log-time', AC.fmtClock(event.ts)));
      line.appendChild(el('span', 'ac-log-text',
        (event.stage ? '[' + event.stage + '] ' : '') + event.message));
      log.appendChild(line);
    });

    while (log.childNodes.length > AC.MAX_LOG_LINES) log.removeChild(log.firstChild);
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

  /* ══ Queue and history ══════════════════════════════════ */

  function loadJobs() {
    var host = $('#ac-jobs');
    var empty = $('#ac-jobs-empty');
    return api('GET', '/api/jobs?limit=20')
      .then(function (data) {
        var jobs = data.jobs || [];
        AC.settled(host);
        clear(host);
        if (!jobs.length) {
          empty.hidden = false;
          clear(empty);
          empty.appendChild(icon('inbox', 'state-icon'));
          empty.appendChild(el('p', 'state-title', 'ยังไม่มีงานในระบบ'));
          empty.appendChild(el('p', 'state-desc',
            'งานที่สั่งจากหน้าสั่งรันและจากตารางเวลาจะปรากฏที่นี่'));
          var create = el('a', 'btn btn-sm');
          create.href = AC.PATHS.run;
          create.appendChild(icon('add'));
          create.appendChild(el('span', null, 'ไปหน้าสั่งรันไปป์ไลน์'));
          empty.appendChild(create);
          return;
        }
        empty.hidden = true;
        jobs.forEach(function (job) { host.appendChild(jobRow(job)); });
      })
      .catch(function (err) {
        AC.settled(host);
        clear(host);
        AC.errorState(empty, 'โหลดประวัติงานไม่สำเร็จ', err.message, loadJobs);
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
    var meta = AC.fmtTime(job.created_at) +
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

  /* ══ Entry ══════════════════════════════════════════════ */

  /* Adopt a job on load: an explicit ?job=<id> first, otherwise whatever the
     server says is running, so a reload never loses the thread. */
  function adoptJob() {
    var wanted = AC.queryParam('job');
    if (wanted) {
      return api('GET', '/api/jobs/' + encodeURIComponent(wanted) + '?events=200')
        .then(function (job) {
          followJob(job);
          appendEvents(job.events || []);
        })
        .catch(function (err) {
          setStatus($('#ac-progress-status'),
                    'เปิดงานที่ระบุไม่ได้: ' + err.message, 'error');
        });
    }
    return AC.fetchOverview()
      .then(function (data) {
        if (data.active_job && !state.job) followJob(data.active_job);
      })
      .catch(function () { /* the history list already reports transport errors */ });
  }

  function load() {
    return Promise.all([loadJobs(), adoptJob()]);
  }

  AC.page('jobs', function () {
    AC.skeleton($('#ac-jobs'), 3, 'ac-sk-row');
    wirePreviewDialog();
    $('#ac-cancel').addEventListener('click', cancelJob);
    $('#ac-retry').addEventListener('click', retryJob);
    AC.onRefresh(function () {
      loadJobs();
      if (state.job) schedulePoll(0);
    });
    load();
  });
})();
