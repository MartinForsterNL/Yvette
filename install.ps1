# install.ps1 - Yvette Voice Avatar AI - backend installer
# Runs on Windows PowerShell 5.1+. Idempotent: safe to re-run.
$ErrorActionPreference = "Stop"

$Root        = $PSScriptRoot
$Engines     = Join-Path $Root "engines"
$Servers     = Join-Path $Root "servers"
$TensorRT    = Join-Path $Root "TensorRT\TensorRT-8.6.1.6"
$TensorRTLib = Join-Path $TensorRT "lib"
$TensorRTPy  = Join-Path $TensorRT "python"
$Patches     = Join-Path $Root "patches"

function Step($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "  [ok] $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "  [skip] $msg" -ForegroundColor Yellow }
function Die($msg)  { Write-Host "ERROR: $msg" -ForegroundColor Red; exit 1 }

New-Item -ItemType Directory -Force -Path $Engines | Out-Null

# ---------------------------------------------------------------------------
Step "Loading install-config.ps1"

$ConfigFile = Join-Path $Root "install-config.ps1"
if (Test-Path $ConfigFile) {
    . $ConfigFile
    Ok "loaded install-config.ps1"
} else {
    Warn "install-config.ps1 not found (it should ship with the repo). Using empty defaults."
    $HF_TOKEN = ""
    $SKIP_DITTO_CONVERT = $false
    $SKIP_HIGGS = $false
    $HIGGS_BINARY_URL = ""
    $HF_ENDPOINT = ""
}

if ($HF_ENDPOINT) { $env:HF_ENDPOINT = $HF_ENDPOINT; Ok "HF_ENDPOINT set" }

# ---------------------------------------------------------------------------
Step "Checking prerequisites"

function HasCmd($cmd) { return [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }

if (-not (HasCmd "python")) { Die "Python not found. Install Python 3.10 and put it on PATH." }
$pyVer = & python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ($pyVer -ne "3.10") { Die "Python 3.10 is required (found $pyVer). Install Python 3.10 - see install-readme.md Part 1." }
Ok "python $pyVer"

if (-not (HasCmd "git")) { Die "git not found. Install Git for Windows." }
Ok "git"

git lfs version *> $null
if ($LASTEXITCODE -ne 0) { Die "git-lfs not enabled. Run: git lfs install" }
Ok "git-lfs"

if (-not (HasCmd "ffmpeg")) { Warn "ffmpeg not on PATH (needed for audio handling)" } else { Ok "ffmpeg" }

if (-not (Test-Path $TensorRTLib)) {
    Die "TensorRT not found at $TensorRTLib. Extract TensorRT-8.6.1.6 into the TensorRT folder (see install-readme.md Part 1)."
}
Ok "TensorRT lib: $TensorRTLib"

# ---------------------------------------------------------------------------
Step "DITTO (talking-head avatar)"

$ditto = Join-Path $Engines "ditto"
if (-not (Test-Path (Join-Path $ditto ".git"))) {
    git clone https://github.com/justinjohn0306/ditto-talkinghead-windows.git $ditto
    git -C $ditto checkout 227f71e003eb377e1924e304e2f069999037dff8  # pinned
} else { Warn "ditto repo already cloned" }

$dVenv = Join-Path $ditto "venv"
if (-not (Test-Path $dVenv)) { & python -m venv $dVenv }
$dPy = Join-Path $dVenv "Scripts\python.exe"

& $dPy -m pip install --upgrade pip | Out-Null
& $dPy -m pip install torch==2.5.1 torchvision==0.20.1 torchaudio==2.5.1 --index-url https://download.pytorch.org/whl/cu121
& $dPy -m pip install numpy==2.0.1 opencv-python-headless==4.10.0.84 librosa==0.10.2.post1 soundfile==0.13.0 soxr==0.5.0.post1 numba==0.60.0 tqdm filetype scikit-image
& $dPy -m pip install cuda-python==12.6.2.post1 nvidia-cublas-cu12==12.6.4.1 nvidia-cuda-runtime-cu12==12.1.105 nvidia-cudnn-cu12==9.6.0.74
& $dPy -m pip install onnx==1.23.0 onnxruntime==1.23.2 tifffile==2024.12.12 imageio==2.36.1 imageio-ffmpeg==0.5.1 pooch==1.8.2
& $dPy -m pip install polygraphy==0.53.4 colored "triton-windows<3.2"
& $dPy -m pip install fastapi uvicorn python-multipart cython

# TensorRT python bindings from the SDK (the only reliable source for 8.6.1.6), matching Python 3.10
$trtWhl = Get-ChildItem -Path $TensorRTPy -Filter "tensorrt-8.6.1-cp310*.whl" -ErrorAction SilentlyContinue | Select-Object -First 1
if ($trtWhl) {
    & $dPy -m pip install $trtWhl.FullName
} else {
    Warn "no tensorrt-8.6.1-cp310 wheel found in $TensorRTPy - install it manually if conversion fails"
}
Ok "ditto python deps installed"

# Apply our DITTO patch. Must be idempotent: a re-run (patch already applied) has to
# skip cleanly, not abort the whole install. Native stderr + $ErrorActionPreference=Stop is
# why the old version blew up here, so errors are relaxed around the git calls.
$patch = Join-Path $Patches "ditto-windows.patch"
if (Test-Path $patch) {
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & git -C $ditto apply --check $patch 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) {
        & git -C $ditto apply $patch 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) { Ok "ditto patch applied" } else { Die "ditto patch failed to apply" }
    } else {
        # Not applicable forward: either already applied, or this checkout differs. Neither is
        # fatal on a re-run - warn and carry on. (The old version never reached this branch:
        # the git stderr above aborted the script under $ErrorActionPreference=Stop.)
        Warn "ditto patch already applied or does not match this checkout - skipping"
    }
    $ErrorActionPreference = $prevEap
}

