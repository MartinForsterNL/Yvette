import os, sys, time, uuid, threading, subprocess, re

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
os.chdir(BASE_DIR)

import numpy as np
import librosa
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import FileResponse

from stream_pipeline_offline import StreamSDK

CFG = "checkpoints/ditto_cfg/v0.4_hubert_cfg_trt.pkl"
DATA_ROOT = os.environ.get("DITTO_DATA_ROOT", "checkpoints/ditto_trt_custom")
AVATAR_IMAGE = "avatar.jpg"
AVATAR_DIR = os.path.join(BASE_DIR, "example", "images")

HEAD_MOTION_ALPHA = float(os.environ.get("HEAD_MOTION_ALPHA", "1.25"))  # head-rotation amplification (1.0 = default)

def resolve_avatar(name):
    stem = os.path.basename((name or "").strip())
    if not re.fullmatch(r"[A-Za-z0-9_-]+", stem):
        return AVATAR_IMAGE
    for ext in (".png", ".jpg", ".jpeg"):
        p = os.path.join(AVATAR_DIR, stem + ext)
        if os.path.exists(p):
            return p
    return AVATAR_IMAGE

OUTPUT_DIR = os.path.join(BASE_DIR, "server_output")
INPUT_DIR = os.path.join(BASE_DIR, "server_input")

os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(INPUT_DIR, exist_ok=True)

app = FastAPI()
sdk = None
lock = threading.Lock()

def ensure_sdk():
    global sdk
    if sdk is None:
        sdk = StreamSDK(CFG, DATA_ROOT)
        print("[ditto] model loaded")
    return sdk

@app.on_event("startup")
def startup():
    ensure_sdk()

@app.post("/api/avatar")
async def avatar(audio: UploadFile = File(...), avatar: str = Form(""), head_motion_alpha: str = Form("")):
    rid = uuid.uuid4().hex
    audio_path = os.path.join(INPUT_DIR, rid + ".wav")
    out_path = os.path.join(OUTPUT_DIR, rid + ".mp4")

    try:
        hm_alpha = float(head_motion_alpha) if head_motion_alpha.strip() else HEAD_MOTION_ALPHA
    except (TypeError, ValueError):
        hm_alpha = HEAD_MOTION_ALPHA

    with open(audio_path, "wb") as f:
        f.write(await audio.read())

    def generate():
        s = ensure_sdk()
        t0 = time.time()
        s.setup(resolve_avatar(avatar), out_path, overall_ctrl_info={"alpha_pitch": hm_alpha, "alpha_yaw": hm_alpha, "alpha_roll": hm_alpha})
        audio_data, sr = librosa.load(audio_path, sr=16000)
        num_f = int(np.ceil(len(audio_data) / 16000 * 25))
        s.setup_Nd(N_d=num_f, fade_in=-1, fade_out=-1, ctrl_info={})
        aud_feat = s.wav2feat.wav2feat(audio_data)
        s.audio2motion_queue.put(aud_feat)
        s.close()
        # mux audio into the video
        tmp = out_path + ".tmp.mp4"
        cmd = f'ffmpeg -loglevel error -y -i "{tmp}" -i "{audio_path}" -map 0:v -map 1:a -c:v copy -c:a aac "{out_path}"'
        subprocess.run(cmd, shell=True)
        return time.time() - t0

    with lock:
        dt = generate()

    return FileResponse(out_path, media_type="video/mp4", filename="avatar.mp4",
                        headers={"X-Gen-Seconds": f"{dt:.1f}"})

@app.get("/api/health")
def health():
    return {"ok": True, "model_loaded": sdk is not None}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8902)
