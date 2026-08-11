/* ══════════════════════════════════════════════════════════
   Star — Prediction style guide (/automation/guide/)

   A reader for GET /api/automation/prediction-guide, which returns the same
   file the script stage feeds to the prompt, through the same validator. The
   point of reading it here rather than from a copy is that an operator can
   never be looking at rules the pipeline has already rejected: when the API
   says the guide is invalid this page shows the validator's reason and no
   content at all.

   Consequently this file contains no guide text. Every heading below a section
   title is a key from the payload and every sentence is a value from it; the
   renderer only knows shapes — string, list, object — not subject matter.
   ══════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  var AC = window.StarAC;
  if (!AC) return;

  var $ = AC.$, el = AC.el, clear = AC.clear;

  var GUIDE_PATH = '/api/automation/prediction-guide';

  /* Structural labels for the top-level sections only. These name the shape of
     the document, not its rules — the rules themselves are never written here.
     An unknown key falls back to the key itself, so a new section appears
     without a frontend change. */
  var SECTION_LABEL = {
    audience: 'กลุ่มผู้อ่าน',
    core_voice: 'น้ำเสียงหลัก',
    mood_tone: 'อารมณ์และโทน',
    structure: 'โครงสร้างงานเขียน',
    vocabulary: 'คลังคำ',
    prohibited_patterns: 'รูปแบบที่ห้ามใช้',
    factual_rules: 'กติกาด้านข้อเท็จจริง',
    consistency_checklist: 'รายการตรวจก่อนส่ง',
    examples: 'ตัวอย่าง'
  };

  /* Rendered separately in the header, so the body must not repeat them. */
  var HEADER_KEYS = ['version', 'title', 'purpose'];

  function humanise(key) {
    return SECTION_LABEL[key] || String(key).replace(/_/g, ' ');
  }

  /* ── generic value renderer ────────────────────────────── */

  function isPlainObject(value) {
    return value !== null && typeof value === 'object' && !Array.isArray(value);
  }

  /* Depth only decides which heading element is used, so a deeply nested
     payload degrades to plain paragraphs instead of inventing h7. */
  function headingFor(depth) {
    return depth <= 0 ? 'h3' : depth === 1 ? 'h4' : 'h5';
  }

  function renderValue(value, depth) {
    if (value === null || value === undefined) return null;

    if (typeof value === 'string' || typeof value === 'number' ||
        typeof value === 'boolean') {
      return el('p', 'ac-guide-text', String(value));
    }

    if (Array.isArray(value)) {
      if (!value.length) return el('p', 'ac-guide-empty', '—');
      /* A list of scalars is a list; a list of objects is a stack of cards,
         because bullets cannot carry sub-keys legibly. */
      var scalar = value.every(function (item) { return !isPlainObject(item); });
      if (scalar) {
        var list = el('ul', 'ac-guide-list');
        value.forEach(function (item) {
          list.appendChild(el('li', null, String(item)));
        });
        return list;
      }
      var stack = el('div', 'ac-guide-stack');
      value.forEach(function (item) {
        var node = renderValue(item, depth + 1);
        if (node) {
          var card = el('div', 'ac-guide-item');
          card.appendChild(node);
          stack.appendChild(card);
        }
      });
      return stack;
    }

    if (isPlainObject(value)) {
      var wrap = el('div', 'ac-guide-block');
      Object.keys(value).forEach(function (key) {
        var child = renderValue(value[key], depth + 1);
        if (!child) return;
        var group = el('div', 'ac-guide-group');
        group.appendChild(el(headingFor(depth), 'ac-guide-key', humanise(key)));
        group.appendChild(child);
        wrap.appendChild(group);
      });
      return wrap.childNodes.length ? wrap : null;
    }

    return null;
  }

  /* ── page renderers ────────────────────────────────────── */

  function renderMeta(view) {
    var host = $('#ac-guide-meta');
    if (!host) return;
    AC.settled(host);
    clear(host);

    host.appendChild(AC.tile('เวอร์ชันคู่มือ', 'bookmark',
      view.version || '—',
      'รองรับเวอร์ชันหลักที่ ' + (view.supported_major != null ? view.supported_major : '—'),
      'is-sm'));
    host.appendChild(AC.tile('สถานะการตรวจสอบ', view.valid ? 'verified' : 'error',
      view.valid ? 'ใช้งานได้' : 'ใช้ไม่ได้',
      view.valid ? 'ขั้นตอนเขียนบทใช้ไฟล์นี้อยู่' : 'ขั้นตอนเขียนบทจะปฏิเสธไฟล์นี้',
      'is-sm'));
    host.appendChild(AC.tile('ความยาวที่ส่งเข้าพรอมป์ต์', 'straighten',
      String(view.prompt_chars != null ? view.prompt_chars : '—'),
      'จำกัดไม่เกิน ' + (view.max_prompt_chars != null ? view.max_prompt_chars : '—') +
      ' อักขระ'));
    host.appendChild(AC.tile('จำนวนแฮชแท็กที่บังคับ', 'tag',
      String(view.hashtag_count != null ? view.hashtag_count : '—'),
      'ตรวจอัตโนมัติทุกครั้งที่สร้างแคปชั่น'));

    /* The path the server read, so an operator editing the wrong copy of the
       file can see it immediately. */
    var source = $('#ac-guide-source');
    if (source) source.textContent = view.source || '—';

    var heading = $('#ac-guide-heading');
    if (heading) heading.textContent = view.required_heading || '—';
  }

  function renderGuide(view) {
    var host = $('#ac-guide-body');
    var invalid = $('#ac-guide-invalid');
    if (!host || !invalid) return;
    AC.settled(host);
    clear(host);
    clear(invalid);

    if (!view.valid || !view.guide) {
      invalid.hidden = false;
      invalid.appendChild(AC.callout('report', 'คู่มือนี้ยังใช้กับไปป์ไลน์ไม่ได้',
        view.error || 'เซิร์ฟเวอร์ไม่ได้ระบุเหตุผล',
        'callout-warn'));
      host.hidden = true;
      return;
    }
    invalid.hidden = true;
    host.hidden = false;

    var guide = view.guide;
    var title = $('#ac-guide-title');
    if (title) title.textContent = guide.title || '';
    var purpose = $('#ac-guide-purpose');
    if (purpose) purpose.textContent = guide.purpose || '';

    var toc = $('#ac-guide-toc');
    if (toc) clear(toc);

    Object.keys(guide).forEach(function (key) {
      if (HEADER_KEYS.indexOf(key) !== -1) return;
      var content = renderValue(guide[key], 0);
      if (!content) return;

      var sectionId = 'ac-guide-s-' + key;
      var section = el('section', 'panel-card ac-guide-section');
      section.id = sectionId;
      section.setAttribute('aria-labelledby', sectionId + '-h');

      var head = el('div', 'panel-card-head');
      var h2 = el('h2', null, humanise(key));
      h2.id = sectionId + '-h';
      head.appendChild(h2);
      head.appendChild(el('span', 'ac-guide-rawkey', key));
      section.appendChild(head);

      var body = el('div', 'panel-card-body');
      body.appendChild(content);
      section.appendChild(body);
      host.appendChild(section);

      if (toc) {
        var link = el('a', 'ac-guide-toc-link', humanise(key));
        link.href = '#' + sectionId;
        toc.appendChild(link);
      }
    });
  }

  function load() {
    var body = $('#ac-guide-body');
    var errorBox = $('#ac-guide-error');
    AC.skeleton($('#ac-guide-meta'), 4, 'ac-tile ac-sk-card');
    AC.skeleton(body, 3, 'ac-sk-card');
    return AC.api('GET', GUIDE_PATH)
      .then(function (view) {
        if (errorBox) errorBox.hidden = true;
        renderMeta(view);
        renderGuide(view);
      })
      .catch(function (err) {
        AC.settled(body);
        clear(body);
        clear($('#ac-guide-meta'));
        clear($('#ac-guide-invalid'));
        AC.errorState(errorBox, 'โหลดคู่มือไม่สำเร็จ', err.message, load);
      });
  }

  AC.page('guide', function () {
    AC.onRefresh(load);
    load();
  });
})();
