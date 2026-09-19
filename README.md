# voice-ai

A single-package voice assistant: speech-to-text, LLM chat, text-to-speech, and a
talking-head avatar, all behind one backend and one settings UI.

Three former servers (TTS, talk, avatar) are merged into one FastAPI app. Heavy
models run as internal subprocesses; lightweight models run in-process.

## What it does

- **Hold-to-talk UI** - record audio or type; replies stream back as speech
- **Full chat flow** - mic → Whisper (STT) → LLM → sentence chunking → TTS → avatar video
- **Talking-head avatar** - DITTO renders a face that lip-syncs each reply; an idle
  "breathing" loop plays between turns (both optional, toggled in settings)
- **Multiple voices** - Breeze (clone + design + direction), OmniVoice, Lux, Kokoro
- **Voice cloning / design** - create custom voices from reference audio or a text
  description (in the admin UI)
- **Long-term memory** - the assistant remembers facts across sessions, with a
  daily curator
- **Tool calling** - web search, web fetch, weather, and a personal file store
- **Per-profile history** - separate chat history per personality profile

## Architecture

```
voice-ai/
  server.py            # the merged FastAPI app (chat flow + TTS + avatar + settings + auth)
  config.yaml          # one config for everything
  tts/                 # TTS module (registry + backends + STT + voices)
  static/              # talk UI + avatars + idle videos
  static/admin/        # backend admin UI (voice clone/design, TTS tester, unload)
  output/              # generated audio + per-turn files (audio + video)
  voices/              # voice profiles + reference audio
  memory.py            # long-term memory (brain)
  personalities/ skills/  # persona + skill definitions
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

## Installation

### Prerequisites

- Windows 11 (or Linux) with a CUDA-capable NVIDIA GPU (24 GB recommended)
- Python 3.10+
- ffmpeg on PATH
- Git

### Orchestrator (the main app)

```bash
python -m venv venv
venv\Scripts\pip install -r requirements.txt
```

### Backend subprocesses (each in its own venv)

The heavy models need their own environments (incompatible dependency sets).

**Breeze (PyTorch/CUDA):**

```bash
git clone https://github.com/breezeblue-ai/breeze-tts.git
python -m venv breeze-cuda/venv
breeze-cuda/venv/Scripts/pip install torch==2.9.1 torchaudio==2.9.1 --index-url https://download.pytorch.org/whl/cu128
breeze-cuda/venv/Scripts/pip install qwen-tts==0.1.1 transformers==4.57.3 soundfile fastapi uvicorn python-multipart numpy
# download the checkpoint (gated; needs a HuggingFace token)
breeze-cuda/venv/Scripts/python -c "from huggingface_hub import snapshot_download; snapshot_download('BreezeBlue/breeze-tts-2', token='HF_TOKEN', local_dir='breeze-tts-2')"
```

**OmniVoice / Lux / DITTO:** see their respective upstream repos. Point
`config.yaml` at each one's venv + script.

DITTO additionally needs **NVIDIA TensorRT** at runtime. Download
`TensorRT-8.6.1.6.Windows10.x86_64.cuda-12.0` from NVIDIA and point
`ditto.tensorrt_lib` at its `lib` folder. TensorRT is free to use but
proprietary - it is **not** bundled here and must not be redistributed.

### Configuration

Copy `config.yaml` and edit:

- `llm` - your LLM endpoint (OpenAI-compatible `/v1/chat/completions`)
- `tts` - default voice + engine
- `models` - paths to each backend's venv / script / model
- `auth` - UI username/password + API token
- `ditto` - avatar image dir + the ditto server paths
- `stt` - Whisper model (default `large-v3-turbo`)

### Run

```bash
venv\Scripts\python server.py
```

Then open `http://localhost:8900` (the talk UI) and `http://localhost:8900/admin`
(the backend admin UI). Log in with the credentials from `config.yaml`.

## Settings

- **Profile** - personality + voice + model, saved per profile
- **Clone / Design** - which voice the avatar speaks with
- **Generate avatar video** - on: render the talking head; off: fast audio-only
- **Show idle avatar** - the background "breathing" loop
- **Bubble / text opacity** - transparency of the chat overlay
- **Mode** - chunked (stream per sentence) or full
- **Model** - which LLM (8b / 27b / ...)
- **TTS engine** - breeze / omnivoice / lux

## License

Source code is licensed under the Apache License 2.0. **Model weights are NOT
included** and are governed by their own licenses:

- **Breeze TTS 2** (`BreezeBlue/breeze-tts-2`): research / non-commercial only
- **DITTO**, **OmniVoice**, **Lux**, **Whisper**, **Kokoro**: see each upstream repo
- **NVIDIA TensorRT**: proprietary, download separately from NVIDIA - not redistributable

Download model weights at setup time; do not redistribute them commercially.
