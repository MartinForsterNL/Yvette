# Yvette Voice Avatar AI

A talking-head voice assistant: speech-to-text, chat, text-to-speech, and a
lip-synced avatar, all behind one backend.

The app (the orchestrator) ships in this repo. The heavy AI models run as four
separate backends, each in its own Python environment so their dependency sets
can't clash. This guide covers how to install everything.

## What gets installed

| Component   | Purpose                          | Source |
|-------------|----------------------------------|--------|
| voice-ai app | Orchestrator (chat, TTS, avatar, UI) | this repo |
| DITTO      | Talking-head avatar (lip-sync)   | justinjohn0306/ditto-talkinghead-windows |
| Breeze     | Text-to-speech (Breeze TTS 2)    | breezeblue-ai/breeze-tts |
| OmniVoice  | Text-to-speech (multilingual)    | k2-fsa/OmniVoice |
| LuxTTS     | Text-to-speech (voice cloning)   | ysharma3501/LuxTTS |

## System requirements

- Windows 10 or 11
- NVIDIA GPU. Ampere or newer (RTX 30/40/50 series) recommended. Turing (RTX 20 / GTX 16 series) works but the DITTO conversion may need more care.
- The latest NVIDIA GPU driver (Triton needs a recent driver)
- 12 GB VRAM minimum. This depends mostly on the LLM: with a smaller LLM (or
  the LLM on a separate machine) even 10 GB can work.
- About 60 GB of free disk space
- A free NVIDIA developer account (needed to download cuDNN and TensorRT)

---

## Part 1 - Manual prerequisites (one time, before the script)

The installer script cannot download these for you, mostly because two of them
sit behind a free NVIDIA account login. Do these first, in order.

### 1. Python 3.10

Install 64-bit Python 3.10 and make sure it is on your PATH.

Download: https://www.python.org/downloads/release/python-31011/

### 2. Git and Git LFS

Install Git for Windows, then enable Git LFS:

```
git lfs install
```

Download: https://git-scm.com/download/win

### 3. ffmpeg

Install ffmpeg and make sure it is on your PATH.

Download: https://ffmpeg.org/download.html

### 4. CUDA 12.0 toolkit

The TensorRT build this system uses targets CUDA 12.0. Install the CUDA 12.0
toolkit (runtime is enough; the full toolkit is fine too).

Download: https://developer.nvidia.com/cuda-12-0-0-download-archive

### 5. cuDNN 8.9.x

Install cuDNN 8.9.x. This version matches CUDA 12.0 and TensorRT 8.6.1.6. Use
8.9.0 or any later 8.9 release (8.9.2, 8.9.7, etc).

This requires a free NVIDIA account.

Download: https://developer.nvidia.com/cudnn-archive

Pick "cuDNN 8.9.x for CUDA 12.x". After installing, copy the cuDNN DLLs
(cudnn64_8.dll and the others in its bin folder) into your CUDA 12.0 bin folder
(C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.0\bin), which is the
standard cuDNN install step.

### 6. TensorRT 8.6.1.6

Download the Windows TensorRT package from NVIDIA:

TensorRT-8.6.1.6.Windows10.x86_64.cuda-12.0.zip

This requires a free NVIDIA account.

Download: https://developer.nvidia.com/tensorrt (choose the 8.6.1.6 archive)
Select the "TensorRT 8.6 GA"
Go to Zip Packages for Windows: and download "TensorRT 8.6 GA for Windows 10 and CUDA 12.0 and 12.1 ZIP Package"

Unpack it into the TensorRT folder in the root of this project. The TensorRT
folder already exists. Extract the zip directly inside it so you end up with
this exact layout:

```
TensorRT/
  TensorRT-8.6.1.6/
    lib/      <- contains nvinfer.dll (required)
    include/
    bin/
    python/   <- contains the tensorrt Python wheel (required)
```

In other words: open the zip, and extract the TensorRT-8.6.1.6 folder so it
sits directly inside the project's TensorRT folder. The install script finds it
at TensorRT/TensorRT-8.6.1.6 and uses a relative path, so it works regardless
of where the project lives.

Do not rename the inner folder. The script looks for TensorRT-8.6.1.6.

---

## Part 2 - Configure, then install

### 1. Edit your config file

