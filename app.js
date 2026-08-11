// Star — Content Ops workspace
const CDN = '/cdn/star';
let manifest = null;
let manifestError = false;
let currentYear, currentMonth;
let currentDay = 'mon';  // default day-of-week

let ephemData = null;      // astrology data cache
let ephemPromise = null;   // in-flight ephemeris request
let currentScriptText = '';

const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
const prefersReducedMotion = () => reduceMotion.matches;

const $ = (id) => document.getElementById(id);

// ── Init ──
document.addEventListener('DOMContentLoaded', () => {
  setupSidebar();
  setupNav();
  setupDaySelector();
  initStarfield();
  startShootingStars();
  setupCreateStar();
  setupCommandPalette();
  setupShortcuts();
  const now = new Date();
  currentYear = now.getFullYear();
  currentMonth = now.getMonth();
  refreshData(true);
  handleHash();
  window.addEventListener('hashchange', handleHash);
  window.addEventListener('resize', debounce(() => {
    resizeStarfield();
    drawConstellation();
  }, 120));
});

// Load (or reload) the manifest, then repaint everything that depends on it.
async function refreshData(initial) {
  showCalendarSkeleton();
  await loadManifest();
  renderCalendar();
  updateStarCounter();
  updateStarOfDay();
  updateSyncChip();
  drawConstellation();
  if (!initial && !manifestError) showToast('อัปเดตข้อมูลล่าสุดแล้ว', 'success');
  // Re-render an open detail view with fresh data
  if (!initial && !$('detail-view').classList.contains('hidden')) {
    const dateStr = getCurrentDate();
    if (isValidDate(dateStr)) showDetail(dateStr);
  }
}

function debounce(fn, wait) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), wait); };
}

// ═══════════════════════════════════════════
// Ambient starfield — background texture only
// ═══════════════════════════════════════════
let stars = [];
let starCanvas, starCtx;
let animFrame;

function initStarfield() {
  starCanvas = document.getElementById('starfield');
  if (!starCanvas) return;
  starCtx = starCanvas.getContext('2d');
  resizeStarfield();
  if (prefersReducedMotion()) {
    paintStarfield(false);   // static, no animation loop
  } else {
    animateStarfield();
  }
  reduceMotion.addEventListener('change', () => {
    cancelAnimationFrame(animFrame);
    if (prefersReducedMotion()) paintStarfield(false);
    else animateStarfield();
  });
}

function resizeStarfield() {
  if (!starCanvas) return;
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  starCanvas.width = Math.round(window.innerWidth * dpr);
  starCanvas.height = Math.round(window.innerHeight * dpr);
  starCanvas.style.width = window.innerWidth + 'px';
  starCanvas.style.height = window.innerHeight + 'px';
  starCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
  // Density scales with viewport; capped so small screens stay cheap.
  const density = Math.round((window.innerWidth * window.innerHeight) / 22000);
  generateStars(Math.max(40, Math.min(140, density)));
  if (prefersReducedMotion()) paintStarfield(false);
}

function generateStars(count) {
  stars = [];
  for (let i = 0; i < count; i++) {
    stars.push({
      x: Math.random() * window.innerWidth,
      y: Math.random() * window.innerHeight,
      r: Math.random() * 1.1 + .25,
      twinkle: Math.random() * Math.PI * 2,
      twinkleSpeed: Math.random() * .012 + .003,
      opacity: Math.random() * .28 + .12
    });
  }
}

function paintStarfield(animate) {
  starCtx.clearRect(0, 0, window.innerWidth, window.innerHeight);
  stars.forEach(s => {
    if (animate) s.twinkle += s.twinkleSpeed;
    const alpha = s.opacity + (animate ? Math.sin(s.twinkle) * .1 : 0);
    starCtx.beginPath();
    starCtx.arc(s.x, s.y, s.r, 0, Math.PI * 2);
    starCtx.fillStyle = `rgba(186,199,225,${Math.max(.05, alpha)})`;
    starCtx.fill();
  });
}

function animateStarfield() {
  paintStarfield(true);
  animFrame = requestAnimationFrame(animateStarfield);
}

// ═══════════════════════════════════════════
// Shooting star — rare ambient cue + action feedback
// ═══════════════════════════════════════════
function startShootingStars() {
  const shoot = () => {
    if (!document.hidden) triggerShootingStar();
    setTimeout(shoot, Math.random() * 60000 + 45000);   // 45–105s
  };
  setTimeout(shoot, 20000);
}

function triggerShootingStar() {
  const el = document.getElementById('shooting-star');
  if (!el || prefersReducedMotion()) return;
  const w = window.innerWidth;
  const h = window.innerHeight;
  // Start from top-right quadrant
  const startX = w * .5 + Math.random() * w * .5;
  const startY = Math.random() * h * .3;
  el.style.left = startX + 'px';
  el.style.top = startY + 'px';
  el.classList.remove('fly');
  void el.offsetWidth; // force reflow
  el.classList.add('fly');
}

