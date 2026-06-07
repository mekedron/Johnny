# Local TTS Runtimes — How They Work, How to Configure, What to Pick

Johnny ships three local text-to-speech providers — **Piper**, **KittenTTS**,
and **Kokoro** — and each one exposes a **Runtime** picker that decides *where*
and *how* synthesis happens without changing the voice you hear. This page is
the long-form companion to the per-provider tables in the
[README](../README.md#local-tts-providers) and to the cross-provider latency
methodology in [LATENCY.md](LATENCY.md).

TTS pays its cost on **every** conversation turn (unlike STT, which is loaded
once and then transcribes streamingly), so the runtime you pick is the single
biggest lever on how fast Johnny starts speaking. Read the
[comparison table](#3-comparison-table) first if you just want a recommendation.

> **Validate latency yourself in the browser.** Numbers below were measured on
> one machine; yours will differ. The authoritative per-call measurement is the
> **Play sample** badge in Settings → Providers → *{provider}* (drive it with
> the **chrome-devtools MCP** server per
> [CLAUDE.md](../CLAUDE.md)) plus the structured INFO log line the api emits for
> that click — see [Reading the structured logs](#reading-the-structured-logs).
> Treat the log line, not your stopwatch, as the source of truth.

---

## 1. The runtime picker pattern

Every local STT/TTS adapter in Johnny shares the same three-way split. The
canonical implementation is the Parakeet STT adapter
(`backend/app/providers/parakeet_stt.py`) — read it if you want to see the
pattern end-to-end; the TTS adapters mirror its shape exactly (a `runtime`
SELECT field + a `sidecar_url` URL field, both auto-rendered by the
schema-driven `/providers` form).

The three runtime *kinds*:

1. **In-container (default).** Synthesis runs inside the `api` container, in the
   api process itself. The model is lazy-imported on first use and the warm
   model/voice is cached at module scope, so the first call per voice (or per
   language) pays the load cost and every later call is warm. No host setup, no
   extra process — but CPU-only, because the arm64 Linux container has no access
   to the host's Apple MLX / Metal GPU / Neural Engine. For Piper this runtime
   is named **`persistent-subprocess`** (historical name — it is in-process, not
   a child process); for KittenTTS and Kokoro it is **`in-container`**.

2. **In-container optimised baseline.** A deliberately un-optimised variant that
   trades latency for isolation/debuggability. Only Piper has one:
   **`subprocess`** spawns a fresh `piper` CLI per call so every turn is a clean
   cold start — the safe single-step-debug default, and bit-for-bit identical to
   Johnny's historical behaviour. KittenTTS and Kokoro have no separate
   baseline; their `in-container` runtime already keeps the model warm.

3. **Out-of-container sidecar.** Synthesis runs in a **native macOS host
   process** outside Docker, and the api POSTs text to it over
   `http://host.docker.internal:<port>`. This is the only way to reach hardware
   the container can't — Apple MLX (Metal GPU) for Kokoro's `mlx-sidecar` — and
   it also keeps heavy dependencies (torch, onnxruntime) out of the api image or
   isolates synthesis on another box. All of Johnny's TTS sidecars speak the
   **same wire protocol** (`POST /synthesize` JSON in → raw PCM + `X-Sample-Rate`
   out), so one api-side adapter method drives every sidecar runtime a provider
   has.

**Why a provider may have two runtimes instead of three:** the count follows
what the underlying library actually supports, not a fixed template. Piper has
all three. Kokoro has `in-container` + `mlx-sidecar` + `http-sidecar` (three,
because it has an MLX build). KittenTTS has only `in-container` + `http-sidecar`
(two: it ships no CLI to drive a separate baseline, and no MLX/CoreML build, so
there is no GPU sidecar — it is CPU-only everywhere).

Switching runtime in the UI: **Settings → Providers → *{provider}* → Runtime**.
The picker re-renders a **Sidecar URL** field (used only by sidecar runtimes)
and a live **sidecar running / offline** badge that probes the configured URL
the moment you pick a sidecar runtime, so you see reachability *before* you click
Test or Play sample.

---

## 2. Per-provider guide

### Piper

[Piper](https://github.com/rhasspy/piper) is the default and most mature local
TTS. Hundreds of voices across dozens of languages, each shipped as a small
`.onnx` + `.onnx.json` pair you install per-voice from the picker. Voices come in
three quality tiers: **low** (16 kHz output), **medium** (22.05 kHz,
recommended), and **high** (22.05 kHz, +quality, +latency). The adapter
resamples every voice to the canonical 16 kHz mono bridge format.

The Piper library is **baked into the api image** (the `local-tts` extra), so the
`subprocess` and `persistent-subprocess` runtimes work out of the box with no
host setup.

| Runtime | Where | Setup | What changes when you switch |
| --- | --- | --- | --- |
| `subprocess` (default) | api container, fresh `piper` CLI per call | none | Pays ONNX cold-start **every** turn. Safe debug baseline. |
| `persistent-subprocess` | api container, warm in-process `PiperVoice` (ONNX session) cached at module scope | none | First synth per voice pays the ~700 ms load; later turns are warm (~40 ms). **Pick this for real meetings.** |
| `http-sidecar` | macOS host, `sidecars/piper-http/server.py` on :8775 | `./scripts/start-piper-sidecar.sh start`, set **Sidecar URL** | Adds a network round-trip but isolates synthesis from the api process. |

> piper-tts 1.x (the Python rewrite in the `local-tts` extra) dropped the old
> C++ piper's `--json-input` streaming CLI **and** its `--http` server. So
> `persistent-subprocess` keeps `PiperVoice` warm **in-process** rather than
> feeding a long-lived child, and `http-sidecar` is a thin FastAPI wrapper around
> the same library. See `sidecars/piper-http/README.md`.

### KittenTTS

[KittenTTS](https://github.com/KittenML/KittenTTS) is a tiny (<25 MB) Apache-2.0
ONNX model with **eight English voices** — four female (`Bella`, `Luna`,
`Rosie`, `Kiki`) and four male (`Jasper`, `Bruno`, `Hugo`, `Leo`). All voices
live inside the one model, so switching voice is instant and needs no install. It
is the smallest-footprint local option, complementary to Piper's larger voices.
Native output is 24 kHz float; the adapter converts to S16LE and resamples to
16 kHz.

| Runtime | Where | Setup | What changes when you switch |
| --- | --- | --- | --- |
| `in-container` (default) | api container, `kittentts` in the api process; the loaded model (all voices) cached at module scope keyed by `model_id` | requires the `kittentts` wheel in the api image | First synth pays the one-off model load; later turns warm. CPU-only. |
| `http-sidecar` | macOS host, `sidecars/kitten-tts/server.py` on :8771 | `./scripts/start-kitten-sidecar.sh start`, set **Sidecar URL** | Keeps `onnxruntime` out of the api image, or isolates synthesis on another host. |

KittenTTS's `model.generate()` returns the **whole** utterance as one array (not
a streaming generator), so time-to-first-audio ≈ total synth time — there is no
"first chunk early" win. Fine for such a small model on short utterances.

### Kokoro

[Kokoro](https://github.com/hexgrad/kokoro) is an 82 M-parameter Apache-2.0
model with **41 voices across nine languages** (American & British English, plus
Spanish, French, Hindi, Italian, Japanese, Brazilian Portuguese, and Mandarin
Chinese). Every voice lives in the single checkpoint, so switching voice is
instant and needs no per-voice install — but the **language** is what drives a
model (re)load: the adapter caches the warm pipeline keyed by `(model, language)`
because the checkpoint is shared across a language's voices. Native output is
24 kHz float, resampled to 16 kHz. Voice ids encode language + gender in the
first two letters (`af`/`am` American female/male, `bf`/`bm` British, etc.).

| Runtime | Where | Setup | What changes when you switch |
| --- | --- | --- | --- |
| `in-container` (default) | api container, `kokoro` in the api process; warm `KPipeline` cached keyed by `(model, language)` | requires `pip install kokoro` in the api image (torch-heavy) | First synth per language pays the load; later turns warm. CPU-only inside the container. |
| `mlx-sidecar` | macOS host, Kokoro under Apple MLX (Metal GPU) via `mlx-audio`, `sidecars/kokoro-mlx/server.py` on :8772 | `./scripts/start-kokoro-sidecar.sh start mlx`, set **Sidecar URL** | The GPU-accelerated path. Needs `mlx-audio` working on the host. |
| `http-sidecar` | host process (CPU, or a CUDA box), upstream Kokoro, `sidecars/kokoro-http/server.py` on :8773 | `./scripts/start-kokoro-sidecar.sh start http`, set **Sidecar URL** | The non-MLX out-of-container path. |

> Non-English Kokoro voices use **espeak-ng** for grapheme-to-phoneme, so install
> it on whichever host runs the model: `brew install espeak-ng`.

---

## 3. Comparison table

**Methodology.** Measured on an Apple M-series Mac (16 GB) on 2026-06-07 against
the running Docker stack, driving `POST /providers/{id}/play_sample` (and the
keyless `/providers/preview/play_sample` for Kokoro) with a runtime override.
Sample text was identical across every cell (`text_chars=67`); every runtime
resamples to 16 kHz mono S16LE. TTFA / total are the median of three reps, read
from the `X-TTS-TTFA-Ms` / `X-TTS-Total-Ms` response headers (the same numbers
the **Play sample** badge shows). "Cold" is the first call after an api restart
(or first call to a freshly-started sidecar); "warm" is steady state.

Only **Piper `persistent-subprocess`** streams the first chunk early, so its TTFA
is decoupled from phrase length (~40 ms regardless of how long the utterance is).
Every other path here returns the **whole** PCM before the first byte — the
`subprocess` CLI writes a complete WAV, sidecars POST back the full buffer, and
atomic models (`generate()`) hand back one array — so for those, TTFA ≈ total ≈
*full synth of the 67-char sample*. That is why `subprocess` shows ~930 ms end to
end even though the ONNX cold-start alone (what the in-UI tip cites as
~200–400 ms) is only one part of it.

| Provider · runtime | TTFA cold | TTFA warm | Audible? (peak) | Quality / language / rate | Install complexity | Loaded footprint | When to pick |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **Piper · subprocess** | ~930 ms | ~930 ms (no warm state) | ✅ 0.99 | Hundreds of voices, many languages; 16/22.05 kHz native | None (baked in) | One ONNX session per call, freed after | Debugging, or a box where you never repeat-synthesise |
| **Piper · persistent-subprocess** | ~930 ms (incl. ~700 ms load) | **~40 ms** | ✅ 0.99 | same as above | None (baked in) | One warm `PiperVoice` (ONNX session) per (voice, rate); voice file 16–63 MB on disk | **Real meetings** — the local-stack default win |
| **Piper · http-sidecar** | ~90 ms + first-load | ~90 ms | ✅ 0.99 | same as above | `uv` venv on host | One warm voice in the sidecar process | Process isolation, or future macOS-native voices |
| **KittenTTS · in-container** | model load | <200 ms target¹ | wheel needed¹ | 8 English voices; 24 kHz native | `kittentts` wheel baked into api image | One model (all voices) ≈ <25 MB | Smallest footprint, fully in-container |
| **KittenTTS · http-sidecar** | ~1.8 s² | ~1.8 s² | ✅ 0.51 | 8 English voices; 24 kHz native | `uv` venv on host (GitHub-release wheel) | One model in the sidecar | Keep onnxruntime out of the api image |
| **Kokoro · in-container** | model load | <200 ms target¹ | lib needed¹ | 41 voices / 9 languages; 24 kHz native | `pip install kokoro` (torch) in api image | One `KModel` per language ≈ 82 M params | Multilingual, fully in-container, no GPU |
| **Kokoro · mlx-sidecar** | host-dependent³ | host-dependent³ | mlx-audio needed³ | 41 voices / 9 languages; 24 kHz | `mlx-audio` on Apple-Silicon host | One model on the host GPU | Fastest Kokoro on Apple Silicon |
| **Kokoro · http-sidecar** | ~1.7 s | **~425 ms** | ✅ 0.37 | 41 voices / 9 languages; 24 kHz | `kokoro` on host (+ espeak-ng for non-EN) | One model in the sidecar | Kokoro on a CPU/CUDA host outside Docker |

¹ **In-container KittenTTS and Kokoro need their library baked into the api
image**, which the default image does **not** include (kittentts is a
GitHub-release wheel; kokoro is torch-heavy). Without it the runtime returns a
clear `502 … library not importable` and the Play sample badge surfaces it — the
`<200 ms` warm figure is the cache-shape target the unit tests pin, not a number
measured on the stock image. Bake the dependency in (or run the sidecar) to get
real audio. See [Troubleshooting](#5-troubleshooting).

² KittenTTS synthesises the **whole** utterance before returning (atomic
`generate()`), so TTFA ≈ total and scales with phrase length. The 67-char sample
renders ~7.7 s of audio in ~1.8 s; a short meeting utterance is proportionally
faster.

³ The `mlx-sidecar` was reachable on the test host but `mlx-audio`'s Kokoro path
produced no `.wav` output (a known mlx-audio API-drift gap — the sidecar's
non-empty assertion correctly 500s instead of returning silence). The wire
protocol is stable; the sidecar internals can be adjusted on the host. Numbers
will be host-specific once it generates.

**Quick recommendation:** for a real all-local meeting stack, **Piper ·
persistent-subprocess** is the default — sub-100 ms warm TTFA, zero host setup,
hundreds of voices. Reach for **Kokoro** when you need multilingual or its
cleaner prosody (sidecar on Apple Silicon for speed), and **KittenTTS** when
footprint is the priority.

---

## 4. Sidecar lifecycle

All sidecars are auto-started by `./run.sh` (and stopped by `./stop.sh`), so a
saved sidecar runtime usually just works. To manage one by hand:

```bash
./scripts/start-piper-sidecar.sh  start    # piper on :8775
./scripts/start-kitten-sidecar.sh start    # kittentts on :8771
./scripts/start-kokoro-sidecar.sh start mlx   # kokoro MLX on :8772
./scripts/start-kokoro-sidecar.sh start http  # kokoro generic on :8773
# every launcher also accepts: stop | restart | status | logs | --help
./scripts/start-sidecars.sh status         # one line per sidecar across all providers
```

Every launcher shares one CLI contract (commands, env vars
`<PROVIDER>_<BACKEND>_PORT/_HOST/_MODEL`, exit codes `0/1/2/3/4`, logs + PIDs
under `.validation/<provider>-<backend>-sidecar.{log,pid}`). See the
[README sidecar-management section](../README.md#sidecar-management) for the full
contract.

**Health-check each sidecar directly** (all return `{"ready": true, ...}` once
the default voice/model has loaded):

```bash
curl http://localhost:8775/health   # piper
curl http://localhost:8771/health   # kittentts
curl http://localhost:8772/health   # kokoro mlx
curl http://localhost:8773/health   # kokoro http
```

Or ask the api for every known sidecar at once (this is what the UI badge uses):

```bash
curl -s http://localhost:8000/sidecars/health | python3 -m json.tool
# → [{"name":"piper-http","url":"http://host.docker.internal:8775","ok":true,"latency_ms":5.6,...}, ...]
```

### Reading the structured logs

Every synth click emits one structured INFO line to `docker compose logs api`.
This is the **source of truth** for which runtime actually ran and how long it
took — quote it, not a stopwatch:

```bash
docker compose logs -f api | grep -E '(piper|kitten|kokoro)\.(synth|sidecar):'
```

Real lines from the measurement run behind the table above:

```
piper.synth:   runtime=persistent-subprocess voice=en_GB-cori-medium text_chars=67 ttfa_ms=34 total_ms=222
piper.sidecar: action=request url=http://host.docker.internal:8775/synthesize status=200 ms=80
piper.synth:   runtime=http-sidecar voice=en_GB-cori-medium text_chars=67 ttfa_ms=92 total_ms=92
kitten.synth:  runtime=http-sidecar voice=Bella text_chars=67 ttfa_ms=1744 total_ms=1744
kokoro.synth:  runtime=http-sidecar voice=af_heart text_chars=67 ttfa_ms=430 total_ms=430
```

Field guide: `runtime=` confirms which path served the call (catches a silent
fallback); `ttfa_ms` is time-to-first-audio, `total_ms` is full synth; a
`.sidecar:` line with `status=` + `ms=` is the api↔host HTTP leg. A failed
in-container synth logs `ttfa_ms=-1 total_ms=0` — that means **no audio was
produced**, look at the `502` detail.

---

## 5. Troubleshooting

**Sidecar unreachable / Play sample 502.** The badge says "sidecar offline" or
the call returns `502 … sidecar unreachable`. Start it
(`./scripts/start-<provider>-sidecar.sh start [<backend>]`), confirm with
`curl http://localhost:<port>/health`, and check the **Sidecar URL** field points
at `http://host.docker.internal:<port>` (from *inside* the api container,
`localhost` is the container, not your Mac).

**`library not importable` (KittenTTS / Kokoro in-container).** The default api
image does not bundle the `kittentts` wheel or the torch-heavy `kokoro` package.
Either switch the runtime to that provider's sidecar (recommended — no image
rebuild), or bake the dependency into the api image and rebuild. The error body
names the exact `pip install` to run.

**Runtime silently fell back / wrong runtime served.** Trust the `runtime=` field
in the `*.synth:` log line over what you think you selected. If it disagrees with
the Runtime picker, you're looking at a stale saved row — re-Save the provider.

**Audio crackle / wrong sample rate / chipmunk speed.** For Piper, the **Native
sample rate** must match the voice file (22050 for medium/high, 16000 for low);
a mismatch makes audio chipmunk-fast or sluggish *before* resampling adds
artefacts. The voice picker sets it for you on install. All providers resample to
16 kHz internally; if downstream audio is wrong-pitched, the native rate is the
first knob to check.

**Silent output (200 OK but nothing plays).** A runtime can return `200` with
empty / all-zero PCM. The Play sample badge warns ("not audible") and
`X-TTS-Audible: 0` rides back on the headers. Prove every cell end-to-end with:

```bash
docker compose exec api johnny-tts-smoke   # PASS/SKIP/FAIL per provider×runtime×voice
```

See [Verifying TTS audio output](../README.md#verifying-tts-audio-output-every-provider--runtime).

**Voice missing on disk (Piper).** Both `<voice>.onnx` and `<voice>.onnx.json`
must sit together in the voice directory. Install from the picker (it fetches
both), or check `docker run --rm -v johnny_piper_models:/m alpine ls -la /m`.
KittenTTS and Kokoro bundle every voice in one checkpoint, so this only affects
Piper.

---

## See also

- [README → Local TTS providers](../README.md#local-tts-providers) — the short
  per-provider runtime tables.
- [LATENCY.md](LATENCY.md) — cross-provider latency targets, the stage-by-stage
  latency map, and how to measure a full turn.
- [SETUP_LOCAL.md §10](SETUP_LOCAL.md) — installing the default Piper voice.
- `backend/app/providers/parakeet_stt.py` — the canonical runtime-split
  implementation the TTS adapters mirror.