Open install-config.ps1 (it ships with the repo, values are empty by default)
and put your Hugging Face access token in it (read permission is enough). Get a
token at https://huggingface.co/settings/tokens and accept the Breeze model
terms at https://huggingface.co/BreezeBlue/breeze-tts-2.

The token is only needed to download the gated Breeze voice checkpoint. The
other models (OmniVoice, LuxTTS) are public and download on first run without
one.

Your token is a secret. Do not commit install-config.ps1 with your token
filled in if you fork the repo. If you leave the token empty, the installer
will ask you for it interactively.

### 2. Run the installer

Open PowerShell in the project root and run:

```
Set-ExecutionPolicy -Scope Process Bypass
.\install.ps1
```

The script is idempotent: you can re-run it and it will skip steps that are
already done.

It will:

1. Verify the prerequisites (Python, git, git-lfs, ffmpeg, and the TensorRT
   folder from Part 1).
2. Clone the four upstream repos.
3. Create a Python venv for each backend.
4. Install each backend's dependencies (PyTorch with CUDA, plus engine-specific
   packages). Triton is installed from triton-windows on PyPI, and the TensorRT
   Python bindings are installed from your unpacked SDK.
5. Download the DITTO checkpoints (ONNX models + configs + plugin) via git-lfs.
6. Apply the small DITTO patch this project ships (two upstream files need a
   NumPy 2.x fix and an ONNX custom-op registration).
7. Build the TensorRT engines for your specific GPU from the ONNX models using
   cvt_onnx_to_trt.py. This is why the TensorRT and cuDNN steps in Part 1 are
   required: the conversion needs them.
8. Copy this project's wrapper servers (servers/) into each backend folder.
9. Create the app venv and install its Python dependencies (requirements.txt).
10. Generate a random api_token and write your HF token into config.yaml.
11. Pre-download the internal models (Whisper, Kokoro, embeddings).

---

## Part 3 - What the four backends look like after install

```
engines/
  ditto/          # cloned repo + venv + checkpoints/ + ditto_trt_custom/
  breeze/         # cloned repo + venv + model checkpoint
  omnivoice/      # venv (model downloads on first run)
  lux/            # cloned repo + venv (model downloads on first run)
```

The repo's servers/ folder holds the three wrapper servers that the installer
copies into the backends above (ditto_server.py, omnivoice_server.py,
lux_server.py). These are this project's own code and are tracked in git.

---

## Part 4 - After install: LLM and first boot

### 1. Point it at your LLM

The app ships with a dummy LLM (http://localhost:1234/v1, model
"your-model-name"). Set your real LLM one of two ways:

- Admin UI: open http://localhost:8900/admin -> LLMs tab -> add/edit a model.
- Or edit config.yaml's llm section (base_url + model).

The LLM is any OpenAI-compatible endpoint (LM Studio, Ollama, vLLM, etc.).

### 2. Change the admin password

The app boots with Admin / Testing123. Log in and change the password in
General > Auth.

### 3. Rotate the API key / enable HTTPS

install.ps1 already generated a random api_token. To rotate it (or enable HTTPS),
open General > Server and use "Generate new API key" and "Generate SSL cert".
The SSL cert is created on demand - it is never shipped.

### 4. Run it

```
start.bat
```

Then open http://localhost:8900 (talk UI) and http://localhost:8900/admin
(backend admin).

---

## Notes

- TensorRT engines are GPU-specific. The script builds them for the GPU in the
  machine it runs on. If you move the install to a different GPU, re-run the
  DITTO conversion step.
- Model weights are downloaded at install time and are governed by their own
  licenses. Breeze TTS 2 is research / non-commercial only. See each upstream
  repo for the others.
- TensorRT and cuDNN are proprietary NVIDIA software. They are not bundled here
  and must be downloaded from NVIDIA.
- DITTO's upstream README installs it with conda, but this project installs it in
  a plain Python venv (matching the reference setup, which works fine). You do
  not need conda, Miniconda, or micromamba.
- config.yaml ships with default credentials (Admin / Testing123, empty
  api_token). install.ps1 writes a random api_token and your HF token into it,
  so git shows it as modified after install. If you contribute back, freeze it
  with: `git update-index --skip-worktree config.yaml`
