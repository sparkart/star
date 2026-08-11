/* ══════════════════════════════════════════════════════════
   Star — Automatic schedule (/automation/schedule/)

   One daily run, off by default, with no automatic retry. The form is the
   only place in the app that can turn unattended production on, so every
   guard that used to live in the monolith lives here and nowhere else:

     · the publish targets are whatever /api/automation/overview called
       automatable — never a list written into this file, so a platform the
       backend cannot post to can never be offered here;
     · a platform the backend calls manual (TikTok, Shopee) is drawn disabled
       and under a different checkbox name, so it can neither be ticked nor be
       read back by selectedValues('splatform');
     · asking for the publish stage without a target is refused before the
       request is sent, because the backend refuses it too.

   The form is repopulated from the response, not from what was submitted, so
   the page always shows what the server actually stored.
   ══════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  var AC = window.StarAC;
  if (!AC) return;

  var $ = AC.$, el = AC.el, icon = AC.icon, clear = AC.clear;
  var state = AC.state;
  var DAYS = AC.DAYS, STAGES = AC.STAGES;
  var selectedValues = AC.selectedValues, applySelection = AC.applySelection;
  var setStatus = AC.setStatus, toast = AC.toast, api = AC.api;

  /* ── publish targets ───────────────────────────────────── */

  /* The backend's own split of the platform list. Nothing here is hard coded:
     if the server stops automating a platform it stops being offered. */
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

    AC.settled(host);
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
      face.appendChild(el('span', null, AC.providerLabel(key)));
      wrap.appendChild(box);
      wrap.appendChild(face);
      host.appendChild(wrap);
    });

    meta.manual.forEach(function (key) {
      var label = AC.providerLabel(key);
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
      var manualNames = meta.manual.map(AC.providerLabel);
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

  /* ── load and save ─────────────────────────────────────── */

  function applyConfig(config) {
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
  }

  function loadSchedule() {
    var errorBox = $('#ac-sched-error');
    return api('GET', '/api/schedule')
      .then(function (config) {
        if (errorBox) errorBox.hidden = true;
        applyConfig(config);
        setStatus($('#ac-sched-status'), '', null);
      })
      .catch(function (err) {
        AC.errorState(errorBox, 'โหลดตารางเวลาไม่สำเร็จ', err.message, loadSchedule);
        setStatus($('#ac-sched-status'), 'โหลดตารางเวลาไม่สำเร็จ: ' + err.message, 'error');
      });
  }

  /* The target metadata lives on the overview payload, so the pills can only
     be drawn once that has landed. */
  function loadPlatformMeta() {
    var host = $('#ac-sched-platforms');
    AC.skeleton(host, 3, 'ac-sk-pill');
    return Promise.all([AC.fetchProviders(), AC.fetchOverview()])
      .then(function () { syncSchedulePlatforms(); })
      .catch(function (err) {
        AC.settled(host);
        clear(host);
        AC.errorState($('#ac-sched-platforms-error'),
                      'โหลดปลายทางที่ตั้งเวลาได้ไม่สำเร็จ', err.message, loadPlatformMeta);
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
        /* Reflect what was actually stored, not what was submitted. */
        applyConfig(config);
        toast('บันทึกตารางเวลาแล้ว', 'success');
        setStatus(statusNode, config.enabled
          ? ('เปิดใช้งานแล้ว — จะรันทุกวันเวลา ' + config.time + ' ตามเวลากรุงเทพฯ')
          : 'บันทึกแล้ว — ตารางเวลาปิดอยู่', 'success');
      })
      .catch(function (err) { setStatus(statusNode, err.message, 'error'); })
      .finally(function () { button.disabled = false; });
  }

  function load() {
    /* The stored config is applied after the metadata so the pills it ticks
       already exist. */
    return loadPlatformMeta().then(loadSchedule);
  }

  AC.page('schedule', function () {
    AC.buildPills($('#ac-sched-days'), DAYS, 'sday', null, false, updateScheduleWarning);
    AC.buildPills($('#ac-sched-stages'), STAGES, 'sstage', 'icon', true, updateScheduleWarning);
    $('#ac-sched-save').addEventListener('click', saveSchedule);
    AC.onRefresh(load);
    updateScheduleWarning();
    load();
  });
})();
