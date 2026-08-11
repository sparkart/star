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

  /* ══ Following one job ══════════════════════════════════ */

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
    $('#ac-cancel').addEventListener('click', cancelJob);
    $('#ac-retry').addEventListener('click', retryJob);
    AC.onRefresh(function () {
      loadJobs();
      if (state.job) schedulePoll(0);
    });
    load();
  });
})();
