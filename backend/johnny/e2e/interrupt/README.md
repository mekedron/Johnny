# Interrupt-reproduction harness (`johnny.e2e.interrupt`)

This harness drives the production voice pipeline against synthetic
speaker timelines and asserts the latency-budget contract that
voice-interrupt must satisfy. Originally shipped for the split
(STT+LLM+TTS) pipeline (Johnny-2bw, Johnny-ckz.4), extended in
Johnny-ckz.22 to cover the unified S2S pipeline (OpenAI GPT-Realtime,
Gemini Live).

## Quick start

```bash
# 1. Stack must be up so the pytest container has all provider adapters
#    available (image is COPY-baked from backend/Dockerfile):
./run-dev.sh

# 2. Drop into the worker container (bind-mounts ./backend so source
#    edits land instantly):
docker compose exec -w /workspace worker bash

# 3. Run scenarios. Examples below run inside the container.
```

## Modes

### Split pipeline (scripted providers) — the legacy harness

Scripted STT/LLM/TTS shims return preset transcripts/decisions/PCM so
the assertions are deterministic and the suite runs in ~10 seconds.

```bash
uv run python -m johnny.e2e.interrupt
```

This exercises the four classic scenarios:

| Scenario | What it asserts |
| --- | --- |
| `stop_interrupts_long_answer` | Speaker says "stop" mid-monologue → bot's TTS cut within 500 ms, no follow-up utterance. |
| `clarification_redirects_long_answer` | Speaker asks a new question mid-monologue → bot cuts THEN follows up addressing the new question. |
| `stt_keeps_running_during_bot_speech` | The Johnny-har contract: side-chat mid-bot-utterance still lands in `transcript_chunks`. |
| `cough_does_not_interrupt` | Sub-160 ms transient does NOT trigger fast barge-in. |

### Split pipeline (real STT/LLM/TTS adapters)

Same scenarios driven through real adapters from a `providers.json`
file (the same shape the API seeder consumes). Real STT will sometimes
add punctuation or split utterances — the runner uses fuzzy
keyword-based assertions.

```bash
uv run python -m johnny.e2e.interrupt --real \
    --providers-file /path/to/providers.json
```

ElevenLabs out of credits? The escape hatch synthesises an OpenAI TTS
adapter from the OpenAI LLM api_key:

```bash
uv run python -m johnny.e2e.interrupt --real \
    --providers-file providers.json --fallback-tts-openai
```

### Unified S2S pipeline (real provider)

A single `S2SProvider` collapses STT+LLM+TTS into one bidirectional
session. The harness reports **barge-in cancellation latency** — time
from the runner triggering the interrupt to `S2SResponseCompleted`
arriving — alongside the standard PASS/FAIL assertions.

```bash
# OpenAI GPT-Realtime (uses OPENAI_API_KEY from env):
uv run python -m johnny.e2e.interrupt --mode=unified \
    --provider=openai-realtime

# Gemini Live (uses GEMINI_API_KEY or GOOGLE_API_KEY from env):
uv run python -m johnny.e2e.interrupt --mode=unified \
    --provider=gemini-live
```

S2S scenarios:

| Scenario | What it asserts |
| --- | --- |
| `s2s_open_and_receive_audio` | Smoke: open session, send tone+commit, receive ≥1 audio frame + a `S2SResponseCompleted`. |
| `s2s_barge_in_via_session_interrupt` | Mid-response `pipeline.interrupt()` (= `response.cancel` + `input_audio_buffer.clear` on OpenAI Realtime) yields a completion within 3 s. |
| `s2s_barge_in_via_new_user_turn` | Mid-response fresh `send_audio` + `commit_user_turn` triggers a clean completion within 3 s. Works on both adapters; the only interrupt path Gemini Live supports. |

### Surface flag

```bash
--surface=meet        # default; annotates the report with the Meet entry-point shape
--surface=playground  # annotates the report with the in-process browser entry-point shape
```

The underlying pipeline classes (`VoicePipeline`, `UnifiedVoicePipeline`)
are identical across both surfaces — the flag stamps the report so
operators can audit "playground + Meet parity" from a single output.

### Single scenario

```bash
uv run python -m johnny.e2e.interrupt --only stop_interrupts_long_answer

uv run python -m johnny.e2e.interrupt --mode=unified \
    --provider=openai-realtime \
    --only s2s_barge_in_via_session_interrupt
```

## Output

* Console: PASS/FAIL summary plus per-assertion lines.
* JSON report: `tests/e2e/artifacts/<UTC-timestamp>-<label>/report.json`
  with full per-scenario diagnostics. Label suffix:
  * `-interrupt` for split-mode runs.
  * `-interrupt-s2s-<provider>-<surface>` for unified-mode runs.
* Exit code: 0 if every scenario passed; 1 otherwise.

The JSON report carries `scenarios[].interrupt_to_cut_ms` — pluck it
out for trend tracking across runs.

## Adapter-specific barge-in semantics

The harness exercises both client-side cancel paths uniformly via
`pipeline.interrupt()` / fresh user turn. The adapter-side behaviour
differs:

* **OpenAI GPT-Realtime** — `session.interrupt()` sends BOTH
  `response.cancel` (stops generation immediately) AND
  `input_audio_buffer.clear` (drops pending uncommitted user audio).
  Server replies with `response.done` carrying `status: "cancelled"`,
  surfaced as `finish_reason="interrupted"`. Confirmed against the GA
  API at fetch date 2026-06-07.
* **Gemini Live** — no client-side cancel exists in the protocol.
  `session.interrupt()` sends `realtimeInput.activityEnd: {}` as a
  soft hint, but the real cancellation only happens when the server's
  VAD detects a fresh user turn (auto VAD) or the client sends a fresh
  `activityStart` (manual VAD). The `s2s_barge_in_via_new_user_turn`
  scenario is the canonical Gemini barge-in shape. Confirmed at fetch
  date 2026-06-07.

The runner accepts either `finish_reason="interrupted"` or `"stop"`
for the last completion in interrupt scenarios — the latency budget
is the load-bearing assertion; the reason is informational and
adapter-dependent.
