/* ============================================================
   DEFLECTION PROTOCOL — easter-egg mini-game
   Triple-click empty space to start. Pop the awkward questions;
   Johnny fires back the perfect professional dodge.
   ============================================================ */
(function () {
  'use strict';

  // question → Johnny's counter-dodge, by archetype
  const DECK = [
    { cat: 'DEV',       q: 'When will it be done?',            a: "It'll be done when it's ready." },
    { cat: 'DEV',       q: "It's just a small change, right?", a: 'Nothing is ever a small change.' },
    { cat: 'DEV',       q: 'Can you give an estimate?',         a: 'Somewhere between two days and never.' },
    { cat: 'DEV',       q: 'Why is it taking so long?',         a: 'Because you asked for it to work.' },
    { cat: 'SALES',     q: "What's the price?",                 a: "What's your budget? We'll start there." },
    { cat: 'SALES',     q: 'Can we get a discount?',            a: 'For you? The price just went up.' },
    { cat: 'SALES',     q: 'Is that your best offer?',          a: 'It was — until you asked.' },
    { cat: 'HR',        q: "What's the salary range?",          a: 'What were you expecting?' },
    { cat: 'HR',        q: 'Can you share the band?',           a: "Let's hear your number first." },
    { cat: 'PM',        q: 'Is this on track?',                 a: "We're aligning on the alignment." },
    { cat: 'PM',        q: 'Any blockers?',                     a: "Only the ones we haven't found yet." },
    { cat: 'LEGAL',     q: 'Can you just approve it?',          a: 'Let me loop in three more people.' },
    { cat: 'CLIENT',    q: 'Can we add one more thing?',        a: "That's a phase-two conversation." },
    { cat: 'MARKETING', q: 'Can you make the logo bigger?',     a: "We'll explore some directions." },
    { cat: 'SUPPORT',   q: 'Is it down?',                       a: "It's experiencing elevated latency." },
    { cat: 'FINANCE',   q: 'Did we go over budget?',            a: 'We re-baselined the budget.' }
  ];

  const RANKS = [
    { min: 0,  t: 'Brutally Honest',    d: 'You answered the questions. Rookie mistake.' },
    { min: 4,  t: 'Account Manager',    d: 'Smooth. Slippery, even.' },
    { min: 8,  t: 'Master of Deflection', d: 'No question survives contact with you.' },
    { min: 13, t: 'Chief Excuse Officer', d: 'Legendary. Nobody got a straight answer.' }
  ];

  const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  let open = false;

  /* ---------- triple-click trigger on empty space ---------- */
  const INTERACTIVE = 'a,button,input,textarea,select,label,kbd,' +
    '.console,.panel,.term,.btn,.chip,.cal-evt,.mode,.swap-toggle,.copy-btn,' +
    '.tweaks-panel,[data-tweaks-root],.game-overlay,.q-bubble,.game-card';
  let clicks = [];
  document.addEventListener('click', (e) => {
    if (open) return;
    // never trigger while the user is selecting text
    const sel = window.getSelection && window.getSelection();
    if (sel && sel.toString().trim().length > 0) { clicks = []; return; }
    if (e.target.closest && e.target.closest(INTERACTIVE)) { clicks = []; return; }
    const now = performance.now();
    clicks.push(now);
    clicks = clicks.filter((t) => now - t < 1400);
    if (clicks.length >= 5) { clicks = []; start(); }
  });

  /* ---------- DOM scaffold ---------- */
  let ov, hud, scoreEl, comboEl, defEl, timebar, reticle, raf;
  function el(tag, cls, html) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html != null) n.innerHTML = html;
    return n;
  }

  const RET_SVG =
    '<svg viewBox="0 0 46 46" fill="none" stroke="currentColor" stroke-width="1.5">' +
    '<circle cx="23" cy="23" r="13" opacity="0.5"/>' +
    '<path d="M23 2 V12 M23 34 V44 M2 23 H12 M34 23 H44"/>' +
    '<circle cx="23" cy="23" r="1.6" fill="currentColor" stroke="none"/>' +
    '</svg>';

  // game state
  let bubbles = [], spawnAcc = 0, spawnEvery = 1050, elapsed = 0, duration = 26000;
  let score = 0, combo = 0, maxCombo = 0, deflected = 0, lastHit = 0, bestPhrase = '';
  let queue = [], lastT = 0, running = false;

  function shuffledDeck() {
    const d = DECK.slice();
    for (let i = d.length - 1; i > 0; i--) {
      const j = (Math.random() * (i + 1)) | 0;
      [d[i], d[j]] = [d[j], d[i]];
    }
    return d;
  }

  function start() {
    if (open) return;
    open = true;
    document.body.style.overflow = 'hidden';

    ov = el('div', 'game-overlay');
    ov.style.color = 'var(--signal)';
    timebar = el('div', 'game-timebar');
    timebar.style.transform = 'scaleX(1)';

    hud = el('div', 'game-hud');
    const title = el('div', 'gtitle', '<span class="rec"></span> Deflection Protocol');
    const sp = el('div', 'spacer');
    defEl = el('div', 'stat', 'deflected <b>0</b>');
    comboEl = el('div', 'stat combo', 'combo <b>x1</b>');
    scoreEl = el('div', 'stat', 'score <b>0</b>');
    hud.append(title, sp, defEl, comboEl, scoreEl);

    // exit lives outside the HUD flex flow so it can never be pushed off-screen
    const exit = el('button', 'game-exit', '<span class="x">\u2715</span><span class="lbl">exit</span>');
    exit.setAttribute('type', 'button');
    exit.setAttribute('aria-label', 'Exit game');
    exit.addEventListener('click', () => end(true));

    reticle = el('div', 'game-reticle', RET_SVG);
    reticle.style.color = 'var(--signal)';

    ov.append(timebar, hud, reticle, exit);
    document.body.appendChild(ov);
    requestAnimationFrame(() => ov.classList.add('in'));

    // pointer + fire handling
    ov.addEventListener('mousemove', onMove);
    ov.addEventListener('mousedown', onFire);
    document.addEventListener('keydown', onKey);

    showIntro();
  }

  function onMove(e) {
    if (!reticle) return;
    reticle.style.transform = `translate(${e.clientX}px, ${e.clientY}px)`;
  }
  function onKey(e) {
    if (e.key === 'Escape') end(true);
  }

  function showIntro() {
    const card = el('div', 'game-card');
    card.innerHTML =
      '<p class="gk">// incoming awkward questions</p>' +
      '<h2>Deflect everything</h2>' +
      '<p>Questions you should never answer straight are floating in. ' +
      'Shoot each one — Johnny fires back the perfect dodge. ' +
      'Never give a real estimate. Never name the price first.</p>' +
      '<div class="gbtns"></div>' +
      '<p class="keyhint">click bubbles to deflect · <kbd>Esc</kbd> to bail</p>';
    const go = el('button', 'btn btn-primary', 'Engage');
    go.addEventListener('click', () => { card.remove(); runGame(); });
    card.querySelector('.gbtns').appendChild(go);
    ov.appendChild(card);
  }

  function runGame() {
    bubbles = []; spawnAcc = 0; spawnEvery = 1050; elapsed = 0;
    score = 0; combo = 0; maxCombo = 0; deflected = 0; lastHit = 0; bestPhrase = '';
    queue = shuffledDeck();
    running = true; lastT = performance.now();
    updateHud();
    raf = requestAnimationFrame(loop);
  }

  function nextCard() {
    if (!queue.length) queue = shuffledDeck();
    return queue.pop();
  }

  function spawn() {
    const data = nextCard();
    const b = el('div', 'q-bubble');
    b.innerHTML =
      `<div class="cat"><span class="pin"></span>${data.cat}</div>` +
      `<div class="q">${data.q}</div>`;
    b.style.left = '0px';
    b.style.top = '0px';
    ov.appendChild(b);
    const w = b.offsetWidth || 240;
    const h = b.offsetHeight || 80;
    const x = 40 + Math.random() * (window.innerWidth - w - 80);
    const obj = {
      node: b, data,
      x, y: -(h + 30),
      vy: (46 + Math.random() * 34) / 1000, // px per ms, downward
      drift: (Math.random() - 0.5) * 0.018,
      phase: Math.random() * Math.PI * 2,
      amp: 12 + Math.random() * 16,
      dead: false
    };
    b.addEventListener('mousedown', (ev) => { ev.stopPropagation(); hit(obj, ev.clientX, ev.clientY); });
    bubbles.push(obj);
  }

  function hit(obj, cx, cy) {
    if (obj.dead) return;
    obj.dead = true;
    deflected++;
    const now = performance.now();
    combo = (now - lastHit < 2400) ? combo + 1 : 1;
    lastHit = now;
    maxCombo = Math.max(maxCombo, combo);
    score += 100 * combo;
    bestPhrase = obj.data.a;

    obj.node.classList.add('pop');
    setTimeout(() => obj.node.remove(), 280);

    spawnCounter(obj.data.a, obj.x, obj.y);
    if (combo >= 2) spawnCombo('x' + combo + (combo >= 4 ? ' DEFLECTED!' : ''), cx, cy);
    fireReticle();
    updateHud();
  }

  function spawnCounter(text, x, y) {
    const c = el('div', 'counter', `<span class="by">Johnny ›</span>${text}`);
    c.style.left = Math.min(x, window.innerWidth - 320) + 'px';
    c.style.top = Math.max(70, Math.min(y - 10, window.innerHeight - 130)) + 'px';
    ov.appendChild(c);
    setTimeout(() => c.remove(), 1750);
  }
  function spawnCombo(text, x, y) {
    const c = el('div', 'combo-pop', text);
    c.style.left = x + 'px';
    c.style.top = (y - 26) + 'px';
    ov.appendChild(c);
    setTimeout(() => c.remove(), 820);
  }
  function spawnMiss(x, y) {
    const m = el('div', 'miss-x', '✕ missed');
    m.style.left = x + 'px';
    m.style.top = y + 'px';
    ov.appendChild(m);
    setTimeout(() => m.remove(), 620);
  }

  function onFire(e) {
    fireReticle();
    // a click that didn't hit a bubble breaks the combo
    if (e.target === ov || (e.target.closest && !e.target.closest('.q-bubble') && !e.target.closest('.game-card'))) {
      if (running) {
        combo = 0;
        spawnMiss(e.clientX, e.clientY);
        updateHud();
      }
    }
  }
  function fireReticle() {
    if (!reticle) return;
    reticle.classList.remove('fire');
    void reticle.offsetWidth;
    reticle.classList.add('fire');
  }

  function updateHud() {
    if (scoreEl) scoreEl.querySelector('b').textContent = score;
    if (comboEl) comboEl.querySelector('b').textContent = 'x' + Math.max(1, combo);
    if (defEl) defEl.querySelector('b').textContent = deflected;
  }

  function loop(t) {
    if (!running) return;
    raf = requestAnimationFrame(loop);
    const dt = Math.min(50, t - lastT);
    lastT = t;
    elapsed += dt;

    // timer bar
    const left = Math.max(0, 1 - elapsed / duration);
    timebar.style.transform = `scaleX(${left})`;
    if (elapsed >= duration) { end(false); return; }

    // spawn ramp
    spawnAcc += dt;
    spawnEvery = Math.max(560, 1050 - elapsed / 26);
    if (spawnAcc >= spawnEvery) { spawnAcc = 0; spawn(); }

    // move bubbles
    for (let i = bubbles.length - 1; i >= 0; i--) {
      const b = bubbles[i];
      if (b.dead) { bubbles.splice(i, 1); continue; }
      b.phase += 0.0022 * dt;
      b.y += b.vy * dt;
      const wob = Math.sin(b.phase) * b.amp;
      b.node.style.transform = `translate(${b.x + wob}px, ${b.y}px)`;
      if (b.y > window.innerHeight + 140) { b.node.remove(); bubbles.splice(i, 1); }
    }
  }

  function rankFor(n) {
    let r = RANKS[0];
    for (const x of RANKS) if (n >= x.min) r = x;
    return r;
  }

  function end(aborted) {
    running = false;
    cancelAnimationFrame(raf);
    bubbles.forEach((b) => b.node.remove());
    bubbles = [];
    if (aborted) { close(); return; }

    const best = +(localStorage.getItem('johnny_deflect_best') || 0);
    if (score > best) localStorage.setItem('johnny_deflect_best', String(score));
    const isBest = score > best;
    const r = rankFor(deflected);

    const card = el('div', 'game-card');
    card.innerHTML =
      '<p class="gk">// debrief</p>' +
      `<div class="rank">${r.t}</div>` +
      `<p>${r.d}</p>` +
      '<div class="scores">' +
        `<div class="s"><span class="n">${deflected}</span><span class="l">deflected</span></div>` +
        `<div class="s"><span class="n">${score}</span><span class="l">score</span></div>` +
        `<div class="s"><span class="n">x${maxCombo || 1}</span><span class="l">best combo</span></div>` +
      '</div>' +
      (bestPhrase ? `<p class="best-line">last word: <em>"${bestPhrase}"</em></p>` : '') +
      (isBest ? '<p class="best-line">★ new personal best</p>' : `<p class="best-line">personal best: ${Math.max(best, score)}</p>`) +
      '<div class="gbtns"></div>' +
      '<p class="keyhint">or <kbd>Esc</kbd> to return to the page</p>';
    const again = el('button', 'btn btn-primary', 'Run it back');
    again.addEventListener('click', () => { card.remove(); runGame(); });
    const out = el('button', 'btn', 'Exit');
    out.addEventListener('click', () => close());
    card.querySelector('.gbtns').append(again, out);
    ov.appendChild(card);
  }

  function close() {
    running = false;
    cancelAnimationFrame(raf);
    document.removeEventListener('keydown', onKey);
    document.body.style.overflow = '';
    if (ov) {
      ov.classList.remove('in');
      const node = ov;
      setTimeout(() => node.remove(), 280);
    }
    ov = null; reticle = null;
    open = false;
  }

  // expose for the footer hint link
  window.__johnnyDeflect = start;
})();