// ═══════════════════════════════════════════
// Create star button
// ═══════════════════════════════════════════
// Swap the icon glyph and the label copy in place — the icon and label spans
// stay in the DOM so nothing has to be rebuilt when the button comes back.
function setButtonBusy(btn, busy, busyLabel) {
  const icon = btn.querySelector('.ms');
  const label = btn.querySelector('.btn-label');
  if (busy) {
    if (icon && !btn.dataset.idleIcon) btn.dataset.idleIcon = icon.textContent;
    if (label && !btn.dataset.idleLabel) btn.dataset.idleLabel = label.textContent;
    if (icon) icon.textContent = 'progress_activity';
    if (label) label.textContent = busyLabel || 'กำลังทำงาน…';
  } else {
    if (icon && btn.dataset.idleIcon) icon.textContent = btn.dataset.idleIcon;
    if (label && btn.dataset.idleLabel) label.textContent = btn.dataset.idleLabel;
  }
  btn.disabled = busy;
  btn.setAttribute('aria-busy', String(busy));
}

// Local calendar date — toISOString() would shift to UTC and can name yesterday.
function todayStr() {
  const d = new Date();
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

// The API answers JSON, but an error page or empty body must not throw here.
async function readJson(res) {
  try {
    return await res.json();
  } catch {
    return null;
  }
}

function setupCreateStar() {
  document.getElementById('create-star-btn').addEventListener('click', async () => {
    const btn = document.getElementById('create-star-btn');
    setButtonBusy(btn, true, 'กำลังเผยแพร่…');

    // Trigger shooting star as visual feedback
    triggerShootingStar();

    try {
      const res = await fetch('/api/regenerate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ date: todayStr(), day: 'all' })
      });
      const data = await readJson(res);
      if (res.ok) {
        const n = data && Array.isArray(data.published) ? data.published.length : 0;
        showToast(n ? `เผยแพร่สคริปต์ที่มีอยู่แล้ว ${n} วัน` : 'เผยแพร่สคริปต์ที่มีอยู่แล้ว', 'success');
        await loadManifest();
        renderCalendar();
        updateStarCounter();
        updateStarOfDay();
        drawConstellation();
      } else {
        showToast((data && data.error) || 'เผยแพร่ไม่สำเร็จ', 'warn');
      }
    } catch {
      showToast('เชื่อมต่อ API ไม่ได้ — ลองใหม่อีกครั้งภายหลัง', 'error');
    } finally {
      setButtonBusy(btn, false);
    }
  });
}

// ═══════════════════════════════════════════
// Star counter
// ═══════════════════════════════════════════
function updateStarCounter() {
  const el = document.getElementById('star-count');
  if (!manifest || !manifest.days) return;
  const count = manifest.days.filter(d => d.status === 'done').length;
  // Animate counter
  const current = parseInt(el.textContent) || 0;
  animateNumber(el, current, count, 800);
}

