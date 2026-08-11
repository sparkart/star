/* ══════════════════════════════════════════════════════════
   Star — Automation section nav

   The six links themselves are in the markup of every page, so the section is
   navigable with scripting off and the active link is already correct on first
   paint. This file only enhances what is already there:

     · re-asserts the active link from <body data-ac-page="…">, so a page that
       was copied from a sibling cannot ship the wrong highlight;
     · prefetches a sibling page once, on hover or keyboard focus, and only
       when the network is not metered or in data-saver mode.

   It touches nothing else and is a no-op on a document without the nav.
   ══════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  var nav = document.getElementById('ac-nav');
  if (!nav) return;

  var links = Array.prototype.slice.call(nav.querySelectorAll('[data-ac-nav]'));
  if (!links.length) return;

  var current = (document.body && document.body.dataset &&
                 document.body.dataset.acPage) || '';

  /* Markup carries the active state; this makes a stale copy self-correct. */
  links.forEach(function (link) {
    var active = link.dataset.acNav === current;
    link.classList.toggle('is-active', active);
    if (active) {
      link.setAttribute('aria-current', 'page');
    } else {
      link.removeAttribute('aria-current');
    }
  });

  /* ── prefetch on intent ────────────────────────────────── */

  function metered() {
    var conn = navigator.connection || navigator.mozConnection ||
               navigator.webkitConnection;
    if (!conn) return false;
    if (conn.saveData) return true;
    return /(^|-)2g$/.test(conn.effectiveType || '');
  }

  var prefetched = {};

  function prefetch(href) {
    if (!href || prefetched[href] || metered()) return;
    prefetched[href] = true;
    var hint = document.createElement('link');
    hint.rel = 'prefetch';
    hint.href = href;
    document.head.appendChild(hint);
  }

  links.forEach(function (link) {
    if (link.dataset.acNav === current) return;
    var warm = function () { prefetch(link.getAttribute('href')); };
    /* `once` keeps the listener from outliving the single fetch it triggers. */
    link.addEventListener('pointerenter', warm, { once: true });
    link.addEventListener('focus', warm, { once: true });
    link.addEventListener('touchstart', warm, { once: true, passive: true });
  });
})();
