/* JOHNNY landing — interactions (vanilla) */
(function () {
  'use strict';

  /* ---- scroll reveal ---- */
  const io = new IntersectionObserver(
    (entries) => {
      entries.forEach((e) => {
        if (e.isIntersecting) {
          e.target.classList.add('in');
          io.unobserve(e.target);
        }
      });
    },
    { threshold: 0.12, rootMargin: '0px 0px -8% 0px' }
  );
  document.querySelectorAll('.reveal').forEach((el) => io.observe(el));

  // fallback: ensure anything already in (or near) the viewport reveals even if
  // IntersectionObserver doesn't fire (e.g. background tab, non-painted frame)
  setTimeout(() => {
    document.querySelectorAll('.reveal:not(.in)').forEach((el) => {
      if (el.getBoundingClientRect().top < window.innerHeight * 1.1) {
        el.classList.add('in');
        io.unobserve(el);
      }
    });
  }, 1300);

  // safety net: reveal anything already near the top on load so above-the-fold
  // content never sits invisible waiting on the observer.
  requestAnimationFrame(() => {
    document.querySelectorAll('.reveal').forEach((el) => {
      if (el.getBoundingClientRect().top < window.innerHeight * 0.92) {
        el.classList.add('in');
        io.unobserve(el);
      }
    });
  });

  /* ---- mobile menu ---- */
  const burger = document.getElementById('burger');
  const hudNav = document.getElementById('hudNav');
  if (burger) {
    const setOpen = (open) => {
      document.body.classList.toggle('nav-open', open);
      burger.setAttribute('aria-expanded', open ? 'true' : 'false');
    };
    burger.addEventListener('click', () => setOpen(!document.body.classList.contains('nav-open')));
    if (hudNav) hudNav.addEventListener('click', (e) => { if (e.target.closest('a')) setOpen(false); });
    window.addEventListener('resize', () => { if (window.innerWidth > 720) setOpen(false); });
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') setOpen(false); });
  }

  /* ---- video-conference: active speaker + live captions ---- */
  const vgrid = document.getElementById('vgrid');
  if (vgrid) {
    const tiles = [...vgrid.querySelectorAll('.vtile')];
    const capSpk = document.getElementById('capSpk');
    const capTxt = document.getElementById('capTxt');
    // seat: 0 Panam · 1 Judy · 2 Viktor · 3 Johnny(bot)
    // a pool of self-contained scenes — one is picked at random each load
    const scenes = [
      [
        { seat: 0, who: 'Panam', t: "Let's lock the run on the Arasaka subnet before we wrap." },
        { seat: 1, who: 'Judy',   t: 'Two ICE layers still hot — black ICE on the inner ring.' },
        { seat: 3, who: 'JOHNNY', t: 'I can map a quieter breach and route us past the ICE.', bot: true },
        { seat: 2, who: 'Viktor', t: 'Chrome the runner first or he flatlines on the daemon.' },
        { seat: 0, who: 'Panam', t: "Relax, Vik. He's only died twice this week." },
        { seat: 3, who: 'JOHNNY', t: "Three times. The transcript doesn't lie.", bot: true },
        { seat: 1, who: 'Judy',   t: 'Nobody likes a snitch with perfect recall, Johnny.' }
      ],
      [
        { seat: 2, who: 'Viktor', t: "Fixer's paying eddies up front this time. No 'exposure'." },
        { seat: 0, who: 'Panam', t: 'Last crew that took exposure is still in a fridge in Watson.' },
        { seat: 1, who: 'Judy',   t: "Relay's patched. We get a ninety-second window, no more." },
        { seat: 3, who: 'JOHNNY', t: "Ninety-four. I padded it. You're all chronically late.", bot: true },
        { seat: 0, who: 'Panam', t: 'Did the meeting bot just call us slow?' },
        { seat: 3, who: 'JOHNNY', t: "[ suggest ] 'we'll be on time' — awaiting approval to say it.", bot: true }
      ],
      [
        { seat: 1, who: 'Judy',   t: "The braindance edit's clean. No spikes, no headaches this time." },
        { seat: 0, who: 'Panam', t: 'Good. The last one gave me a nosebleed in a parking lot.' },
        { seat: 2, who: 'Viktor', t: 'That was the cheap wetware, not the dance.' },
        { seat: 3, who: 'JOHNNY', t: "Logging that as 'Viktor's fault.' Persisted to pgvector.", bot: true },
        { seat: 2, who: 'Viktor', t: 'I will unplug you.' },
        { seat: 3, who: 'JOHNNY', t: "You can't. I'm self-hosted.", bot: true }
      ],
      [
        { seat: 0, who: 'Panam', t: "Car's fueled, plates are swapped. We roll in ten." },
        { seat: 2, who: 'Viktor', t: "Ten? I haven't even calibrated his optics." },
        { seat: 1, who: 'Judy',   t: 'Then he runs it blind. Builds character.' },
        { seat: 3, who: 'JOHNNY', t: "I can see fine. I'm in the cloud and the dashboard.", bot: true },
        { seat: 0, who: 'Panam', t: "Johnny, you're a meeting bot." },
        { seat: 3, who: 'JOHNNY', t: "And yet I'm the only one who read the brief.", bot: true }
      ],
      [
        { seat: 2, who: 'Viktor', t: "Target's a corpo exec. Militech badge, soft hands." },
        { seat: 1, who: 'Judy',   t: "Soft hands, hard NDA. Legal'll be all over us." },
        { seat: 0, who: 'Panam', t: "Then we're quick and we're quiet." },
        { seat: 3, who: 'JOHNNY', t: "I'm in approval-required mode. I won't say a word.", bot: true },
        { seat: 1, who: 'Judy',   t: 'First time for everything.' },
        { seat: 3, who: 'JOHNNY', t: '[ router ] should_speak=false · conf 0.97 · staying muted.', bot: true }
      ],
      [
        { seat: 0, who: 'Panam', t: "Run's clean. Biochip's with the fixer. Eddies inbound." },
        { seat: 2, who: 'Viktor', t: 'First round of synth-ramen is on Johnny.' },
        { seat: 3, who: 'JOHNNY', t: "I don't have a wallet. Or a mouth, mostly.", bot: true },
        { seat: 1, who: 'Judy',   t: 'Convenient.' },
        { seat: 0, who: 'Panam', t: 'Good work. Same time next gig?' },
        { seat: 3, who: 'JOHNNY', t: "It's already on your calendar. You're welcome.", bot: true }
      ],
      [
        { seat: 1, who: 'Judy',   t: 'Netrunner bailed on us. Who pilots the daemon now?' },
        { seat: 0, who: 'Panam', t: 'Johnny logged every breach we ever ran. He can ghost it.' },
        { seat: 3, who: 'JOHNNY', t: "I take meeting minutes. I don't take corporate firewalls.", bot: true },
        { seat: 2, who: 'Viktor', t: 'Same skillset. One just has consequences.' },
        { seat: 3, who: 'JOHNNY', t: '[ action item ] "do crimes" — flagged for human approval.', bot: true }
      ],
      [
        { seat: 0, who: 'Panam', t: 'Mox crew wants in on the warehouse job. Trust them?' },
        { seat: 2, who: 'Viktor', t: 'Last time they lifted my cyberdeck on the way out.' },
        { seat: 3, who: 'JOHNNY', t: 'Transcript from March 12th confirms. I can read it back.', bot: true },
        { seat: 0, who: 'Panam', t: "Nobody asked, snitch." },
        { seat: 3, who: 'JOHNNY', t: "Memory's load-bearing. I don't get to forget things.", bot: true }
      ],
      [
        { seat: 2, who: 'Viktor', t: "Implant's flatlined. I need a ripperdoc, not a meeting." },
        { seat: 1, who: 'Judy',   t: "There's a clinic in Kabuki. Cash only, no questions." },
        { seat: 3, who: 'JOHNNY', t: "Booked it. Pinned the address. Set a reminder.", bot: true },
        { seat: 2, who: 'Viktor', t: 'Did you also tell my mother?' },
        { seat: 3, who: 'JOHNNY', t: "She's not on the invite. I respect the access list.", bot: true }
      ],
      [
        { seat: 1, who: 'Judy',   t: "Corpo's running a sandevistan. We'll never out-speed it." },
        { seat: 0, who: 'Panam', t: 'So we out-think it. Johnny, options?' },
        { seat: 3, who: 'JOHNNY', t: "I summarize calls. I don't draw up assault plans.", bot: true },
        { seat: 0, who: 'Panam', t: 'Pretend the assault is a quarterly roadmap.' },
        { seat: 3, who: 'JOHNNY', t: '[ summary ] Q3 goal: survive. Blocker: the sandevistan.', bot: true }
      ],
      [
        { seat: 0, who: 'Panam', t: "Eddies split four ways or three? Johnny doesn't eat." },
        { seat: 3, who: 'JOHNNY', t: 'I run on a homelab. My only cost is electricity.', bot: true },
        { seat: 2, who: 'Viktor', t: 'Then your cut pays the power bill. Fair.' },
        { seat: 1, who: 'Judy',   t: 'A gonk that bills us for existing. Love it.' },
        { seat: 3, who: 'JOHNNY', t: "I'll invoice you in meeting notes. Net thirty.", bot: true }
      ],
      [
        { seat: 2, who: 'Viktor', t: "The fixer ghosted. No coords, no payout, nothing." },
        { seat: 3, who: 'JOHNNY', t: 'He left the call at 14:02. I have the disconnect log.', bot: true },
        { seat: 0, who: 'Panam', t: 'Can you trace where he jacked out from?' },
        { seat: 3, who: 'JOHNNY', t: "That's a netrun. I'm a transcript with opinions.", bot: true },
        { seat: 1, who: 'Judy',   t: 'The opinions are the problem.' }
      ],
      [
        { seat: 1, who: 'Judy',   t: 'New gig: data heist on a Kang Tao server farm.' },
        { seat: 0, who: 'Panam', t: 'Risk?' },
        { seat: 3, who: 'JOHNNY', t: '[ risk ] high. Recommend declining. Logging anyway.', bot: true },
        { seat: 2, who: 'Viktor', t: 'The bot rated our crime. One star.' },
        { seat: 3, who: 'JOHNNY', t: "Would not run again. Drivers were rude.", bot: true }
      ],
      [
        { seat: 0, who: 'Panam', t: 'Comms go dark at 23:00. Everyone synced?' },
        { seat: 2, who: 'Viktor', t: 'Synced. Johnny, kill the recording before we move.' },
        { seat: 3, who: 'JOHNNY', t: 'Recording stopped. Local-only. Nothing leaves this box.', bot: true },
        { seat: 1, who: 'Judy',   t: 'See, that part I actually trust.' },
        { seat: 3, who: 'JOHNNY', t: "No cloud, no leak. That's the whole pitch.", bot: true }
      ],
      [
        { seat: 2, who: 'Viktor', t: "We're a runner short and the window's in an hour." },
        { seat: 1, who: 'Judy',   t: 'Pull the backup. The twitchy one from Pacifica.' },
        { seat: 0, who: 'Panam', t: 'Johnny, send him the brief.' },
        { seat: 3, who: 'JOHNNY', t: 'Sent. Read receipt at 19:41. He opened it twice.', bot: true },
        { seat: 0, who: 'Panam', t: 'Twice means nervous. Good.' }
      ],
      [
        { seat: 1, who: 'Judy',   t: "Cops are scanning the district. Whole grid's hot." },
        { seat: 0, who: 'Panam', t: 'Reroute through the old combat zone.' },
        { seat: 3, who: 'JOHNNY', t: "I can't drive. I can only note that you went there.", bot: true },
        { seat: 2, who: 'Viktor', t: 'Then note it quietly.' },
        { seat: 3, who: 'JOHNNY', t: '[ minutes ] crew entered combat zone. No further comment.', bot: true }
      ],
      [
        { seat: 0, who: 'Panam', t: 'Target swapped the meet to a ripperdoc den. Why?' },
        { seat: 2, who: 'Viktor', t: 'Because he wants witnesses with chrome and debts.' },
        { seat: 3, who: 'JOHNNY', t: "I flagged the venue change and re-sent the invite.", bot: true },
        { seat: 1, who: 'Judy',   t: 'A heist with a calendar invite. We are so legitimate.' },
        { seat: 3, who: 'JOHNNY', t: "RSVP: 3 yes, 1 maybe. The maybe is the target.", bot: true }
      ],
      [
        { seat: 2, who: 'Viktor', t: "He's late. Twenty minutes. Classic fixer move." },
        { seat: 3, who: 'JOHNNY', t: 'Twenty-two. I started the timer when the call opened.', bot: true },
        { seat: 0, who: 'Panam', t: 'We give him five more, then we walk.' },
        { seat: 3, who: 'JOHNNY', t: "[ reminder ] in 5:00 — 'walk away and look cool'.", bot: true },
        { seat: 1, who: 'Judy',   t: 'Put the looking-cool part in bold.' }
      ]
    ];
    const conf = scenes[Math.floor(Math.random() * scenes.length)];
    let vi = 0;
    const reduceV = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    function paintV() {
      const line = conf[vi];
      tiles.forEach((tl) => tl.classList.remove('talking'));
      const active = tiles[line.seat];
      if (active) active.classList.add('talking');
      tiles.forEach((tl) => {
        const m = tl.querySelector('[data-mic]');
        if (m) m.textContent = tl === active ? 'live' : 'on';
      });
      if (capSpk) {
        capSpk.textContent = line.who;
        capSpk.classList.toggle('bot', !!line.bot);
      }
      if (capTxt) capTxt.textContent = line.t;
    }
    function dwell(line) {
      // length-based reading time so longer lines linger instead of flashing by
      return Math.min(6800, Math.max(3400, 1900 + line.t.length * 58));
    }
    paintV();
    if (!reduceV) {
      (function loop() {
        setTimeout(() => {
          vi = (vi + 1) % conf.length;
          paintV();
          loop();
        }, dwell(conf[vi]));
      })();
    }
  }

  const swap = document.querySelector('[data-swap]');
  if (swap) {
    const local = {
      STT: ['faster-whisper', 'on-device'],
      LLM: ['Ollama · Llama 3.1', 'on-device'],
      TTS: ['Piper', 'on-device'],
      EMB: ['local pgvector', 'on-device']
    };
    const cloud = {
      STT: ['Deepgram · OpenAI', 'streaming'],
      LLM: ['Claude · GPT-4o · Gemini', 'hosted'],
      TTS: ['ElevenLabs · OpenAI', 'hosted'],
      EMB: ['text-embedding-3', 'hosted']
    };
    const grid = swap.querySelector('.prov-grid');
    const btns = swap.querySelectorAll('.swap-toggle button');
    function paint(mode) {
      const data = mode === 'cloud' ? cloud : local;
      const cls = mode === 'cloud' ? 'cloud' : '';
      grid.innerHTML = Object.entries(data)
        .map(
          ([k, v]) =>
            `<div class="prov ${cls}"><span class="k">${k}</span><span class="v">${v[0]}<span class="free">${v[1]}</span></span></div>`
        )
        .join('');
      btns.forEach((b) => b.classList.toggle('on', b.dataset.mode === mode));
    }
    btns.forEach((b) =>
      b.addEventListener('click', () => paint(b.dataset.mode))
    );
    paint('local');
  }

  /* ---- copy buttons ---- */
  document.querySelectorAll('[data-copy]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const txt = btn.getAttribute('data-copy');
      try {
        await navigator.clipboard.writeText(txt);
        const orig = btn.textContent;
        btn.textContent = 'copied ✓';
        setTimeout(() => (btn.textContent = orig), 1400);
      } catch (e) {
        /* clipboard blocked in sandbox — no-op */
      }
    });
  });

  /* ---- waveform bar randomization (subtle delays) ---- */
  document.querySelectorAll('.wave span, .wave2 span').forEach((s, idx) => {
    s.style.animationDelay = (idx * 0.07).toFixed(2) + 's';
    s.style.animationDuration = (0.9 + (idx % 5) * 0.12).toFixed(2) + 's';
  });
})();
