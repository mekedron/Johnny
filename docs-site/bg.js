/* Johnny landing — cyberpunk background canvas.
   Dark + atmospheric: an orthogonal circuit-board network with travelling
   signal pulses, sparse horizontal data-streaks, drifting data bits, glitch
   blocks and a slow scan sweep.
   Mouse-reactive: parallax depth, cursor spotlight, HUD reticle, streak spawn.
   Honours reduced-motion and the --glitch tweak (0 off / 1 subtle / 2 full). */
(function () {
  'use strict';
  const cv = document.getElementById('bg');
  if (!cv) return;
  const ctx = cv.getContext('2d', { alpha: true });
  const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  let W = 0, H = 0, DPR = 1;
  function glow() {
    return (
      getComputedStyle(document.documentElement).getPropertyValue('--glow').trim() ||
      'rgba(249,233,78,0.55)'
    );
  }
  function rgb() {
    const m = glow().match(/(\d+),\s*(\d+),\s*(\d+)/);
    return m ? `${m[1]},${m[2]},${m[3]}` : '249,233,78';
  }

  /* ---------- mouse state ---------- */
  const mouse = { x: -999, y: -999, tx: -999, ty: -999, active: 0, last: 0, vx: 0, vy: 0 };
  window.addEventListener('mousemove', (e) => {
    const now = performance.now();
    const dt = Math.max(16, now - mouse.last);
    mouse.vx = (e.clientX - mouse.tx) / dt;
    mouse.vy = (e.clientY - mouse.ty) / dt;
    mouse.tx = e.clientX;
    mouse.ty = e.clientY;
    mouse.last = now;
  }, { passive: true });
  window.addEventListener('mouseleave', () => { mouse.last = 0; });

  /* ---------- blaster fire on click (page-wide discoverability hint) ---------- */
  let retRecoil = 0;
  function fireBlaster(x, y) {
    if (reduce) return;
    const s = document.createElement('div');
    s.className = 'blaster-shot';
    s.style.left = x + 'px';
    s.style.top = y + 'px';
    let sp = '';
    for (let i = 0; i < 6; i++) sp += `<i class="bf-spark" style="--a:${i * 60}deg"></i>`;
    s.innerHTML = '<span class="bf-flash"></span><span class="bf-ring"></span>' + sp;
    document.body.appendChild(s);
    setTimeout(() => s.remove(), 380);
  }
  window.addEventListener('mousedown', (e) => {
    // the game overlay (z-index 200) has its own reticle/fire; skip underneath it
    if (document.querySelector('.game-overlay')) return;
    if (e.button !== 0) return;
    fireBlaster(e.clientX, e.clientY);
    retRecoil = performance.now();
  });

  /* ---------- scene objects ---------- */
  let streaks = [], pips = [];
  const net = { edges: [], pulses: [], nodes: [], cols: 0, rows: 0, canvas: null, lastColor: '' };

  function build() {
    buildCircuit();
    streaks = [];
    pips = [];
    for (let i = 0; i < 11; i++) pips.push(newPip());
  }

  /* ---------- circuit network (replaces the perspective grid) ---------- */
  function makeEdge(a, b) {
    // Manhattan route with one right-angle corner — reads as a PCB trace
    const corner = Math.random() < 0.5 ? { x: b.x, y: a.y } : { x: a.x, y: b.y };
    const pts = [{ x: a.x, y: a.y }, corner, { x: b.x, y: b.y }];
    const segs = [];
    let len = 0;
    for (let i = 0; i < pts.length - 1; i++) {
      const l = Math.hypot(pts[i + 1].x - pts[i].x, pts[i + 1].y - pts[i].y);
      segs.push(l); len += l;
    }
    return { pts, segs, len };
  }
  function buildCircuit() {
    const spacing = 72;
    const cols = Math.ceil(W / spacing) + 1;
    const rows = Math.ceil(H / spacing) + 1;
    const jit = spacing * 0.22;
    const nodes = [];
    for (let r = 0; r < rows; r++) {
      nodes[r] = [];
      for (let c = 0; c < cols; c++) {
        nodes[r][c] = {
          x: c * spacing + (Math.random() - 0.5) * jit,
          y: r * spacing + (Math.random() - 0.5) * jit,
          live: false
        };
      }
    }
    const edges = [];
    for (let r = 0; r < rows; r++) {
      for (let c = 0; c < cols; c++) {
        const n = nodes[r][c];
        if (c < cols - 1 && Math.random() < 0.55) {
          edges.push(makeEdge(n, nodes[r][c + 1]));
          n.live = nodes[r][c + 1].live = true;
        }
        if (r < rows - 1 && Math.random() < 0.5) {
          edges.push(makeEdge(n, nodes[r + 1][c]));
          n.live = nodes[r + 1][c].live = true;
        }
      }
    }
    net.nodes = nodes; net.edges = edges; net.cols = cols; net.rows = rows;
    net.pulses = [];
    const pcount = Math.min(46, Math.round(edges.length * 0.08));
    for (let i = 0; i < pcount; i++) net.pulses.push(newPulse());
    renderNet();
  }
  function newPulse() {
    const e = net.edges.length ? net.edges[(Math.random() * net.edges.length) | 0] : null;
    const pick = Math.random();
    const col = pick < 0.72 ? rgb() : pick < 0.88 ? '52,228,208' : '255,46,99';
    return { e, t: -Math.random() * 220, speed: 0.9 + Math.random() * 1.7, col, a: 0.5 + Math.random() * 0.3 };
  }
  // bake the static trace + pad layer once (cheap to blit each frame)
  function renderNet() {
    const off = document.createElement('canvas');
    off.width = Math.max(1, W * DPR);
    off.height = Math.max(1, H * DPR);
    const o = off.getContext('2d');
    o.setTransform(DPR, 0, 0, DPR, 0, 0);
    const c = rgb();
    o.lineWidth = 1;
    o.lineCap = 'round';
    o.lineJoin = 'round';
    o.strokeStyle = `rgba(${c},0.06)`;
    for (const e of net.edges) {
      o.beginPath();
      o.moveTo(e.pts[0].x, e.pts[0].y);
      for (let i = 1; i < e.pts.length; i++) o.lineTo(e.pts[i].x, e.pts[i].y);
      o.stroke();
    }
    for (let r = 0; r < net.rows; r++) {
      for (let cc = 0; cc < net.cols; cc++) {
        const n = net.nodes[r][cc];
        if (!n.live) continue;
        o.fillStyle = `rgba(${c},0.11)`;
        o.fillRect(n.x - 1.5, n.y - 1.5, 3, 3);
        o.fillStyle = `rgba(${c},0.045)`;
        o.fillRect(n.x - 3, n.y - 0.5, 6, 1);
        o.fillRect(n.x - 0.5, n.y - 3, 1, 6);
      }
    }
    net.canvas = off;
    net.lastColor = c;
  }
  function posOnEdge(e, t) {
    let d = Math.max(0, t);
    for (let i = 0; i < e.segs.length; i++) {
      if (d <= e.segs[i]) {
        const p0 = e.pts[i], p1 = e.pts[i + 1];
        const f = e.segs[i] ? d / e.segs[i] : 0;
        return { x: p0.x + (p1.x - p0.x) * f, y: p0.y + (p1.y - p0.y) * f };
      }
      d -= e.segs[i];
    }
    const last = e.pts[e.pts.length - 1];
    return { x: last.x, y: last.y };
  }
  function drawPulses(intensity) {
    for (const p of net.pulses) {
      if (!p.e) { Object.assign(p, newPulse()); continue; }
      p.t += p.speed * (3 + intensity * 3);
      if (p.t > p.e.len + 8) { Object.assign(p, newPulse()); continue; }
      if (p.t < 0) continue;
      const pos = posOnEdge(p.e, p.t);
      const tail = posOnEdge(p.e, Math.max(0, p.t - 16));
      const grd = ctx.createLinearGradient(tail.x, tail.y, pos.x, pos.y);
      grd.addColorStop(0, `rgba(${p.col},0)`);
      grd.addColorStop(1, `rgba(${p.col},${p.a * intensity})`);
      ctx.strokeStyle = grd;
      ctx.lineWidth = 1.5;
      ctx.lineCap = 'round';
      ctx.beginPath();
      ctx.moveTo(tail.x, tail.y);
      ctx.lineTo(pos.x, pos.y);
      ctx.stroke();
      ctx.fillStyle = `rgba(${p.col},${Math.min(0.85, p.a * 1.5)})`;
      ctx.fillRect(pos.x - 1, pos.y - 1, 2.4, 2.4);
    }
  }
  function newPip() {
    return {
      x: Math.random() * W, y: Math.random() * H,
      vx: (Math.random() - 0.5) * 0.25, vy: (Math.random() - 0.5) * 0.25,
      a: 0.06 + Math.random() * 0.13
    };
  }

  function size() {
    DPR = Math.min(window.devicePixelRatio || 1, 2);
    W = window.innerWidth;
    H = window.innerHeight;
    cv.width = W * DPR;
    cv.height = H * DPR;
    cv.style.width = W + 'px';
    cv.style.height = H + 'px';
    ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
    build();
  }
  size();
  let rt;
  window.addEventListener('resize', () => { clearTimeout(rt); rt = setTimeout(size, 180); });

  function glitchLevel() {
    const v = parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--glitch'));
    return isNaN(v) ? 1 : v;
  }

  /* ---------- layers ---------- */
  function drawPips(c) {
    for (const p of pips) {
      ctx.fillStyle = `rgba(${c},${p.a})`;
      ctx.fillRect(p.x, p.y, 2, 2);
      ctx.fillStyle = `rgba(${c},${p.a * 0.5})`;
      ctx.fillRect(p.x - 2, p.y, 6, 1);
      ctx.fillRect(p.x, p.y - 2, 1, 6);
      p.x += p.vx;
      p.y += p.vy;
      if (p.x < -10 || p.x > W + 10 || p.y < -10 || p.y > H + 10) Object.assign(p, newPip());
    }
  }

  function spawnStreak(g, atY) {
    const cyan = '52,228,208', mag = '255,46,99', yellow = rgb();
    const pick = Math.random();
    const col = pick < 0.5 ? yellow : pick < 0.78 ? cyan : mag;
    streaks.push({
      x: -120,
      y: atY == null ? Math.random() * H : atY,
      w: 70 + Math.random() * 180,
      speed: 6 + Math.random() * 10 + g * 3,
      col,
      a: 0.2 + Math.random() * 0.16
    });
  }
  function drawStreaks() {
    for (let i = streaks.length - 1; i >= 0; i--) {
      const s = streaks[i];
      const grd = ctx.createLinearGradient(s.x - s.w, s.y, s.x, s.y);
      grd.addColorStop(0, `rgba(${s.col},0)`);
      grd.addColorStop(1, `rgba(${s.col},${s.a})`);
      ctx.strokeStyle = grd;
      ctx.lineWidth = 1.2;
      ctx.beginPath();
      ctx.moveTo(s.x - s.w, s.y);
      ctx.lineTo(s.x, s.y);
      ctx.stroke();
      ctx.fillStyle = `rgba(${s.col},${Math.min(0.7, s.a * 2.2)})`;
      ctx.fillRect(s.x - 1, s.y - 1, 3, 2);
      s.x += s.speed;
      if (s.x - s.w > W) streaks.splice(i, 1);
    }
  }

  let glitchUntil = 0, blocks = [];
  function maybeGlitch(c, g) {
    const p = g >= 2 ? 0.05 : 0.018;
    if (performance.now() > glitchUntil && Math.random() < p) {
      glitchUntil = performance.now() + 70 + Math.random() * 120;
      const n = 1 + ((Math.random() * (g >= 2 ? 4 : 2)) | 0);
      blocks = [];
      for (let k = 0; k < n; k++) {
        blocks.push({
          x: Math.random() * W * 0.7 + W * 0.15,
          y: Math.random() * H,
          w: 40 + Math.random() * 180,
          h: 2 + Math.random() * 14,
          dx: (Math.random() - 0.5) * 40
        });
      }
    }
    if (performance.now() < glitchUntil) {
      for (const b of blocks) {
        ctx.fillStyle = 'rgba(52,228,208,0.12)';
        ctx.fillRect(b.x + b.dx, b.y, b.w, b.h);
        ctx.fillStyle = 'rgba(255,46,99,0.12)';
        ctx.fillRect(b.x - b.dx, b.y + 1, b.w, Math.max(1, b.h * 0.5));
        ctx.fillStyle = `rgba(${c},0.08)`;
        ctx.fillRect(b.x, b.y, b.w, 1);
      }
    }
  }

  let scan = -80;
  function sweep(c) {
    scan += 1.6;
    if (scan > H + 80) scan = -80;
    const grd = ctx.createLinearGradient(0, scan - 60, 0, scan + 2);
    grd.addColorStop(0, `rgba(${c},0)`);
    grd.addColorStop(1, `rgba(${c},0.05)`);
    ctx.fillStyle = grd;
    ctx.fillRect(0, scan - 60, W, 62);
  }

  /* ---------- cursor spotlight + HUD reticle ---------- */
  function spotlight(c) {
    if (mouse.active <= 0.01) return;
    const r = Math.min(W, H) * 0.26;
    const g = ctx.createRadialGradient(mouse.x, mouse.y, 0, mouse.x, mouse.y, r);
    g.addColorStop(0, `rgba(${c},${0.10 * mouse.active})`);
    g.addColorStop(0.5, `rgba(${c},${0.035 * mouse.active})`);
    g.addColorStop(1, `rgba(${c},0)`);
    ctx.fillStyle = g;
    ctx.fillRect(mouse.x - r, mouse.y - r, r * 2, r * 2);
  }
  function reticle(c) {
    if (mouse.active <= 0.02) return;
    const a = mouse.active;
    const x = mouse.x, y = mouse.y;
    ctx.save();
    // recoil kick on fire
    const since = performance.now() - retRecoil;
    if (since < 180) {
      const k = since / 180;
      const sc = 0.78 + 0.22 * k; // 0.78 -> 1
      ctx.translate(x, y);
      ctx.scale(sc, sc);
      ctx.translate(-x, -y);
    }
    ctx.strokeStyle = `rgba(${c},${0.5 * a})`;
    ctx.lineWidth = 1;
    // gapped crosshair
    const gap = 7, arm = 16;
    ctx.beginPath();
    ctx.moveTo(x - gap - arm, y); ctx.lineTo(x - gap, y);
    ctx.moveTo(x + gap, y); ctx.lineTo(x + gap + arm, y);
    ctx.moveTo(x, y - gap - arm); ctx.lineTo(x, y - gap);
    ctx.moveTo(x, y + gap); ctx.lineTo(x, y + gap + arm);
    ctx.stroke();
    // rotating-ish corner ticks on a square
    const s = 22;
    ctx.strokeStyle = `rgba(${c},${0.38 * a})`;
    const corners = [[-1,-1],[1,-1],[1,1],[-1,1]];
    ctx.beginPath();
    corners.forEach(([sx, sy]) => {
      ctx.moveTo(x + sx * s, y + sy * s - sy * 6);
      ctx.lineTo(x + sx * s, y + sy * s);
      ctx.lineTo(x + sx * s - sx * 6, y + sy * s);
    });
    ctx.stroke();
    // center dot
    ctx.fillStyle = `rgba(${c},${0.8 * a})`;
    ctx.fillRect(x - 1, y - 1, 2, 2);
    ctx.restore();
  }

  /* ---------- contrast guard (lighter than before) ---------- */
  function guard() {
    const lg = ctx.createLinearGradient(0, 0, W, 0);
    lg.addColorStop(0, 'rgba(8,8,10,0.5)');
    lg.addColorStop(0.42, 'rgba(8,8,10,0.22)');
    lg.addColorStop(1, 'rgba(8,8,10,0)');
    ctx.fillStyle = lg;
    ctx.fillRect(0, 0, W, H);
    const rg = ctx.createRadialGradient(W * 0.7, H * 0.3, H * 0.2, W * 0.5, H * 0.5, H * 1.05);
    rg.addColorStop(0, 'rgba(8,8,10,0)');
    rg.addColorStop(1, 'rgba(8,8,10,0.42)');
    ctx.fillStyle = rg;
    ctx.fillRect(0, 0, W, H);
  }

  /* ---------- update + compositing ---------- */
  function update() {
    // ease cursor + activity
    if (mouse.tx < -500) { mouse.active += (0 - mouse.active) * 0.06; }
    else {
      mouse.x += (mouse.tx - mouse.x) * 0.18;
      mouse.y += (mouse.ty - mouse.y) * 0.18;
      const idle = performance.now() - mouse.last;
      const target = idle < 1500 ? 1 : 0;
      mouse.active += (target - mouse.active) * 0.05;
    }
  }

  function frame(intensity) {
    const c = rgb();
    update();
    ctx.clearRect(0, 0, W, H);
    ctx.fillStyle = '#08080a';
    ctx.fillRect(0, 0, W, H);

    // parallax offset for the scene (depth), guard/reticle excluded
    let px = 0, py = 0;
    if (mouse.tx > -500) {
      px = (mouse.x / W - 0.5) * -22;
      py = (mouse.y / H - 0.5) * -14;
    }

    if (c !== net.lastColor) renderNet();

    ctx.globalCompositeOperation = 'lighter';
    ctx.save();
    ctx.translate(px, py);
    if (net.canvas) ctx.drawImage(net.canvas, 0, 0, W, H);
    drawPulses(intensity);
    drawPips(c);
    drawStreaks();
    sweep(c);
    maybeGlitch(c, glitchLevel());
    ctx.restore();
    // spotlight brightens whatever sits under the cursor
    spotlight(c);
    ctx.globalCompositeOperation = 'source-over';

    guard();

    ctx.globalCompositeOperation = 'lighter';
    reticle(c);
    ctx.globalCompositeOperation = 'source-over';
  }

  function staticFrame() {
    const c = rgb();
    ctx.fillStyle = '#08080a';
    ctx.fillRect(0, 0, W, H);
    if (c !== net.lastColor) renderNet();
    ctx.globalCompositeOperation = 'lighter';
    if (net.canvas) ctx.drawImage(net.canvas, 0, 0, W, H);
    drawPips(c);
    ctx.globalCompositeOperation = 'source-over';
    guard();
  }

  let raf, last = 0, streakTimer = 0;
  function loop(t) {
    raf = requestAnimationFrame(loop);
    if (document.hidden) return;
    if (t - last < 33) return; // ~30fps
    last = t;
    const g = glitchLevel();
    if (g <= 0) { staticFrame(); return; }
    const intensity = g >= 2 ? 1.35 : 1;
    streakTimer += 1;
    const cadence = g >= 2 ? 14 : 26;
    if (streakTimer % cadence === 0 && Math.random() > 0.3) spawnStreak(g);
    // spawn a streak along the cursor row when moving fast
    const speed = Math.hypot(mouse.vx, mouse.vy);
    if (speed > 1.1 && mouse.tx > -500 && Math.random() > 0.86) spawnStreak(g, mouse.y);
    frame(intensity);
  }

  if (reduce) staticFrame();
  else raf = requestAnimationFrame(loop);
})();
