# Yvette Voice Avatar AI

A voice assistant with a face. You talk to it (or type), and it answers in
speech while an animated avatar on screen moves its lips in sync. It runs
entirely on your own machine.

Whether you want a sharp assistant to get things done or an AI girlfriend
to keep you company, you can have it all - pick the voice, the face, and
the personality that feels right. Make it yours.

## What it does

- **Talk to it** - hold the button and speak, or just type. It replies with a
  spoken answer.
- **A face that talks back** - an animated avatar moves its lips in sync with
  what it says, and breathes gently between replies. You can turn this off if
  you only want the voice.
- **Pick a voice** - choose from built-in voices, or make your own: clone a
  voice from a short audio sample, or describe the voice you want in plain text.
- **It remembers** - it keeps notes on what you tell it, so it can pick up where
  you left off in a later conversation.
- **It can do things for you** - search the web, read a web page, check the
  weather, and keep a small set of files for you.
- **Separate personalities** - set up different profiles (say "work" and "fun"),
  each with its own personality, voice, and chat history.

## Requirements

- Windows 10 or 11
- An NVIDIA GPU (CUDA-capable). Ampere or newer (RTX 30/40/50 series) is
  recommended. 12 GB VRAM minimum.
- About 60 GB of free disk space

How much VRAM you need depends mostly on the LLM you run. 12 GB is a safe
minimum; with a smaller LLM (or the LLM on a separate machine) even 10 GB
can work.

The full software prerequisites (Python, CUDA, cuDNN, TensorRT) are listed in
install-readme.md.

---

## Install

This repo ships the app code and an installer. The heavy model backends are
downloaded and built on your machine by the installer - they are not committed.

See **install-readme.md** for the full, step-by-step guide. The short version:

1. Install the prerequisites: Windows 10/11, an NVIDIA GPU, Python 3.10, Git
   + Git LFS, ffmpeg, CUDA 12.0, cuDNN 8.9.x, and TensorRT 8.6.1.6 (exact
   versions and links are in install-readme.md).
2. Edit install-config.ps1 (your Hugging Face token).
3. Run .\install.ps1 - it clones the backends, creates the venvs, downloads the
   models, and builds the DITTO TensorRT engines for your GPU.
4. Run start.bat, then open http://localhost:8900 (talk UI) and
   http://localhost:8900/admin (admin UI).

## Architecture

```
server.py            # the FastAPI app (chat flow + TTS + avatar + settings + auth)
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