function animateNumber(el, from, to, duration) {
  const start = performance.now();
  const step = (now) => {
    const progress = Math.min((now - start) / duration, 1);
    const eased = 1 - Math.pow(1 - progress, 3); // ease-out cubic
    el.textContent = Math.round(from + (to - from) * eased);
    if (progress < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

// ═══════════════════════════════════════════
// Star of the day
// ═══════════════════════════════════════════
function updateStarOfDay() {
  const el = document.getElementById('star-of-day');
  const today = new Date().toISOString().slice(0, 10);
  const day = manifest && manifest.days ? manifest.days.find(d => d.date === today) : null;

  if (day && day.status === 'done') {
    const scripts = day.days ? Object.keys(day.days).filter(k => day.days[k]).length : 7;
    el.classList.add('has-star');
    el.querySelector('.sod-text').textContent = `วันนี้ ${formatDate(today)} · ${scripts}/7 scripts`;
    el.querySelector('.sod-icon').textContent = 'star';
  } else {
    el.classList.remove('has-star');
    el.querySelector('.sod-text').textContent = 'Star of the Day';
    el.querySelector('.sod-icon').textContent = 'schedule';
  }
}

// ═══════════════════════════════════════════
// Constellation lines
// ═══════════════════════════════════════════
function drawConstellation() {
  // Only when calendar view is visible
  if (document.getElementById('calendar-view').classList.contains('hidden')) return;

  let canvas = document.getElementById('constellation-canvas');
  // Create if not exists
  if (!canvas) {
    canvas = document.createElement('canvas');
    canvas.id = 'constellation-canvas';
    const calSection = document.getElementById('calendar-view');
    calSection.style.position = 'relative';
    calSection.appendChild(canvas);
  }

  const grid = document.getElementById('cal-grid');
  const rect = grid.getBoundingClientRect();
  canvas.width = rect.width;
  canvas.height = rect.height;
  canvas.style.width = rect.width + 'px';
  canvas.style.height = rect.height + 'px';
  canvas.style.top = (rect.top - grid.parentElement.getBoundingClientRect().top) + 'px';
  canvas.style.left = (rect.left - grid.parentElement.getBoundingClientRect().left) + 'px';

  const ctx = canvas.getContext('2d');
  const cells = grid.querySelectorAll('.cal-day.status-done');
  if (cells.length < 2) return;

  // Collect star positions
  const points = [];
  cells.forEach(cell => {
    const cr = cell.getBoundingClientRect();
    points.push({
      x: cr.left - rect.left + cr.width / 2,
      y: cr.top - rect.top + cr.height / 2,
    });
  });

  // Draw constellation lines between nearby stars
  ctx.strokeStyle = 'rgba(255,215,0,.12)';
  ctx.lineWidth = 1;
  for (let i = 0; i < points.length; i++) {
    for (let j = i + 1; j < points.length; j++) {
      const dx = points[i].x - points[j].x;
      const dy = points[i].y - points[j].y;
      const dist = Math.sqrt(dx * dx + dy * dy);
      // Only connect stars within reasonable distance
      if (dist < 200) {
        const alpha = Math.max(0, (1 - dist / 200) * .3);
        ctx.strokeStyle = `rgba(255,215,0,${alpha})`;
        ctx.beginPath();
        ctx.moveTo(points[i].x, points[i].y);
        ctx.lineTo(points[j].x, points[j].y);
        ctx.stroke();
      }
    }
  }
}

// ═══════════════════════════════════════════
// Toast
// ═══════════════════════════════════════════
// Each variant maps to a .toast-* accent in the stylesheet and a leading
// Material Symbol; the message itself is always plain text.
const TOAST_ICONS = {
  info:    'info',
  success: 'check_circle',
  warn:    'warning',
  error:   'error',
};

function showToast(msg, variant = 'info') {
  const kind = TOAST_ICONS[variant] ? variant : 'info';
  const region = document.getElementById('toast-region') || document.body;
  region.querySelectorAll('.toast').forEach(t => t.remove());

  const toast = document.createElement('div');
  toast.className = `toast toast-${kind}`;
  toast.appendChild(msIcon(TOAST_ICONS[kind]));
  const text = document.createElement('span');
  text.textContent = msg;
  toast.appendChild(text);
  region.appendChild(toast);

  setTimeout(() => {
    toast.classList.add('is-out');
    setTimeout(() => toast.remove(), 240);
  }, 3200);
}

// A decorative Material Symbols glyph. The text content is the ligature name,
// which the icon font renders as the icon itself.
function msIcon(name) {
  const el = document.createElement('span');
  el.className = 'ms';
  el.setAttribute('aria-hidden', 'true');
  el.textContent = name;
  return el;
}

// Status line under a panel: icon + concise Thai text, coloured by .status-msg
// modifier classes. Passing an empty message clears the line entirely.
function setStatus(el, msg, variant, icon) {
  if (!el) return;
  el.className = variant ? `status-msg ${variant}` : 'status-msg';
  el.textContent = '';
  if (!msg) return;
  if (icon) el.appendChild(msIcon(icon));
  const text = document.createElement('span');
  text.textContent = msg;
  el.appendChild(text);
}

// ── Sidebar ──
function setupSidebar() {
  const hamburger = document.getElementById('hamburger');
  const sidebar = document.getElementById('sidebar');
  hamburger.addEventListener('click', () => {
    const open = sidebar.classList.toggle('sidebar-open');
    hamburger.setAttribute('aria-expanded', open);
  });
}

// ── Nav ──
function setupNav() {
  document.getElementById('prev-month').addEventListener('click', () => {
    currentMonth--; if (currentMonth < 0) { currentMonth = 11; currentYear--; }
    renderCalendar();
  });
  document.getElementById('next-month').addEventListener('click', () => {
    currentMonth++; if (currentMonth > 11) { currentMonth = 0; currentYear++; }
    renderCalendar();
  });
  document.getElementById('today-btn').addEventListener('click', () => {
    const now = new Date();
    currentYear = now.getFullYear();
    currentMonth = now.getMonth();
    renderCalendar();
  });
  // The error state renderCalendar() falls back to needs a way out.
  document.getElementById('cal-retry').addEventListener('click', () => refreshData());
}

// ═══════════════════════════════════════════
// Command palette (Ctrl/Cmd+K)
// ═══════════════════════════════════════════
// Every entry points at an action that already exists on the page — either a
// control we click for the user, or a function the app already defines. The
// palette adds no behaviour of its own.
const CMDK_COMMANDS = [
  { icon: 'refresh',       label: 'โหลดข้อมูลใหม่',   hint: 'รีเฟรช', run: () => refreshData() },
  { icon: 'auto_awesome',  label: 'สร้างดาวดวงใหม่',  hint: 'สร้าง',  click: '#create-star-btn' },
  { icon: 'chevron_left',  label: 'เดือนก่อนหน้า',    hint: 'ปฏิทิน', click: '#prev-month' },
  { icon: 'chevron_right', label: 'เดือนถัดไป',       hint: 'ปฏิทิน', click: '#next-month' },
  { icon: 'arrow_back',    label: 'กลับไปหน้าปฏิทิน', hint: 'นำทาง',  click: '#back-btn' },
  { icon: 'folder_open',   label: 'คลังคอนเทนต์',     hint: 'นำทาง',  click: '.nav-links a[href="/content/"]' },
  { icon: 'bolt',          label: 'ระบบอัตโนมัติ',    hint: 'นำทาง',  click: '.nav-links a[href="/automation/"]' },
  { icon: 'menu_book',     label: 'คู่มือระบบ',       hint: 'นำทาง',  click: '.nav-links a[href="/howto/"]' },
];

let cmdkVisible = [];         // commands matching the current query
let cmdkIndex = 0;            // highlighted row within cmdkVisible
let cmdkReturnFocus = null;   // element focused before the palette opened

function setupCommandPalette() {
  const root = $('cmdk'), input = $('cmdk-input'), list = $('cmdk-list');
  if (!root || !input || !list) return;

  const trigger = $('cmd-trigger');
  if (trigger) trigger.addEventListener('click', openCommandPalette);

  const scrim = $('cmdk-scrim');
  if (scrim) scrim.addEventListener('click', closeCommandPalette);

  input.addEventListener('input', () => renderCommandList(input.value));

  input.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown')      { e.preventDefault(); moveCommandSelection(1); }
    else if (e.key === 'ArrowUp')   { e.preventDefault(); moveCommandSelection(-1); }
    else if (e.key === 'Enter')     { e.preventDefault(); runCommand(cmdkVisible[cmdkIndex]); }
    else if (e.key === 'Tab')       { e.preventDefault(); } // keep focus inside the dialog
  });

  list.addEventListener('click', (e) => {
    const item = e.target.closest('.cmdk-item');
    if (item) runCommand(cmdkVisible[Number(item.dataset.index)]);
  });
}

// Ctrl/Cmd+K toggles the palette; Escape closes it while it is open.
function setupShortcuts() {
  document.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && !e.altKey && (e.key === 'k' || e.key === 'K')) {
      e.preventDefault();
      if (isCommandPaletteOpen()) closeCommandPalette(); else openCommandPalette();
    } else if (e.key === 'Escape' && isCommandPaletteOpen()) {
      e.preventDefault();
      closeCommandPalette();
    }
  });
}

