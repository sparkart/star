// Star — Asset Manager with Constellation Gimmicks
const CDN = '/cdn/star';
let manifest = null;
let currentYear, currentMonth;
let currentDay = 'mon';  // default day-of-week

// ── Init ──
document.addEventListener('DOMContentLoaded', () => {
  setupSidebar();
  setupNav();
  setupDaySelector();
  initStarfield();
  startShootingStars();
  setupCreateStar();
  const now = new Date();
  currentYear = now.getFullYear();
  currentMonth = now.getMonth();
  loadManifest().then(() => {
    renderCalendar();
    updateStarCounter();
    updateStarOfDay();
    drawConstellation();
  });
  handleHash();
  window.addEventListener('hashchange', handleHash);
  window.addEventListener('resize', () => {
    resizeStarfield();
    drawConstellation();
  });
});

// ═══════════════════════════════════════════
// 🎨 Starfield Canvas
// ═══════════════════════════════════════════
let stars = [];
let starCanvas, starCtx;
let animFrame;

function initStarfield() {
  starCanvas = document.getElementById('starfield');
  starCtx = starCanvas.getContext('2d');
  resizeStarfield();
  generateStars(200);
  animateStarfield();
}

function resizeStarfield() {
  starCanvas.width = window.innerWidth;
  starCanvas.height = window.innerHeight;
}

function generateStars(count) {
  stars = [];
  for (let i = 0; i < count; i++) {
    stars.push({
      x: Math.random() * starCanvas.width,
      y: Math.random() * starCanvas.height,
      r: Math.random() * 1.8 + .3,
      twinkle: Math.random() * Math.PI * 2,
      twinkleSpeed: Math.random() * .02 + .005,
      opacity: Math.random() * .5 + .3
    });
  }
}

function animateStarfield() {
  starCtx.clearRect(0, 0, starCanvas.width, starCanvas.height);
  stars.forEach(s => {
    s.twinkle += s.twinkleSpeed;
    const alpha = s.opacity + Math.sin(s.twinkle) * .2;
    starCtx.beginPath();
    starCtx.arc(s.x, s.y, s.r, 0, Math.PI * 2);
    starCtx.fillStyle = `rgba(200,210,255,${Math.max(.1, alpha)})`;
    starCtx.fill();
    // glow for bigger stars
    if (s.r > 1.2) {
      starCtx.beginPath();
      starCtx.arc(s.x, s.y, s.r * 2.5, 0, Math.PI * 2);
      starCtx.fillStyle = `rgba(255,215,0,${Math.max(0, alpha * .15)})`;
      starCtx.fill();
    }
  });
  animFrame = requestAnimationFrame(animateStarfield);
}

// ═══════════════════════════════════════════
// 💫 Shooting Stars
// ═══════════════════════════════════════════
function startShootingStars() {
  const shoot = () => {
    triggerShootingStar();
    // Random interval: 8-20 seconds
    setTimeout(shoot, Math.random() * 12000 + 8000);
  };
  // First one after 3s
  setTimeout(shoot, 3000);
}

function triggerShootingStar() {
  const el = document.getElementById('shooting-star');
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
// 🌟 Create Star Button
// ═══════════════════════════════════════════
function setupCreateStar() {
  document.getElementById('create-star-btn').addEventListener('click', async () => {
    const btn = document.getElementById('create-star-btn');
    btn.textContent = '⏳ กำลังสร้าง...';
    btn.disabled = true;

    // Trigger shooting star as visual feedback
    triggerShootingStar();

    // Try API first, fallback to demo
    try {
      const res = await fetch('/api/regenerate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ date: new Date().toISOString().slice(0,10), template: 'default' })
      });
      if (res.ok) {
        showToast('🌟 กำลังสร้างดาวดวงใหม่...');
        // Poll for completion
        setTimeout(async () => {
          await loadManifest();
          renderCalendar();
          updateStarCounter();
          updateStarOfDay();
          drawConstellation();
        }, 8000);
      } else {
        showToast('⚠️ API ยังไม่พร้อม — แต่ดาวก็ยังสวย!');
      }
    } catch {
      showToast('✨ พรุ่งนี้เช้ามาดูดาวใหม่นะ!');
    }

    setTimeout(() => {
      btn.textContent = '✨ สร้างดาว';
      btn.disabled = false;
    }, 3000);
  });
}

