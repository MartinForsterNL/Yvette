# Changelog - Yvette Voice Avatar AI

Versioning: **v0.1 = the state published on GitHub** (commit `4730bc9`). Anything after is an
unreleased iteration; we are on **v0.2**. A version is released only when it is pushed.

Keep this file brief: **major changes only, one line each.** It is a human summary, not a commit
log - `git log` holds the detail.

## v0.2 - unreleased

- TalkUI: live streaming over a WebSocket with serial chunk queueing (fixes early video cut-off).
- DITTO server: paced ingest worker, HTTP Range on `/video`, timestamped logging.
- Fresh install: pin the engine repos and the build toolchain (polygraphy 0.53.4), install Cython,
  and make the patch step idempotent so a re-run cannot abort.
- Avatars: read from `static/avatars` (mirrored into the DITTO engine dir), idle generation
  serialised, and Add/Regenerate disabled while DITTO is busy.
- Admin: top menu reordered, landing on the Status tab.
- Defaults: `ditto.sampling_timesteps` 30, `stt.compute_type` int8, HF downloads use the config token.
- Layout: `_work/` moved outside the repo so scratch never pollutes the working tree.

## v0.1 - published

- Baseline as published on GitHub: `4730bc9`.