function isCommandPaletteOpen() {
  const root = $('cmdk');
  return !!root && !root.hidden;
}

function openCommandPalette() {
  const root = $('cmdk'), input = $('cmdk-input');
  if (!root || !input || !root.hidden) return;
  cmdkReturnFocus = document.activeElement;
  root.hidden = false;
  input.value = '';
  renderCommandList('');
  input.focus();
}

function closeCommandPalette() {
  const root = $('cmdk'), input = $('cmdk-input');
  if (!root || root.hidden) return;
  root.hidden = true;
  if (input) input.removeAttribute('aria-activedescendant');
  const back = cmdkReturnFocus;
  cmdkReturnFocus = null;
  if (back && document.contains(back) && typeof back.focus === 'function') back.focus();
}

function renderCommandList(query) {
  const list = $('cmdk-list'), empty = $('cmdk-empty');
  if (!list) return;
  const q = (query || '').trim().toLowerCase();
  // Skip commands whose target control is not on this page.
  cmdkVisible = CMDK_COMMANDS.filter(cmd =>
    (cmd.run || document.querySelector(cmd.click)) &&
    (!q || `${cmd.label} ${cmd.hint}`.toLowerCase().includes(q))
  );
  cmdkIndex = 0;
  list.innerHTML = cmdkVisible.map((cmd, i) => `
    <li class="cmdk-item" id="cmdk-opt-${i}" role="option" data-index="${i}" aria-selected="false">
      <span class="ms" aria-hidden="true">${cmd.icon}</span>
      <span>${cmd.label}</span>
      <span class="cmdk-hint">${cmd.hint}</span>
    </li>`).join('');
  if (empty) empty.hidden = cmdkVisible.length > 0;
  highlightCommand();
}

function highlightCommand() {
  const list = $('cmdk-list'), input = $('cmdk-input');
  if (!list) return;
  Array.from(list.children).forEach((li, i) => {
    li.setAttribute('aria-selected', String(i === cmdkIndex));
  });
  const active = list.children[cmdkIndex];
  if (!input) return;
  if (active) {
    input.setAttribute('aria-activedescendant', active.id);
    active.scrollIntoView({ block: 'nearest' });
  } else {
    input.removeAttribute('aria-activedescendant');
  }
}

function moveCommandSelection(step) {
  if (!cmdkVisible.length) return;
  cmdkIndex = (cmdkIndex + step + cmdkVisible.length) % cmdkVisible.length;
  highlightCommand();
}

function runCommand(cmd) {
  if (!cmd) return;
  closeCommandPalette();  // restore focus before the action runs
  if (cmd.run) {
    cmd.run();
  } else {
    const target = document.querySelector(cmd.click);
    if (target) target.click();
  }
}