# Checkpoints (ONNX models + configs + plugin)
$dittoCkpt = Join-Path $ditto "checkpoints"
if (-not (Test-Path (Join-Path $dittoCkpt ".git"))) {
    git clone https://huggingface.co/justinjohn-03/ditto-talkinghead-windows $dittoCkpt
    git -C $dittoCkpt checkout 83630257bffec1d75ebe0246f570603afc036262  # pinned
} else { Warn "ditto checkpoints already cloned" }

# Build TensorRT engines for THIS GPU
$dittoTrt = Join-Path $ditto "checkpoints\ditto_trt_custom"
if ($SKIP_DITTO_CONVERT) {
    Warn "DITTO conversion skipped (SKIP_DITTO_CONVERT = true)"
} elseif (-not (Test-Path $dittoTrt)) {
    $env:PATH = "$(Split-Path $dPy);$TensorRTLib;$env:PATH"
    Step "DITTO - building TensorRT engines for this GPU (takes a while)"
    pushd $ditto
    & $dPy scripts\cvt_onnx_to_trt.py --onnx_dir checkpoints\ditto_onnx --trt_dir checkpoints\ditto_trt_custom
    if ($LASTEXITCODE -ne 0) { Die "DITTO engine build failed" }
    popd
    Ok "DITTO engines built in $dittoTrt"
} else { Warn "DITTO engines already built" }

# Copy our wrapper server into the ditto dir
Copy-Item (Join-Path $Servers "ditto_server.py") (Join-Path $ditto "ditto_server.py") -Force
Copy-Item (Join-Path $Servers "avatar_cache.py") (Join-Path $ditto "avatar_cache.py") -Force
Copy-Item (Join-Path $Servers "gen_idle_worker.py") (Join-Path $ditto "gen_idle_worker.py") -Force
Copy-Item (Join-Path $Root "static\avatars\default.jpg") (Join-Path $ditto "avatar.jpg") -Force
Ok "ditto_server.py + avatar_cache.py + gen_idle_worker.py + default avatar placed"

