# Ralph Progress Log

This file tracks progress across iterations. Agents update this file
after each iteration and it's included in prompts for context.

## Codebase Patterns (Study These First)

### httpx must always set `follow_redirects=True` when hitting Hugging Face

HuggingFace's `https://huggingface.co/<repo>/resolve/main/<file>` endpoint
is documented to 307-redirect to a CDN-cached blob URL. `httpx` defaults
to NOT following redirects (unlike `requests`), so any code that fetches
from HF must pass `follow_redirects=True` on the client. Mirror the
pattern in `backend/app/providers/piper_tts.py:fetch_voice_catalog` /
`download_voice` — both branches (client-injected and default) must
enable it. A unit test that asserts `follow_redirects=True` appears in
the function source guards against future regressions
(`tests/providers/test_piper_tts.py::test_fetch_voice_catalog_uses_follow_redirects_by_default`).

### Model dirs are host bind mounts, not Docker named volumes

Voices (`piper_models`) and STT weights (`whisper_models`) live in host
bind mounts under `~/.johnny/{piper,whisper}-models`, not in Docker named
volumes. This lets users drop files in by hand (`ls ~/.johnny/...`) and
keeps downloads across `docker compose down -v` resets. `run.sh` creates
the dirs idempotently on first boot. The in-container target path stays
the same (`/var/lib/johnny/{piper,whisper}-models`) so adapter defaults
need no change.

When writing code that touches those dirs from the host (tests, wizards,
smoketest helpers), prefer reading the filesystem directly when given an
absolute path — only fall back to `docker run -v` for legacy bare-name
volumes. See `johnny/wizard/models.py:list_files_in_volume` and
`johnny/smoketest/checks.py:_list_files_in_volume` for the dual-mode
pattern.

### `pytest -m network` for live external probes

Use the `network` pytest marker (declared in `backend/pyproject.toml`)
for tests that need to hit a real external service. CI offline runs skip
them with `-m "not network"`; you MUST run them locally before claiming
a network-touching fix works. Don't rely on `MockTransport` alone for
network-shape bugs — that's how the original Johnny-4c0 fix missed the
307 redirect (the mock pretended HF returns 200 directly).

---

## 2026-06-06 - Johnny-ckz.5

Fix the voice browser 307 redirect bug and switch Piper/Whisper model
dirs from Docker named volumes to host bind mounts under `~/.johnny/`.

**Files changed:**

- `backend/app/providers/piper_tts.py` — `fetch_voice_catalog` now
  constructs its default `httpx.AsyncClient` with `follow_redirects=True`
  so HuggingFace's 307 to the CDN-cached blob URL is followed instead of
  raised. `download_voice` already had this; the two branches now match.
- `docker-compose.yml` — `whisper_models` / `piper_models` named volumes
  replaced with `${HOME}/.johnny/{whisper,piper}-models` host bind mounts
  on the `api`, `worker`, and `meet-worker` services. The named-volume
  declarations are removed. Default `JOHNNY_MEET_WORKER_{PIPER,WHISPER}_VOLUME`
  env values switched to the host paths so the launcher inherits them.
- `backend/app/services/docker_launcher.py` — Default values for the
  meet-worker volume env vars now point at `~/.johnny/...` (resolved at
  import time). The volume-spec helper already accepted both host paths
  and bare names; updated docstrings to explain the dual-mode behavior.
- `run.sh` — Pre-creates `~/.johnny/piper-models` and
  `~/.johnny/whisper-models` idempotently on first boot. Detects legacy
  `johnny_piper_models` / `johnny_whisper_models` Docker volumes and
  prints a one-liner `docker cp`-style migration command for the user.
- `backend/johnny/wizard/models.py` — `WHISPER_VOLUME` / `PIPER_VOLUME`
  constants moved from `johnny_*_models` to `~/.johnny/*-models` host
  paths. `list_files_in_volume` now reads host dirs directly for
  absolute paths (faster, no docker round-trip) and falls back to the
  alpine-container path for legacy bare-name volumes. Added
  `_ensure_host_dir` helper so download functions pre-create the host
  directory with the user's uid (preventing the dockerd-as-root
  permission trap).
- `backend/johnny/smoketest/checks.py` — `check_whisper_models_dir` /
  `check_piper_voices_dir` default to the host `~/.johnny/...` paths
  via the same dual-mode `_list_files_in_volume` helper.
- `backend/tests/e2e/providers_ui/plans.py` — `local_asset` for the
  faster-whisper and Piper E2E plans switched from `/var/lib/johnny/...`
  (the in-container path) to `~/.johnny/...` (host path the test harness
  can actually `stat`).
- `backend/tests/e2e/providers_ui/preflight.py` — Updated docstring to
  reflect the host bind mount model.
- `backend/pyproject.toml` — Added `network` pytest marker so the new
  live-HF integration test can be opted in (and skipped on offline CI).
- `backend/tests/providers/test_piper_tts.py` — Added two new tests:
    1. `test_fetch_voice_catalog_uses_follow_redirects_by_default` —
       source-inspection guard so a future edit can't silently drop the
       redirect flag.
    2. `test_fetch_voice_catalog_against_real_huggingface` (marked
       `network`) — hits the real HF voices.json URL, asserts the
       catalog parses, and verifies `en_US-amy-medium` is in the list.

**Verification:**

- 1835 backend tests pass (offline subset, `pytest -m "not network and
  not livekit_smoke and not e2e_ui"`).
- New `pytest -m network` test passes against the live
  `https://huggingface.co/rhasspy/piper-voices/resolve/main/voices.json`
  URL — the redirect IS followed and the catalog parses to a non-empty
  list including `en_US-amy-medium`.
- `docker compose config` resolves both api and worker service volumes
  to `/Users/nikita/.johnny/{piper,whisper}-models` bind mounts and the
  meet-worker env vars carry the same paths.
- `ruff` and `mypy` pass on every file touched.
- Existing voice file `en_US-john-medium.onnx` (~60 MB) is already
  visible at `~/.johnny/piper-models/` on the host — confirms the bind
  mount round-trip works in practice for the user.

**Learnings:**

- See "Codebase Patterns" at top — three new patterns extracted from
  this fix: `follow_redirects=True` for HF, host bind mounts under
  `~/.johnny`, and the `network` pytest marker.
- The original Johnny-4c0 fix passed unit tests because the mock
  `httpx.MockTransport` returned 200 directly. Adding a `network` test
  that hits the real URL catches this entire class of bug — a mock
  cannot pretend to be a redirect.
- Docker's `-v <abs_path>:<container_path>` accepts both host bind
  mounts (when the LHS is an absolute path) and named volumes (when the
  LHS is a bare identifier). This lets the same helper functions
  transparently support both modes for backwards compatibility.
- Pre-creating bind-mount source dirs with the user's uid in `run.sh`
  matters: dockerd creates missing bind sources as root, which then
  prevents the user from writing into them by hand.

---