// ── Manifest ──
async function loadManifest() {
  try {
    const res = await fetch(`${CDN}/manifest.json`);
    if (!res.ok) throw new Error(`manifest HTTP ${res.status}`);
    const data = await res.json();
    if (!data || !Array.isArray(data.days)) throw new Error('manifest has no days[]');
    manifest = data;
    manifestError = false;
  } catch (e) {
    // Keep an empty-but-valid shape so getDay() and the counters stay safe.
    manifest = { days: [] };
    manifestError = true;
  }
}

async function updateManifest() {
  try {
    await fetch('/api/manifest', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(manifest)
    });
  } catch(e) {}
}

function getDay(dateStr) {
  return manifest.days.find(d => d.date === dateStr);
}

// ── Calendar skeleton ──
// Stand-in cells shown while the manifest is in flight. renderCalendar() always
// clears them afterwards — whether it drew a month or fell back to the error state.
// .cal-skeleton declares display:grid, which outranks the UA [hidden] rule, so the
// attribute alone will not hide it; the inline display does the actual hiding.
function showCalendarSkeleton() {
  const skeleton = document.getElementById('cal-skeleton');
  if (!skeleton) return;
  const firstDay = new Date(currentYear, currentMonth, 1).getDay();
  const daysInMonth = new Date(currentYear, currentMonth + 1, 0).getDate();
  const cells = Math.ceil((firstDay + daysInMonth) / 7) * 7;   // same footprint as the real grid
  skeleton.innerHTML = '<div class="sk"></div>'.repeat(cells);
  skeleton.hidden = false;
  skeleton.style.display = '';
  document.getElementById('cal-error').hidden = true;
  // Drop stale cells so a refresh does not show old data above the skeleton.
  document.getElementById('cal-grid').querySelectorAll('.cal-day').forEach(el => el.remove());
}

function hideCalendarSkeleton() {
  const skeleton = document.getElementById('cal-skeleton');
  if (!skeleton) return;
  skeleton.hidden = true;
  skeleton.style.display = 'none';
  skeleton.innerHTML = '';
}

// ── Calendar ──
function renderCalendar() {
  const title = document.getElementById('month-title');
  const grid = document.getElementById('cal-grid');
  const loading = document.getElementById('loading');
  const errorBlock = document.getElementById('cal-error');

  const months = ['January','February','March','April','May','June',
    'July','August','September','October','November','December'];
  title.textContent = `${months[currentMonth]} ${currentYear}`;

  // Clear old day cells (keep headers)
  grid.querySelectorAll('.cal-day').forEach(el => el.remove());
  loading.style.display = 'block';

  try {
    if (manifestError) {
      // Drawing bare cells here would read as "nothing is scheduled" rather than
      // "we could not load", so show the error state instead.
      errorBlock.hidden = false;
      return;
    }
    errorBlock.hidden = true;
    drawMonth(grid);
  } finally {
    hideCalendarSkeleton();
    loading.style.display = 'none';
  }
}

function drawMonth(grid) {
  const firstDay = new Date(currentYear, currentMonth, 1).getDay();
  const daysInMonth = new Date(currentYear, currentMonth + 1, 0).getDate();

  // Empty cells before first day
  for (let i = 0; i < firstDay; i++) {
    const cell = document.createElement('div');
    cell.className = 'cal-day empty';
    grid.appendChild(cell);
  }

  const today = new Date().toISOString().slice(0, 10);

  for (let d = 1; d <= daysInMonth; d++) {
    const dateStr = `${currentYear}-${String(currentMonth+1).padStart(2,'0')}-${String(d).padStart(2,'0')}`;
    const day = getDay(dateStr);
    const cell = document.createElement('div');
    cell.className = 'cal-day';
    const num = document.createElement('span');
    num.className = 'day-num';
    num.textContent = String(d);
    cell.appendChild(num);
    if (day) {
      cell.classList.add(`status-${day.status}`);
      cell.appendChild(buildStatusChip(day.status));
    } else if (dateStr <= today) {
      cell.classList.add('status-empty');
    }
    cell.addEventListener('click', () => {
      window.location.hash = `#/${dateStr}`;
      showDetail(dateStr);
    });
    grid.appendChild(cell);
  }
}

// ── Sync chip ──
// Connection state for the manifest, plus when it was last generated.
function updateSyncChip() {
  const chip = document.getElementById('sync-chip');
  const text = document.getElementById('sync-text');
  if (!chip || !text) return;

  chip.classList.toggle('is-live', !manifestError);
  chip.classList.toggle('is-error', manifestError);

  if (manifestError) {
    text.textContent = 'เชื่อมต่อไม่ได้';
    chip.title = 'อ่าน manifest จาก CDN ไม่สำเร็จ';
    return;
  }

  const updated = manifest && manifest.updated ? new Date(manifest.updated) : null;
  const stamped = updated && !isNaN(updated.getTime());
  const time = stamped
    ? updated.toLocaleTimeString('th-TH', { hour: '2-digit', minute: '2-digit' })
    : '';
  text.textContent = stamped ? `เชื่อมต่อแล้ว · ${time} น.` : 'เชื่อมต่อแล้ว';
  chip.title = stamped
    ? `manifest อัปเดตล่าสุด ${updated.toLocaleString('th-TH')}`
    : 'เวลาที่ manifest อัปเดตล่าสุด';
}

