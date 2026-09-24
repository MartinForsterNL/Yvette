# Changelog - Yvette Voice Avatar AI

## v0.3 - unreleased

- Higgs TTS 3: optional voice-cloning TTS engine (C++ GGUF server, q4_k/q6_k/q8_0 quants), disabled by default.
- Streaming: playback starts only once DITTO's generation FPS clears a configurable minimum
  (default 30, averaged over the last 10 samples), then plays through - no bail-out.

## v0.2 - 2026-09-24

- TalkUI: live streaming over a WebSocket with serial chunk queueing (fixes early video cut-off).
- DITTO server: paced ingest worker, HTTP Range on `/video`, timestamped logging.
- Fresh install: pin the engine repos and the build toolchain (polygraphy 0.53.4), install Cython,
  and make the patch step idempotent so a re-run cannot abort.
- Avatars: read from `static/avatars` (mirrored into the DITTO engine dir), idle generation
  serialised, and Add/Regenerate disabled while DITTO is busy.
- Admin: top menu reordered, landing on the Status tab.
- Defaults: `ditto.sampling_timesteps` 30, `stt.compute_type` int8, HF downloads use the config token.
- Layout: `_work/` moved outside the repo so scratch never pollutes the working tree.

## v0.1 - 2026-09-21

- Baseline as published on GitHub: `4730bc9`.