// ═══════════════════════════════════════════
// 📊 Star Counter
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
// 🌟 Star of the Day
// ═══════════════════════════════════════════
function updateStarOfDay() {
  const el = document.getElementById('star-of-day');
  const today = new Date().toISOString().slice(0, 10);
  const day = manifest && manifest.days ? manifest.days.find(d => d.date === today) : null;

  if (day && day.status === 'done') {
    const scripts = day.days ? Object.keys(day.days).filter(k => day.days[k]).length : 7;
    el.classList.add('has-star');
    el.querySelector('.sod-text').textContent = `วันนี้ ${formatDate(today)} · ${scripts}/7 scripts`;
    el.querySelector('.sod-icon').textContent = '⭐';
  } else {
    el.classList.remove('has-star');
    el.querySelector('.sod-text').textContent = 'Star of the Day';
    el.querySelector('.sod-icon').textContent = '🌙';
  }
}

// ═══════════════════════════════════════════
// 🔗 Constellation Lines
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
// 🍞 Toast
// ═══════════════════════════════════════════
function showToast(msg) {
  const existing = document.querySelector('.toast');
  if (existing) existing.remove();
  const toast = document.createElement('div');
  toast.className = 'toast';
  toast.textContent = msg;
  document.body.appendChild(toast);
  setTimeout(() => toast.remove(), 3200);
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
}

// ── Manifest ──
async function loadManifest() {
  try {
    const res = await fetch(`${CDN}/manifest.json`);
    manifest = await res.json();
  } catch (e) {
    manifest = { days: [] };
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

// ── Calendar ──
function renderCalendar() {
  const title = document.getElementById('month-title');
  const grid = document.getElementById('cal-grid');
  const loading = document.getElementById('loading');

  const months = ['January','February','March','April','May','June',
    'July','August','September','October','November','December'];
  title.textContent = `${months[currentMonth]} ${currentYear}`;

  // Clear old day cells (keep headers)
  grid.querySelectorAll('.cal-day').forEach(el => el.remove());
  loading.style.display = 'block';

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
    if (day) {
      cell.classList.add(`status-${day.status}`);
      cell.innerHTML = `
        <span class="day-num">${d}</span>
        <span class="day-status">${statusEmoji(day.status)}</span>`;
    } else if (dateStr <= today) {
      cell.classList.add('status-empty');
      cell.innerHTML = `<span class="day-num">${d}</span>`;
    } else {
      cell.innerHTML = `<span class="day-num">${d}</span>`;
    }
    cell.addEventListener('click', () => {
      window.location.hash = `#/${dateStr}`;
      showDetail(dateStr);
    });
    grid.appendChild(cell);
  }

  loading.style.display = 'none';
}

function statusEmoji(status) {
  return { done: '✅', generating: '🔄', failed: '❌', pending: '⏳' }[status] || '·';
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
  document.getElementById('script-status').textContent = '';
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

function loadMeta(dateStr, day) {
  const table = document.getElementById('meta-table');
  const rows = [
    ['วันที่', formatDate(dateStr)],
    ['สถานะ', day ? day.status : 'pending'],
    ['ความยาว', day && day.duration_sec ? `${day.duration_sec}s` : '-'],
    ['ความละเอียด', day && day.resolution ? day.resolution : '-'],
    ['มีวิดีโอ', day && day.has_video ? '✅' : '❌'],
    ['มีเสียง', day && day.has_audio ? '✅' : '❌'],
    ['มีปก', day && day.has_thumbnail ? '✅' : '❌'],
    ['CDN', `${CDN}/${dateStr}/`]
  ];
  table.innerHTML = rows.map(([k,v]) => `<tr><td>${k}</td><td>${v}</td></tr>`).join('');
}

// ── Post Data (Caption + Hashtags) ──
function loadPostData(dateStr, day) {
  const dayData = getDay(dateStr);
  const captionDisplay = document.getElementById('caption-display');
  const hashtagDisplay = document.getElementById('hashtag-display');
  const postStatus = document.getElementById('post-status');
  const dayParam = day || currentDay || 'mon';

  postStatus.textContent = '';
  postStatus.className = 'status-msg';

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
      body: JSON.stringify({ date: dateStr, script })
    });
    if (res.ok) {
      status.textContent = '✓ บันทึกแล้ว';
      status.className = 'status-msg success';
      resetEdit();
      document.getElementById('script-display').textContent = script;
    } else {
      status.textContent = '✗ บันทึกไม่สำเร็จ';
      status.className = 'status-msg error';
    }
  } catch {
    status.textContent = '✗ API ไม่ตอบสนอง';
    status.className = 'status-msg error';
  }
});