// ── Status vocabulary ──
// One icon + one Thai label per manifest status, matching the sidebar legend.
const STATUS_META = {
  done:       { icon: 'check_circle',       label: 'เสร็จแล้ว' },
  generating: { icon: 'progress_activity',  label: 'กำลังสร้าง' },
  failed:     { icon: 'error',              label: 'ล้มเหลว' },
  pending:    { icon: 'schedule',           label: 'รอดำเนินการ' },
};

function statusMeta(status) {
  return STATUS_META[status] || { icon: 'remove', label: 'ไม่ทราบสถานะ' };
}

// Built as nodes so a status string from the manifest is never parsed as HTML.
// .day-status hides .label on narrow screens and keeps the icon.
function buildStatusChip(status) {
  const meta = statusMeta(status);
  const chip = document.createElement('span');
  chip.className = 'day-status';
  chip.appendChild(msIcon(meta.icon));
  const label = document.createElement('span');
  label.className = 'label';
  label.textContent = meta.label;
  chip.appendChild(label);
  return chip;
}

// ── Day Selector ──
function setupDaySelector() {
  document.querySelectorAll('.day-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.day-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      currentDay = btn.dataset.day;
      const dateStr = getCurrentDate();
      loadScript(dateStr, currentDay);
      loadPostData(dateStr, currentDay);
    });
  });
}

// ── Detail View ──
function showDetail(dateStr) {
  const day = getDay(dateStr);
  document.getElementById('calendar-view').classList.add('hidden');
  document.getElementById('detail-view').classList.remove('hidden');
  document.getElementById('detail-date').textContent = formatDate(dateStr);

  const statusEl = document.getElementById('detail-status');
  if (day) {
    statusEl.innerHTML = `<span class="badge badge-${day.status}">${day.status.toUpperCase()}</span>
      ${day.duration_sec ? `· ${day.duration_sec}s` : ''}
      ${day.resolution ? `· ${day.resolution}` : ''}`;
  } else {
    statusEl.innerHTML = '<span class="badge badge-pending">PENDING</span>';
  }

  // Video
  const video = document.getElementById('video-player');
  const noVideo = document.getElementById('no-video');
  const videoUrl = `${CDN}/${dateStr}/video.mp4`;
  if (day && day.has_video) {
    video.querySelector('source').src = videoUrl;
    video.load();
    video.classList.remove('hidden');
    noVideo.classList.add('hidden');
  } else {
    video.classList.add('hidden');
    noVideo.classList.remove('hidden');
  }

  // Audio
  const audio = document.getElementById('audio-player');
  const noAudio = document.getElementById('no-audio');
  const audioUrl = `${CDN}/${dateStr}/narration.mp3`;
  if (day && day.has_audio) {
    audio.querySelector('source').src = audioUrl;
    audio.load();
    audio.classList.remove('hidden');
    noAudio.classList.add('hidden');
  } else {
    audio.classList.add('hidden');
    noAudio.classList.remove('hidden');
  }

  // Script — reset day to mon, reload
  currentDay = 'mon';
  document.querySelectorAll('.day-btn').forEach(b => b.classList.remove('active'));
  const monBtn = document.querySelector('.day-btn[data-day="mon"]');
  if (monBtn) monBtn.classList.add('active');
  loadScript(dateStr, currentDay);

  // Meta
  loadMeta(dateStr, day);

  // Post (Caption + Hashtags) — pass currentDay
  loadPostData(dateStr, currentDay);

  // Buttons
  document.getElementById('download-video').classList.toggle('hidden', !(day && day.has_video));
  document.getElementById('download-audio').classList.toggle('hidden', !(day && day.has_audio));
  if (day && day.has_video) {
    document.getElementById('download-video').onclick = () => downloadFile(videoUrl, `${dateStr}_video.mp4`);
  }
  if (day && day.has_audio) {
    document.getElementById('download-audio').onclick = () => downloadFile(audioUrl, `${dateStr}_narration.mp3`);
  }

  // Reset edit state
  document.getElementById('script-display').classList.remove('hidden');
  document.getElementById('script-editor').classList.add('hidden');
  document.getElementById('edit-btn').classList.remove('hidden');
  document.getElementById('save-btn').classList.add('hidden');
  document.getElementById('cancel-btn').classList.add('hidden');
  setStatus(document.getElementById('script-status'), '');
}

async function loadScript(dateStr, day) {
  const display = document.getElementById('script-display');
  const dayParam = day || 'mon';
  try {
    const res = await fetch(`${CDN}/${dateStr}/${dayParam}.txt`);
    if (res.ok) {
      display.textContent = await res.text();
    } else {
      display.textContent = '(ยังไม่มี script)';
    }
  } catch {
    display.textContent = '(โหลด script ไม่สำเร็จ)';
  }
}