# ---------------------------------------------------------------------------
Step "Breeze (TTS)"

$breeze = Join-Path $Engines "breeze"
if (-not (Test-Path (Join-Path $breeze ".git"))) {
    git clone https://github.com/breezeblue-ai/breeze-tts.git $breeze
    git -C $breeze checkout 008f769016b0a24711becd7a4925030bc93f608c  # pinned
} else { Warn "breeze repo already cloned" }

$bVenv = Join-Path $breeze "venv"
if (-not (Test-Path $bVenv)) { & python -m venv $bVenv }
$bPy = Join-Path $bVenv "Scripts\python.exe"

& $bPy -m pip install --upgrade pip | Out-Null
& $bPy -m pip install torch==2.9.1 torchaudio==2.9.1 --index-url https://download.pytorch.org/whl/cu128
& $bPy -m pip install qwen-tts==0.1.1 transformers==4.57.3 huggingface_hub soundfile fastapi uvicorn python-multipart numpy
Ok "breeze python deps installed"

$breezeModel = Join-Path $breeze "breeze-tts-2"
if (-not (Test-Path $breezeModel)) {
    Step "Breeze - downloading voice checkpoint (gated, needs HF token)"
    if (-not $HF_TOKEN) {
        $HF_TOKEN = Read-Host "Paste your Hugging Face token (https://huggingface.co/settings/tokens)"
    }
    & $bPy -c "from huggingface_hub import snapshot_download; snapshot_download('BreezeBlue/breeze-tts-2', token='$HF_TOKEN', local_dir='$($breezeModel -replace '\\','/')')"
    if ($LASTEXITCODE -ne 0) { Die "Breeze model download failed (bad token or terms not accepted)" }
    Ok "breeze model downloaded"
} else { Warn "breeze model already present" }

# ---------------------------------------------------------------------------
Step "OmniVoice (TTS)"

$omni = Join-Path $Engines "omnivoice"
$oVenv = Join-Path $omni "venv"
if (-not (Test-Path $oVenv)) { New-Item -ItemType Directory -Force -Path $omni | Out-Null; & python -m venv $oVenv }
$oPy = Join-Path $oVenv "Scripts\python.exe"

& $oPy -m pip install --upgrade pip | Out-Null
& $oPy -m pip install torch==2.8.0 torchaudio==2.8.0 --index-url https://download.pytorch.org/whl/cu128
& $oPy -m pip install omnivoice==0.2.1 soundfile numpy
Ok "omnivoice python deps installed (model downloads on first run)"

Copy-Item (Join-Path $Servers "omnivoice_server.py") (Join-Path $omni "omnivoice_server.py") -Force
Ok "omnivoice_server.py placed"

# ---------------------------------------------------------------------------
Step "LuxTTS (TTS)"

$lux = Join-Path $Engines "lux"
if (-not (Test-Path (Join-Path $lux ".git"))) {
    git clone https://github.com/ysharma3501/LuxTTS.git $lux
    git -C $lux checkout 28ae6a61151684fffc9d1a7aa15eafa02286fe0b  # pinned
} else { Warn "lux repo already cloned" }

$lVenv = Join-Path $lux "venv"
if (-not (Test-Path $lVenv)) { & python -m venv $lVenv }
$lPy = Join-Path $lVenv "Scripts\python.exe"

& $lPy -m pip install --upgrade pip | Out-Null
& $lPy -m pip install torch==2.5.1 torchaudio==2.5.1 --index-url https://download.pytorch.org/whl/cu121
& $lPy -m pip install -r (Join-Path $lux "requirements.txt")
Ok "lux python deps installed (model downloads on first run)"

Copy-Item (Join-Path $Servers "lux_server.py") (Join-Path $lux "lux_server.py") -Force
Ok "lux_server.py placed"

# ---------------------------------------------------------------------------
Step "Higgs TTS 3 (optional TTS)"

