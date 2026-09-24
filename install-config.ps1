# install-config.ps1
#
# Edit this file BEFORE running install.ps1. It ships with the repo with empty
# values - fill in what you need below.
#
# WARNING: your Hugging Face token is a secret. Do not commit this file with
# your token filled in if you fork this repo.

# ---------------------------------------------------------------------------
# Hugging Face access token (read permission is enough).
# Only needed to download the gated Breeze voice checkpoint. If you leave this
# empty the installer will ask you for it interactively.
# Get a token:  https://huggingface.co/settings/tokens
# Accept terms: https://huggingface.co/BreezeBlue/breeze-tts-2
$HF_TOKEN = ""

# ---------------------------------------------------------------------------
# Skip rebuilding the DITTO TensorRT engines.
# Set to $true only if you already have engines in
# engines/ditto/checkpoints/ditto_trt_custom/ (e.g. you re-ran the installer
# after moving to a machine with the same GPU).
$SKIP_DITTO_CONVERT = $false

# ---------------------------------------------------------------------------
# Skip the optional Higgs TTS 3 engine (C++ CUDA build + ~2.8 GB q4_k model).
# Set to $true to install the rest of the app without it.
$SKIP_HIGGS = $false

# ---------------------------------------------------------------------------
# Optional: use prebuilt Higgs TTS 3 binaries instead of building them.
# Leave empty (default) to build the engine from pinned source during install -
# that needs the MSVC C++ build tools and takes about 12 minutes. Set this to a
# zip URL and the installer downloads and unpacks it instead, skipping the build
# (and the C++ tools requirement). No URL ships with the repo: only use an
# archive whose author you trust.
$HIGGS_BINARY_URL = "https://github.com/MartinForsterNL/Yvette/releases/download/higgs-runtime-v1/higgs-tts-runtime-win64-cuda-multiarch.zip"

# ---------------------------------------------------------------------------
# Optional: Hugging Face endpoint override (e.g. a mirror like hf-mirror.com).
# Leave empty for the default.
$HF_ENDPOINT = ""