// Present/absent marker for a media file.
function flagCell(present) {
  const flag = document.createElement('span');
  flag.className = `flag ${present ? 'yes' : 'no'}`;
  flag.appendChild(msIcon(present ? 'check_circle' : 'remove_circle_outline'));
  const label = document.createElement('span');
  label.textContent = present ? 'มี' : 'ไม่มี';
  flag.appendChild(label);
  return flag;
}

function loadMeta(dateStr, day) {
  const table = document.getElementById('meta-table');
  const rows = [
    ['วันที่', formatDate(dateStr)],
    ['สถานะ', day ? statusMeta(day.status).label : statusMeta('pending').label],
    ['ความยาว', day && day.duration_sec ? `${day.duration_sec}s` : '-'],
    ['ความละเอียด', day && day.resolution ? day.resolution : '-'],
    ['มีวิดีโอ', flagCell(!!(day && day.has_video))],
    ['มีเสียง', flagCell(!!(day && day.has_audio))],
    ['มีปก', flagCell(!!(day && day.has_thumbnail))],
    ['CDN', `${CDN}/${dateStr}/`]
  ];
  // Rewrite the rows only — the <caption> in the markup has to survive.
  let body = table.querySelector('tbody');
  if (!body) {
    body = document.createElement('tbody');
    table.appendChild(body);
  }
  body.textContent = '';
  rows.forEach(([k, v]) => {
    const tr = document.createElement('tr');
    const key = document.createElement('td');
    key.textContent = k;
    const val = document.createElement('td');
    if (v instanceof Node) val.appendChild(v); else val.textContent = v;
    tr.append(key, val);
    body.appendChild(tr);
  });
}

// ── Post Data (Caption + Hashtags) ──
function loadPostData(dateStr, day) {
  const dayData = getDay(dateStr);
  const captionDisplay = document.getElementById('caption-display');
  const hashtagDisplay = document.getElementById('hashtag-display');
  const postStatus = document.getElementById('post-status');
  const dayParam = day || currentDay || 'mon';

  setStatus(postStatus, '');

  if (dayData && dayData.captions && dayData.captions[dayParam]) {
    const caps = dayData.captions[dayParam];
    captionDisplay.textContent = caps.caption || '(ยังไม่มีแคปชั่น)';
    
    hashtagDisplay.innerHTML = '';
    if (caps.hashtags && caps.hashtags.length > 0) {
      caps.hashtags.forEach(tag => {
        const span = document.createElement('span');
        span.className = 'hashtag-tag';
        span.textContent = tag;
        hashtagDisplay.appendChild(span);
      });
    } else {
      hashtagDisplay.innerHTML = '<span style="color: var(--muted); font-size: .85rem;">(ยังไม่มีแฮชแท็ก)</span>';
    }
  } else if (dayData && dayData.captions && Object.keys(dayData.captions).length > 0) {
    // Has captions but not for this day — show fallback
    captionDisplay.textContent = '(ยังไม่มีแคปชั่นสำหรับวันนี้)';
    hashtagDisplay.innerHTML = '<span style="color: var(--muted); font-size: .85rem;">(ยังไม่มีแฮชแท็ก)</span>';
  } else {
    captionDisplay.textContent = '(ยังไม่มีแคปชั่น)';
    hashtagDisplay.innerHTML = '<span style="color: var(--muted); font-size: .85rem;">(ยังไม่มีแฮชแท็ก)</span>';
  }
}

// ── Edit Script ──
document.getElementById('edit-btn').addEventListener('click', () => {
  const display = document.getElementById('script-display');
  const editor = document.getElementById('script-editor');
  editor.value = display.textContent;
  display.classList.add('hidden');
  editor.classList.remove('hidden');
  document.getElementById('edit-btn').classList.add('hidden');
  document.getElementById('save-btn').classList.remove('hidden');
  document.getElementById('cancel-btn').classList.remove('hidden');
});

document.getElementById('cancel-btn').addEventListener('click', resetEdit);
document.getElementById('save-btn').addEventListener('click', async () => {
  const dateStr = getCurrentDate();
  const script = document.getElementById('script-editor').value;
  const status = document.getElementById('script-status');
  try {
    const res = await fetch('/api/save-script', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ date: dateStr, day: currentDay, script })
    });
    if (res.ok) {
      setStatus(status, 'บันทึกแล้ว', 'success', 'check_circle');
      resetEdit();
      document.getElementById('script-display').textContent = script;
    } else {
      setStatus(status, 'บันทึกไม่สำเร็จ', 'error', 'error');
    }
  } catch {
    setStatus(status, 'API ไม่ตอบสนอง', 'error', 'error');
  }
});

function resetEdit() {
  document.getElementById('script-display').classList.remove('hidden');
  document.getElementById('script-editor').classList.add('hidden');
  document.getElementById('edit-btn').classList.remove('hidden');
  document.getElementById('save-btn').classList.add('hidden');
  document.getElementById('cancel-btn').classList.add('hidden');
}

