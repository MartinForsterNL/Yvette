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
# Optional: Hugging Face endpoint override (e.g. a mirror like hf-mirror.com).
# Leave empty for the default.
$HF_ENDPOINT = ""
