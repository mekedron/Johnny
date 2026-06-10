# johnny.agent fixtures

Binary fixtures that ship **inside the api/agent image** (`COPY johnny ./johnny`
in `backend/Dockerfile`) so in-image smokes can run on a clean install with no
network and no `tests/` tree (the prod image excludes `tests/` via
`.dockerignore`).

## sdk_smoke_speech.pcm

Real-speech sample used by `johnny.agent.sdk_surface_smoke` (Johnny-trt.2) to
drive the Silero VAD on a roomless `AgentSession`.

- Format: 16 kHz mono S16LE raw PCM (≈1.97 s, 63 184 bytes).
- Provenance: byte-for-byte copy of
  `backend/tests/e2e/interrupt/fixtures/speech/0631cead92bace05864b21b431bc08bd.pcm`
  (TTS render of "wait, what about the launch date?", voice `alloy`).
- Why a real sample: Silero VAD is a trained speech classifier — DSP-synthetic
  audio (white noise, formant-shaped harmonic "vowels" with syllable-rate AM)
  does **not** trigger it (verified empirically in-image on
  `livekit-agents==1.5.17`, 2026-06-10: real speech → 1 START/END pair,
  synthetic candidates → zero events). Any smoke that needs the *real* VAD to
  fire must push real speech.

## latency_turn_{short,medium,long}{1..8}.pcm — 24 files

Bot-addressed speech fixtures for the scripted latency harness
(`johnny.agent.latency_harness`, Johnny-trt.1): the **full Johnny-cxu
24-utterance set**, one fixture per utterance (8 short ≈1.8–3.0 s, 8 medium
≈3.1–4.3 s, 8 long ≈6.7–8.6 s). The harness cycles all 24 by default,
short/medium/long interleaved. `--providers local` needs the texts to be
bot-addressed AND distinct: the real router declines utterances that aren't
for the bot and repeats of already-answered questions ("already answered" —
observed live: a 3-fixture cycle got 17/24 turns repeat-declined), either of
which leaves no reply stages to time. With 24 distinct texts a default-length
local run replies on nearly every turn; runs past 24 turns recycle and will
see repeat declines.

- Format: 16 kHz mono S16LE raw PCM.
- Provenance: synthesized 2026-06-10 with the in-image piper CLI
  (`en_US-amy-medium` — the same voice as the Johnny-cxu fake-mic WAV, distinct
  from the bot's en_GB voice), internal quiet runs capped at 0.25 s, edges
  trimmed (60 ms pad), linearly resampled 22 050 → 16 000 Hz. Texts verbatim
  from the Johnny-cxu utterance set (`.validation/Johnny-cxu/gen_fake_mic.py`)
  with internal sentence punctuation comma-ized (see contract below). Generator
  + verifier preserved in `.validation/Johnny-trt.1/gen_latency_fixtures.py`
  and `verify_latency_fixtures.py` (run both via
  `docker compose run --rm -T --no-deps -v "$PWD/backend:/workspace" api
  python - < <script>`).
- **Contract: each fixture must read as ONE Silero VAD utterance** (exactly one
  START/END pair) **at the browser session's 0.40 s silence floor**
  (`BROWSER_VAD_MIN_SILENCE_DURATION_S`, Johnny-trt.5 — the harness's default
  VAD since then; it was Silero's 0.55 s default before) — the harness pushes
  one fixture per turn and waits for that turn's terminal, so a fixture the VAD
  splits becomes two engine turns and the second one barges into the first
  one's reply. This is why every internal sentence boundary is comma-ized:
  piper inserts its sentence pause at sentence punctuation, and Silero treats
  that pause (which the model scores as non-speech well beyond the energy-quiet
  stretch) as a > 0.55 s end-of-speech; a two-sentence variant read as two
  pairs even with internal quiet squeezed to 0.30 s. Verified in-image
  2026-06-10 (`livekit-agents==1.5.17`): all 24 fixtures → exactly one
  START/END pair each, at the 0.55 s default **and** at the 0.40 s browser
  floor (`.validation/Johnny-trt.5/verify_fixtures_at_floor.py` — gate any new
  fixture at 0.40).
- Piper TTS speech reliably trips Silero VAD + real STT (verified in
  Johnny-cxu), unlike DSP synthetics — see the sdk_smoke_speech.pcm note above.