# Opt-in engine: set $SKIP_HIGGS = $true in install-config.ps1 to leave it out.
# Higgs TTS 3 runs as a C++ GGUF server, built here from pinned source (or taken
# from a prebuilt zip). Boson AI's Higgs Audio v3 license is research /
# non-commercial only.
$higgs = Join-Path $Engines "higgs"
$hBinDir = Join-Path $higgs "bin"
$hModelsDir = Join-Path $higgs "models"
$hExe = Join-Path $hBinDir "higgs_server.exe"
$higgsReady = $false

if ($SKIP_HIGGS) {
    Warn "higgs skipped (SKIP_HIGGS = true)"
} else {
    New-Item -ItemType Directory -Force -Path $higgs, $hModelsDir | Out-Null

    if ($HIGGS_BINARY_URL) {
        # Prebuilt route: download + unzip an archive and skip the build entirely.
        # No URL ships with the repo - only use an archive you trust.
        Step "Higgs - downloading prebuilt binaries"
        try {
            $zip = Join-Path $higgs "higgs-prebuilt.zip"
            Invoke-WebRequest -Uri $HIGGS_BINARY_URL -OutFile $zip -UseBasicParsing
            New-Item -ItemType Directory -Force -Path $hBinDir | Out-Null
            Expand-Archive -Path $zip -DestinationPath $hBinDir -Force
            Remove-Item $zip -Force
            $higgsReady = Test-Path $hExe
            if ($higgsReady) { Ok "higgs prebuilt binaries installed" } else { Warn "higgs_server.exe not found in the archive" }
        } catch {
            Warn "higgs prebuilt download failed ($_) - skipping the engine"
        }
    } else {
        # Source route: MSVC + portable CMake + a CUDA build (~12 minutes).
        # MSVC is only needed for this engine: if it is missing we warn and skip,
        # so the rest of the install still completes.
        $vcvars = ""
        $vswhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
        if (Test-Path $vswhere) {
            $vsPath = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
            if ($vsPath) { $vcvars = Join-Path $vsPath "VC\Auxiliary\Build\vcvars64.bat" }
        }
        if (-not ($vcvars -and (Test-Path $vcvars))) {
            Warn "MSVC C++ build tools not found - skipping the higgs engine. Install 'Desktop development with C++' in the Visual Studio Build Tools (or set `$HIGGS_BINARY_URL) and re-run install.ps1."
        } else {
            Step "Higgs - fetching portable CMake 4.4.3 (zip, no PATH changes)"
            $cmakeDir = Join-Path $higgs "cmake_raw"
            $cmake = Join-Path $cmakeDir "cmake-4.4.3-windows-x86_64\bin\cmake.exe"
            if (-not (Test-Path $cmake)) {
                $cmakeZip = Join-Path $higgs "cmake.zip"
                Invoke-WebRequest -Uri "https://github.com/Kitware/CMake/releases/download/v4.4.3/cmake-4.4.3-windows-x86_64.zip" -OutFile $cmakeZip -UseBasicParsing
                New-Item -ItemType Directory -Force -Path $cmakeDir | Out-Null
                Expand-Archive -Path $cmakeZip -DestinationPath $cmakeDir -Force
                Remove-Item $cmakeZip -Force
            }
            if (-not (Test-Path $cmake)) {
                Warn "portable CMake download failed - skipping the higgs build"
            } else {
                $hSrc = Join-Path $higgs "HiggsTTS.cpp"
                if (-not (Test-Path (Join-Path $hSrc ".git"))) {
                    Step "Higgs - cloning HiggsTTS.cpp (pinned)"
                    git clone https://github.com/Rafa00127/HiggsTTS.cpp $hSrc
                    git -C $hSrc checkout 5e9f8f0aac79f7503ee95080867ab283216279aa
                } else { Warn "higgs repo already cloned" }

                # 86 = Ampere (RTX 30xx). Change the arch for other GPUs.
                $hBuild = Join-Path $hSrc "build-cu"
                $hLog = Join-Path $higgs "build.log"
                $buildCmd = "call `"$vcvars`" && `"$cmake`" -B `"$hBuild`" -S `"$hSrc`" -DGGML_CUDA=ON -DCMAKE_BUILD_TYPE=Release -DCMAKE_CUDA_ARCHITECTURES=86 && `"$cmake`" --build `"$hBuild`" --config Release -j 8"
                Step "Higgs - building with CUDA (~12 minutes)"
                Write-Host "  log: $hLog"
                # Native stderr must not abort the install under $ErrorActionPreference=Stop
                # (same reason the DITTO patch block relaxes it).
                $prevEap = $ErrorActionPreference
                $ErrorActionPreference = "Continue"
                & cmd /c $buildCmd 2>&1 | Out-File -FilePath $hLog -Encoding utf8
                $buildExit = $LASTEXITCODE
                $ErrorActionPreference = $prevEap
                if ($buildExit -ne 0) {
                    Warn "higgs build failed (exit $buildExit) - see $hLog. The install continues without higgs."
                } else {
                    Copy-Item (Join-Path $hBuild "bin\Release\*") $hBinDir -Force
                    $higgsReady = Test-Path $hExe
                    if ($higgsReady) { Ok "higgs built and installed" } else { Warn "higgs build produced no higgs_server.exe" }
                }
            }
        }
    }

    if ($higgsReady) {
        # Only the default quant is downloaded here; higgs_server.py fetches q6_k /
        # q8_0 from the same repo on demand when you select them in the admin UI.
        $hModel = Join-Path $hModelsDir "higgs-v3-tts-q4_k.gguf"
        if (Test-Path $hModel) {
            Warn "higgs q4_k model already present"
        } else {
            try {
                Step "Higgs - downloading the q4_k model (~2.8 GB)"
                Invoke-WebRequest -Uri "https://huggingface.co/NeemaShioSe/HiggsTTS3.gguf/resolve/main/higgs-v3-tts-q4_k.gguf" -OutFile $hModel -UseBasicParsing
                Ok "higgs q4_k model in $hModelsDir"
            } catch {
                Warn "higgs model download failed ($_) - it downloads on first use instead"
            }
        }

        # The wrapper is stdlib-only (it just drives the C++ exe), so its venv only
        # keeps the engines/ layout uniform - there are no packages to install.
        $hVenv = Join-Path $higgs "venv"
        if (-not (Test-Path (Join-Path $hVenv "Scripts\python.exe"))) { & python -m venv $hVenv }

        Copy-Item (Join-Path $Servers "higgs_server.py") (Join-Path $higgs "higgs_server.py") -Force
        Ok "higgs_server.py placed"
    } else {
        Warn "higgs engine not installed (no binary)"
    }
}

# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
Step "voice-ai app (orchestrator)"

$appVenv = Join-Path $Root "venv"
if (-not (Test-Path (Join-Path $appVenv "Scripts\python.exe"))) { & python -m venv $appVenv }
$appPy = Join-Path $appVenv "Scripts\python.exe"

& $appPy -m pip install --upgrade pip | Out-Null
& $appPy -m pip install -r (Join-Path $Root "requirements.txt")
Ok "app python deps installed"

Step "app post-install (config tokens + model pre-download)"
& $appPy (Join-Path $Root "app_install.py") $HF_TOKEN
if ($LASTEXITCODE -ne 0) {
    Warn "app post-install had an issue - models will download lazily on first run"
} else {
    Ok "app installed (api_token generated, models pre-downloaded)"
}

# ---------------------------------------------------------------------------
Step "Done"

Write-Host ""
Write-Host "Backends installed under $Engines" -ForegroundColor Green
Write-Host "  ditto/      avatar (engines built in checkpoints/ditto_trt_custom)"
Write-Host "  breeze/     TTS"
Write-Host "  omnivoice/  TTS"
Write-Host "  lux/        TTS"
Write-Host "  higgs/      TTS (optional)"
Write-Host ""
Write-Host ""
Write-Host "The app is installed at the repo root. Run start.bat to launch it." -ForegroundColor Green
