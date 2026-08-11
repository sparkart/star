/* ══════════════════════════════════════════════════════════
   Star — Automation dashboard (/automation/)

   A summary and a set of doors, nothing more: every control that changes
   something lives on one of the five other pages. This module reads
   /api/automation/overview and renders tiles, the operational warnings the
   API reported, and a read-only view of the job that is running right now.
   ══════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  var AC = window.StarAC;
  if (!AC) return;

  var $ = AC.$, el = AC.el, icon = AC.icon, clear = AC.clear;
  var state = AC.state;
  var JOB_STATUS_TH = AC.JOB_STATUS_TH;

  function renderOverview(data) {
    var host = $('#ac-overview');
    if (!host) return;
    AC.settled(host);
    clear(host);

    var schedule = data.schedule || {};
    var active = data.active_job;

    host.appendChild(AC.tile(
      'ผู้ให้บริการพร้อมใช้', 'hub',
      String(data.providers_ready != null ? data.providers_ready : '—'),
      (data.providers_pending || 0) + ' รายการยังต้องตั้งค่า'));

    host.appendChild(AC.tile(
      'งานที่กำลังทำงาน', 'sync',
      active ? (JOB_STATUS_TH[active.status] || active.status) : 'ไม่มี',
      active ? ('ความคืบหน้า ' + (active.progress || 0) + '%') : 'คิวว่าง',
      'is-sm'));

    host.appendChild(AC.tile(
      'ตารางเวลาอัตโนมัติ', 'schedule',
      schedule.enabled ? ('ทุกวัน ' + schedule.time) : 'ปิดอยู่',
      schedule.enabled
        ? ((schedule.dry_run ? 'โหมดซ้อม' : 'โหมดจริง') + ' · ' + (schedule.timezone || 'Asia/Bangkok'))
        : 'ค่าเริ่มต้นคือปิด',
      'is-sm'));

    var counts = data.job_counts || {};
    var totals = Object.keys(counts).reduce(function (sum, k) { return sum + counts[k]; }, 0);
    host.appendChild(AC.tile(
      'งานทั้งหมดที่บันทึกไว้', 'history', String(totals),
      (counts.succeeded || 0) + ' สำเร็จ · ' + (counts.failed || 0) + ' ล้มเหลว · ' +
      (counts.blocked || 0) + ' ติดเงื่อนไข'));

    /* Operational warnings, only when the API actually reported them. */
    var warnings = $('#ac-overview-warnings');
    if (!warnings) return;
    clear(warnings);
    var problems = (data.state && data.state.permission_problems) || [];
    if (problems.length) {
      warnings.appendChild(AC.callout('warning',
        'สิทธิ์ไฟล์ไม่ปลอดภัย',
        problems.length + ' ไฟล์ในไดเรกทอรีสถานะเปิดให้ผู้ใช้อื่นอ่านได้ ' +
        'ควรแก้เป็น 0600 บนเซิร์ฟเวอร์', 'callout-warn'));
    }
    if (data.recovered_jobs) {
      warnings.appendChild(AC.callout('restart_alt',
        'กู้คืนงานค้างหลังรีสตาร์ต',
        data.recovered_jobs + ' งานที่ค้างสถานะ "กำลังทำงาน" ถูกทำเครื่องหมายว่าล้มเหลว ' +
        'ต้องสั่งทำใหม่ด้วยตัวเอง', 'callout-info'));
    }
    if (data.state && data.state.network_disabled) {
      warnings.appendChild(AC.callout('wifi_off',
        'เซิร์ฟเวอร์ปิดการเชื่อมต่อภายนอก',
        'ตั้งค่า STAR_DISABLE_NETWORK=1 อยู่ การทดสอบแบบ live และการเผยแพร่จริงจะถูกปฏิเสธ',
        'callout-info'));
    }
  }

  /* The current job, read only. Cancel, retry and the event log all live on
     the jobs page; this is the glance that tells you whether to go there. */
  function renderCurrent(job) {
    var empty = $('#ac-current-empty');
    var panel = $('#ac-current-panel');
    if (!empty || !panel) return;

    if (!job) {
      empty.hidden = false;
      panel.hidden = true;
      return;
    }
    empty.hidden = true;
    panel.hidden = false;

    $('#ac-current-id').textContent = job.id;
    $('#ac-current-scope').textContent = AC.jobScopeText(job);

    var chipHost = $('#ac-current-status');
    clear(chipHost);
    chipHost.appendChild(AC.statusChip(job.status));

    var pct = typeof job.progress === 'number' ? job.progress : 0;
    var fill = $('#ac-current-bar-fill');
    fill.style.width = pct + '%';
    fill.className = 'ac-bar-fill is-' + job.status;
    var meter = $('#ac-current-bar');
    meter.setAttribute('aria-valuenow', String(pct));
    meter.setAttribute('aria-valuetext', pct + ' เปอร์เซ็นต์ — ' +
                       (JOB_STATUS_TH[job.status] || job.status));
    $('#ac-current-pct').textContent = pct + '%';

    var link = $('#ac-current-link');
    link.href = AC.jobHref(job.id);
    link.setAttribute('aria-label', 'เปิดรายละเอียดและบันทึกของงาน ' + job.id);
  }

  function scheduleSummaryPoll() {
    if (state.pollTimer) clearTimeout(state.pollTimer);
    state.pollTimer = setTimeout(load, AC.POLL_SUMMARY);
  }

  function load() {
    var host = $('#ac-overview');
    var errorBox = $('#ac-overview-error');
    return AC.fetchOverview()
      .then(function (data) {
        if (errorBox) errorBox.hidden = true;
        renderOverview(data);
        renderCurrent(data.active_job || null);
        /* Poll only while there is something to watch. */
        if (data.active_job) scheduleSummaryPoll();
        else if (state.pollTimer) { clearTimeout(state.pollTimer); state.pollTimer = null; }
      })
      .catch(function (err) {
        AC.settled(host);
        clear(host);
        clear($('#ac-overview-warnings'));
        AC.errorState(errorBox, 'เชื่อมต่อ API ไม่ได้', err.message, load);
        if (state.pollTimer) { clearTimeout(state.pollTimer); state.pollTimer = null; }
      });
  }

  AC.page('dashboard', function () {
    AC.skeleton($('#ac-overview'), 4, 'ac-tile ac-sk-card');
    AC.onRefresh(load);
    load();
  });
})();
