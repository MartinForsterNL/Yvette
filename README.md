# Yvette Voice Avatar AI

A talking-head voice assistant: speech-to-text, LLM chat, text-to-speech, and a
lip-synced avatar, all behind one backend and one settings UI.

Three former servers (TTS, talk, avatar) are merged into one FastAPI app. Heavy
models run as internal subprocesses; lightweight models run in-process.

## What it does

- **Hold-to-talk UI** - record audio or type; replies stream back as speech
- **Full chat flow** - mic -> Whisper (STT) -> LLM -> sentence chunking -> TTS -> avatar video
- **Talking-head avatar** - DITTO renders a face that lip-syncs each reply; an idle
  "breathing" loop plays between turns (both optional, toggled in settings)
- **Multiple voices** - Breeze (clone + design + direction), OmniVoice, Lux, Kokoro
- **Voice cloning / design** - create custom voices from reference audio or a text
  description (in the admin UI)
- **Long-term memory** - the assistant remembers facts across sessions, with a
  daily curator
- **Tool calling** - web search, web fetch, weather, and a personal file store
- **Per-profile history** - separate chat history per personality profile

## Install

This repo ships the app code and an installer. The heavy model backends are
downloaded and built on your machine by the installer - they are not committed.

See **install-readme.md** for the full, step-by-step guide. The short version:

1. Install the prerequisites: Windows 10/11, an NVIDIA GPU (24 GB VRAM
   recommended), Python 3.10, Git + Git LFS, ffmpeg, CUDA 12.0, cuDNN 8.9.x,
   and TensorRT 8.6.1.6 (exact versions and links are in install-readme.md).
2. Edit install-config.ps1 (your Hugging Face token).
3. Run .\install.ps1 - it clones the backends, creates the venvs, downloads the
   models, and builds the DITTO TensorRT engines for your GPU.
4. Run start.bat, then open http://localhost:8900 (talk UI) and
   http://localhost:8900/admin (admin UI).

## Architecture

```
server.py            # merged FastAPI app (chat flow + TTS + avatar + settings + auth)
config.yaml          # one config for everything
tts/                 # TTS module (registry + backends + STT + voices)
static/              # talk UI + admin UI + default avatar + idle video
memory.py            # long-term memory (brain)
engines/             # created by install.ps1: the 4 backend venvs + models (gitignored)
```

### How the models run

| Model        | Where                  | GPU |
|--------------|------------------------|-----|
| Whisper (STT)| in-process             | yes |
| Kokoro (TTS) | in-process             | CPU |
| Breeze (TTS) | subprocess (own venv)  | CUDA |
| OmniVoice    | subprocess (own venv)  | CUDA |
| Lux          | subprocess (own venv)  | CUDA |
| DITTO (avatar)| subprocess (own venv) | CUDA (TensorRT) |

Each subprocess backend is spawned on first use (lazy) and can be unloaded to
free VRAM from the admin UI.

## Settings

Everything is configured from the admin UI (http://localhost:8900/admin):

- **Profile** - personality + voice + model, saved per profile
- **Clone / Design** - which voice the avatar speaks with
- **Generate avatar video** - on: render the talking head; off: fast audio-only
- **Show idle avatar** - the background "breathing" loop
- **Bubble / text opacity** - transparency of the chat overlay
- **Mode** - chunked (stream per sentence) or full
- **Model** - the LLM (any OpenAI-compatible endpoint, set in the LLMs tab)
- **TTS engine** - breeze / omnivoice / lux / kokoro

## License

Source code is licensed under the Apache License 2.0. **Model weights are NOT
included** and are governed by their own licenses:

- **Breeze TTS 2** (`BreezeBlue/breeze-tts-2`): research / non-commercial only
- **DITTO**, **OmniVoice**, **Lux**, **Whisper**, **Kokoro**: see each upstream repo
- **NVIDIA TensorRT**: proprietary, download separately from NVIDIA - not redistributable

Download model weights at setup time; do not redistribute them commercially.