function resetEdit() {
  document.getElementById('script-display').classList.remove('hidden');
  document.getElementById('script-editor').classList.add('hidden');
  document.getElementById('edit-btn').classList.remove('hidden');
  document.getElementById('save-btn').classList.add('hidden');
  document.getElementById('cancel-btn').classList.add('hidden');
}

// ── Regenerate ──
document.getElementById('regen-btn').addEventListener('click', async () => {
  const dateStr = getCurrentDate();
  const status = document.getElementById('regen-status');
  const confirmed = confirm(`Regenerate content for ${formatDate(dateStr)}?`);
  if (!confirmed) return;

  status.textContent = '⏳ กำลังสร้าง...';
  status.className = 'status-msg info';
  document.getElementById('regen-btn').disabled = true;

  try {
    const res = await fetch('/api/regenerate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ date: dateStr, template: 'default' })
    });
    const data = await res.json();
    if (res.ok) {
      status.textContent = `🔄 ${data.message || 'เริ่มแล้ว'} — รอสักครู่...`;
      pollStatus(dateStr);
    } else {
      status.textContent = `✗ ${data.error || 'สร้างไม่สำเร็จ'}`;
      status.className = 'status-msg error';
      document.getElementById('regen-btn').disabled = false;
    }
  } catch {
    status.textContent = '✗ API ไม่ตอบสนอง — regenerate ยังไม่พร้อมใช้งาน';
    status.className = 'status-msg error';
    document.getElementById('regen-btn').disabled = false;
  }
});

function pollStatus(dateStr) {
  let attempts = 0;
  const max = 30;
  const interval = setInterval(async () => {
    attempts++;
    try {
      const res = await fetch(`${CDN}/manifest.json`);
      const m = await res.json();
      const day = m.days.find(d => d.date === dateStr);
      if (day && (day.status === 'done' || day.status === 'failed')) {
        clearInterval(interval);
        await loadManifest();
        renderCalendar();
        showDetail(dateStr);
        document.getElementById('regen-status').textContent = day.status === 'done' ? '✓ เสร็จแล้ว!' : '✗ ล้มเหลว';
        document.getElementById('regen-status').className = `status-msg ${day.status === 'done' ? 'success' : 'error'}`;
        document.getElementById('regen-btn').disabled = false;
      } else if (attempts >= max) {
        clearInterval(interval);
        document.getElementById('regen-status').textContent = '⚠️ ใช้เวลานานกว่าปกติ — เช็คใหม่ภายหลัง';
        document.getElementById('regen-btn').disabled = false;
      } else {
        document.getElementById('regen-status').textContent = `🔄 กำลังสร้าง... (${attempts * 5}s)`;
      }
    } catch {
      if (attempts >= max) {
        clearInterval(interval);
        document.getElementById('regen-btn').disabled = false;
      }
    }
  }, 5000);
}

// ── Copy Caption ──
document.getElementById('copy-caption').addEventListener('click', () => {
  const text = document.getElementById('caption-display').textContent;
  if (!text || text === '(ยังไม่มีแคปชั่น)') {
    showToast('⚠️ ยังไม่มีแคปชั่นให้คัดลอก');
    return;
  }
  navigator.clipboard.writeText(text).then(() => {
    const status = document.getElementById('post-status');
    status.textContent = '✓ คัดลอกแคปชั่นแล้ว';
    status.className = 'status-msg success';
    showToast('📋 คัดลอกแคปชั่นแล้ว!');
    setTimeout(() => { status.textContent = ''; status.className = 'status-msg'; }, 3000);
  }).catch(() => {
    showToast('⚠️ คัดลอกไม่สำเร็จ');
  });
});

// ── Copy Hashtags ──
document.getElementById('copy-hashtags').addEventListener('click', () => {
  const tags = document.querySelectorAll('#hashtag-display .hashtag-tag');
  if (tags.length === 0) {
    showToast('⚠️ ยังไม่มีแฮชแท็กให้คัดลอก');
    return;
  }
  const text = Array.from(tags).map(t => t.textContent).join(' ');
  navigator.clipboard.writeText(text).then(() => {
    const status = document.getElementById('post-status');
    status.textContent = '✓ คัดลอกแฮชแท็กแล้ว';
    status.className = 'status-msg success';
    showToast('#️⃣ คัดลอกแฮชแท็กแล้ว!');
    setTimeout(() => { status.textContent = ''; status.className = 'status-msg'; }, 3000);
  }).catch(() => {
    showToast('⚠️ คัดลอกไม่สำเร็จ');
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
