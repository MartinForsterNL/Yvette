# Changelog - Yvette Voice Avatar AI

## v0.3 - 2026-09-24

- Streaming: minimum generation FPS gate (`ditto.min_generation_fps`, `fps_sample_count`).
- Higgs TTS 3: optional voice-cloning engine (GGUF; q4_k / q6_k / q8_0).
- Install: Higgs prebuilt CUDA runtime; Higgs model downloads via `huggingface_hub`.
- Higgs added to all engine and clone-voice selectors; `stop.ps1` sweeps its processes.
- Defaults: `auth.username` Admin; `ditto.min_generation_fps` 20.

## v0.2 - 2026-09-24

- TalkUI: live streaming over a WebSocket with serial chunk queueing.
- DITTO server: paced ingest worker, HTTP Range on `/video`, timestamped logging.
- Fresh install: pinned engine repos and build toolchain (polygraphy 0.53.4), Cython install, idempotent patch step.
- Avatars: read from `static/avatars`, mirrored into the DITTO engine dir, idle generation serialised.
- Admin: top menu reordered, landing on the Status tab.
- Defaults: `ditto.sampling_timesteps` 30, `stt.compute_type` int8, HF downloads use the config token.
- Layout: `_work/` moved outside the repo.

## v0.1 - 2026-09-21

- Baseline as published on GitHub: `4730bc9`.