// ── Publish to CDN ──
// The API copies the script that already exists on disk; it never writes text.
document.getElementById('regen-btn').addEventListener('click', async () => {
  const dateStr = getCurrentDate();
  const status = document.getElementById('regen-status');
  const btn = document.getElementById('regen-btn');
  const confirmed = confirm(`เผยแพร่สคริปต์ที่มีอยู่ของ ${formatDate(dateStr)} ขึ้น CDN อีกครั้ง?`);
  if (!confirmed) return;

  setStatus(status, 'กำลังเผยแพร่…', 'info', 'progress_activity');
  btn.disabled = true;

  try {
    const res = await fetch('/api/regenerate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ date: dateStr, day: currentDay })
    });
    const data = await readJson(res);
    if (res.ok) {
      await loadManifest();
      renderCalendar();
      showDetail(dateStr);
      setStatus(document.getElementById('regen-status'), 'เผยแพร่ขึ้น CDN แล้ว', 'success', 'check_circle');
    } else {
      setStatus(status, (data && data.error) || 'เผยแพร่ไม่สำเร็จ', 'error', 'error');
    }
  } catch {
    setStatus(status, 'API ไม่ตอบสนอง — เผยแพร่ไม่สำเร็จ', 'error', 'error');
  } finally {
    document.getElementById('regen-btn').disabled = false;
  }
});

// ── Copy Caption ──
document.getElementById('copy-caption').addEventListener('click', () => {
  const text = document.getElementById('caption-display').textContent;
  if (!text || text === '(ยังไม่มีแคปชั่น)') {
    showToast('ยังไม่มีแคปชั่นให้คัดลอก', 'warn');
    return;
  }
  navigator.clipboard.writeText(text).then(() => {
    const status = document.getElementById('post-status');
    setStatus(status, 'คัดลอกแคปชั่นแล้ว', 'success', 'check_circle');
    showToast('คัดลอกแคปชั่นแล้ว', 'success');
    setTimeout(() => setStatus(status, ''), 3000);
  }).catch(() => {
    showToast('คัดลอกไม่สำเร็จ', 'error');
  });
});

// ── Copy Hashtags ──
document.getElementById('copy-hashtags').addEventListener('click', () => {
  const tags = document.querySelectorAll('#hashtag-display .hashtag-tag');
  if (tags.length === 0) {
    showToast('ยังไม่มีแฮชแท็กให้คัดลอก', 'warn');
    return;
  }
  const text = Array.from(tags).map(t => t.textContent).join(' ');
  navigator.clipboard.writeText(text).then(() => {
    const status = document.getElementById('post-status');
    setStatus(status, 'คัดลอกแฮชแท็กแล้ว', 'success', 'check_circle');
    showToast('คัดลอกแฮชแท็กแล้ว', 'success');
    setTimeout(() => setStatus(status, ''), 3000);
  }).catch(() => {
    showToast('คัดลอกไม่สำเร็จ', 'error');
  });
});

// ── Tabs ──
document.querySelectorAll('.tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
    tab.classList.add('active');
    document.getElementById(`panel-${tab.dataset.tab}`).classList.add('active');
  });
});

// ── Back ──
document.getElementById('back-btn').addEventListener('click', () => {
  window.location.hash = '';
  document.getElementById('detail-view').classList.add('hidden');
  document.getElementById('calendar-view').classList.remove('hidden');
  setTimeout(drawConstellation, 100);
});

// ── Hash routing ──
function handleHash() {
  const hash = window.location.hash;
  if (hash.startsWith('#/')) {
    const dateStr = hash.slice(2);
    showDetail(dateStr);
  } else {
    document.getElementById('detail-view').classList.add('hidden');
    document.getElementById('calendar-view').classList.remove('hidden');
    setTimeout(drawConstellation, 100);
  }
}

function getCurrentDate() {
  const hash = window.location.hash;
  return hash.startsWith('#/') ? hash.slice(2) : new Date().toISOString().slice(0, 10);
}

// ── Helpers ──
// Strict YYYY-MM-DD: right shape, real month, day inside that month's length.
function isValidDate(dateStr) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(dateStr));
  if (!m) return false;
  const year = Number(m[1]), month = Number(m[2]), day = Number(m[3]);
  if (month < 1 || month > 12 || day < 1) return false;
  const leap = (year % 4 === 0 && year % 100 !== 0) || year % 400 === 0;
  const lengths = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  return day <= lengths[month - 1];
}

function formatDate(dateStr) {
  const [y, m, d] = dateStr.split('-');
  const months = ['ม.ค.','ก.พ.','มี.ค.','เม.ย.','พ.ค.','มิ.ย.',
    'ก.ค.','ส.ค.','ก.ย.','ต.ค.','พ.ย.','ธ.ค.'];
  return `${parseInt(d)} ${months[parseInt(m)-1]} ${parseInt(y)+543}`;
}

function downloadFile(url, filename) {
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
}
