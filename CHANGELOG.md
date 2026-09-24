# Changelog - Yvette Voice Avatar AI

Versioning rule: **v0.1 = the state published on GitHub** (github.com/MartinForsterNL/Yvette,
commit `4730bc9`). Everything after that is an unreleased iteration. We are currently on **v0.2**.

A version is only "released" when it is pushed - and pushes happen only when Martin says so.

## v0.2 - unreleased (in progress, not pushed)

### Streaming
- TalkUI delivers DITTO's growing fMP4 over a WebSocket, and the player queues chunk appends
  serially, so video no longer cuts off ~1-2s before the end (`3d6732d`).
- DITTO server: paced ingest worker (each chunk submitted ~1s ahead of real time), HTTP Range
  support on `/video`, timestamped logging with ANSI-aware console output (`7808aa7`).
- `patches/ditto-windows.patch` rebuilt so a fresh install gets the whole streaming change set,
  not just the new writer (`d771af1`).

### Install / fresh-install fixes
- Engine repos are pinned to exact commits instead of following default-branch HEAD (`791fec7`).
- Cython is installed into the DITTO venv. `core/utils/blend` imports `pyximport`, so without it
  DITTO could not start at all on a fresh install (`9a215b8`).
- The patch step is idempotent again: a re-run warns instead of aborting the install. Under
  `$ErrorActionPreference = "Stop"`, git's stderr became a terminating error.
- DITTO build toolchain pinned (`polygraphy==0.53.4`, `onnx==1.23.0`, `onnxruntime==1.23.2`).
  With Polygraphy 0.53.6 the lipsync engine (`lmdm_v0.4_hubert_fp16`) built to 123 MB and produced
  fast gibberish mouth motion; with 0.53.4 it builds to a working 103-111 MB. Note the build is
  still not bit-reproducible (TensorRT selects kernels by timing), so a known-good engine set is
  kept at `_work/trt_known_good/` to copy back instead of trusting a rebuild.

### Avatars and config
- Avatars are read from `static/avatars` - which is also where the frontend already served them
  from - and mirrored into the DITTO engine dir at startup. `avatars_settings.json` holds the
  per-avatar settings (`82eb32e`).
- `config.yaml` ships the DITTO streaming settings, the model voice/instruction defaults and the
  extra `llm.models` entries (`21516de`).
- Hugging Face downloads authenticate with the token from `config.yaml`, instead of going out
  anonymously (`182b586`).
- `_work/` moved out of the repo to the project root; the repo ignores it plus runtime logs (`5a97d65`).
- Default tuning values: `ditto.sampling_timesteps` 30 (was 50) and `stt.compute_type` int8 (was
  float16) - faster inference for a minimal quality cost.

### Admin UI
- Admin top menu reordered to LLMs, STT, TTS, Avatars, Personalities, Profiles, Status, General, and
  the page now lands on the Status tab by default.

## v0.1 - published on GitHub

Baseline as published: commit `4730bc9` (2026-09-21) "Add demo video + 8GB note to README".
