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
