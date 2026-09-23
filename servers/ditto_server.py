import os, sys, time, uuid, threading, subprocess, re
import queue


def ts():
    return time.strftime("%H:%M:%S") + f".{int(time.time() * 1000) % 1000:03d}"


YEL = chr(27) + "[33m"
RST = chr(27) + "[0m"


def _enable_ansi():
    if os.name == "nt":
        try:
            import ctypes
            k = ctypes.windll.kernel32
            k.SetConsoleMode(k.GetStdHandle(-11), 7)
        except Exception:
            pass


def file_monitor(st):
    last = 0
    try:
        while not st.get("done"):
            try:
                size = os.path.getsize(st["tmp_path"])
            except OSError:
                size = 0
            if size != last:
                print(f"\n{ts()} [fragment] +{size - last}B size={size}", flush=True)
                last = size
            time.sleep(0.1)
    except Exception as e:
        print(f"\n{ts()} [fragment] monitor error: {e}", flush=True)


_enable_ansi()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
os.chdir(BASE_DIR)

import numpy as np
import librosa
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

from stream_pipeline_online import StreamSDK
from avatar_cache import AvatarCache

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

AVATAR_CACHE_DIR = os.path.join(BASE_DIR, "cache", "avatar")
avatar_cache = AvatarCache(AVATAR_CACHE_DIR)

STALE_TTL_SECONDS = 24 * 3600

def cleanup_stale_files(directory, ttl_seconds):
    now = time.time()
    try:
        for name in os.listdir(directory):
            p = os.path.join(directory, name)
            try:
                if os.path.isfile(p) and (now - os.path.getmtime(p)) > ttl_seconds:
                    os.remove(p)
            except OSError:
                pass
    except OSError:
        pass

def stale_cleaner_loop():
    while True:
        cleanup_stale_files(OUTPUT_DIR, STALE_TTL_SECONDS)
        cleanup_stale_files(INPUT_DIR, STALE_TTL_SECONDS)
        time.sleep(3600)

app = FastAPI()
sdk = None
lock = threading.Lock()

def ensure_sdk():
    global sdk
    if sdk is None:
        sdk = StreamSDK(CFG, DATA_ROOT)
        print(f"{ts()} [ditto] model loaded")
    return sdk

@app.on_event("startup")
def startup():
    ensure_sdk()
    cleanup_stale_files(OUTPUT_DIR, STALE_TTL_SECONDS)
    cleanup_stale_files(INPUT_DIR, STALE_TTL_SECONDS)
    threading.Thread(target=stale_cleaner_loop, daemon=True).start()

@app.post("/api/avatar")
async def avatar(audio: UploadFile = File(...), avatar: str = Form(""), head_motion_alpha: str = Form(""), sampling_timesteps: str = Form("")):
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
        setup_kwargs = {"overall_ctrl_info": {"alpha_pitch": hm_alpha, "alpha_yaw": hm_alpha, "alpha_roll": hm_alpha}}
        try:
            _sts = int((sampling_timesteps or "").strip())
            if _sts > 0:
                setup_kwargs["sampling_timesteps"] = _sts
        except (TypeError, ValueError):
            pass
        s.setup(resolve_avatar(avatar), out_path, **setup_kwargs)
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



CHUNK_WINDOW = 6480  # 0.4s + 80 pad (samples @16k)
CHUNK_ADVANCE = 3200  # 5 frames * 0.04s * 16000
CHUNK_PRE_PAD = 2000
CHUNK_POST_PAD = 6480

INGEST_LEAD = 1.0  # seconds to submit each chunk before the previous one runs out


def ingest_worker(st):
    start = time.time()
    next_submit = 0.0
    idx = 0
    try:
        while True:
            item = st["q"].get()
            if item is None:
                break
            audio_data = item
            duration = len(audio_data) / 16000.0
            target = start + next_submit
            now = time.time()
            wait = max(0.0, target - now)
            if wait > 0:
                time.sleep(wait)
            idx += 1
            print(f"\n{ts()} {YEL}[ingest]{RST} chunk {idx} dur={duration:.2f}s wait={wait:.2f}s", flush=True)
            try:
                st["sdk"].writer.write_audio(audio_data)
            except Exception:
                pass
            with lock:
                st["pending"] = np.concatenate([st["pending"], audio_data])
                while st["pos"] + CHUNK_WINDOW <= len(st["pending"]):
                    window = st["pending"][st["pos"] : st["pos"] + CHUNK_WINDOW]
                    st["sdk"].run_chunk(window)
                    st["pos"] += CHUNK_ADVANCE
            next_submit += duration - INGEST_LEAD
    except Exception as e:
        print(f"\n{ts()} {YEL}[ingest]{RST} worker error: {e}", flush=True)


streams = {}

@app.post("/api/stream/start")
async def stream_start(avatar: str = Form(""), head_motion_alpha: str = Form(""), sampling_timesteps: str = Form(""), n_d: str = Form("1500"), avatar_id: str = Form("")):
    rid = uuid.uuid4().hex
    out_path = os.path.join(OUTPUT_DIR, rid + ".mp4")
    try:
        hm_alpha = float(head_motion_alpha) if head_motion_alpha.strip() else HEAD_MOTION_ALPHA
    except (TypeError, ValueError):
        hm_alpha = HEAD_MOTION_ALPHA
    try:
        sts = int(sampling_timesteps.strip()) if sampling_timesteps.strip() else 50
    except (TypeError, ValueError):
        sts = 50
    try:
        nd = int(n_d.strip()) if n_d.strip() else 1500
    except (TypeError, ValueError):
        nd = 1500
    avatar_id = (avatar_id or "").strip()
    source_path = resolve_avatar(avatar)
    source_info = None
    if avatar_id:
        source_info = avatar_cache.load(avatar_id, source_path)
    t0 = time.time()
    with lock:
        s = ensure_sdk()
        t1 = time.time()
        s.setup(source_path, out_path,
                source_info=source_info,
                online_mode=True,
                movflags="frag_keyframe+empty_moov+default_base_moof",
                sampling_timesteps=sts,
                overall_ctrl_info={"alpha_pitch": hm_alpha, "alpha_yaw": hm_alpha, "alpha_roll": hm_alpha})
        t2 = time.time()
        if avatar_id and source_info is None:
            avatar_cache.store(avatar_id, s.source_info, source_path)
        s.setup_Nd(N_d=nd, fade_in=-1, fade_out=-1, ctrl_info={})
        # pre-roll silence so the online warm-up eats silence instead of the reply audio
        pre_roll = np.zeros(CHUNK_PRE_PAD + 51200, dtype=np.float32)
        pr_pos = 0
        while pr_pos + CHUNK_WINDOW <= len(pre_roll):
            s.run_chunk(pre_roll[pr_pos : pr_pos + CHUNK_WINDOW])
            pr_pos += CHUNK_ADVANCE
        t3 = time.time()
        print(f"\n{ts()} [ditto] sdk={t1-t0:.2f}s setup={t2-t1:.2f}s pre-roll={t3-t2:.2f}s total={t3-t0:.2f}s avatar_cache={'hit' if source_info is not None else 'miss'}", flush=True)
        streams[rid] = {"sdk": s, "out_path": out_path, "tmp_path": out_path + ".tmp.mp4", "done": False, "pending": np.zeros(CHUNK_PRE_PAD, dtype=np.float32), "pos": 0, "finalized": False, "q": queue.Queue(), "worker": None}
        _st = streams[rid]
        _st["worker"] = threading.Thread(target=ingest_worker, args=(_st,), daemon=True)
        _st["worker"].start()
        _st["monitor"] = threading.Thread(target=file_monitor, args=(_st,), daemon=True)
        _st["monitor"].start()
    return {"ok": True, "stream_id": rid, "avatar_cached": source_info is not None}


@app.post("/api/stream/{sid}/audio")
async def stream_audio(sid: str, audio: UploadFile = File(...), final: str = Form("0")):
    st = streams.get(sid)
    if not st:
        raise HTTPException(404, "unknown stream")
    tmp = os.path.join(INPUT_DIR, sid + "_chunk.wav")
    with open(tmp, "wb") as f:
        f.write(await audio.read())
    audio_data, sr = librosa.load(tmp, sr=16000)
    audio_data = audio_data.astype(np.float32)
    st["q"].put(audio_data)
    frames = int(getattr(st["sdk"], "gen_frame_idx", 0) or 0)
    return {"ok": True, "frames": frames}


@app.post("/api/stream/{sid}/end")
async def stream_end(sid: str):
    st = streams.get(sid)
    if not st:
        raise HTTPException(404, "unknown stream")
    st["q"].put(None)
    if st["worker"] is not None:
        st["worker"].join()
    with lock:
        if not st.get("finalized"):
            st["pending"] = np.concatenate([st["pending"], np.zeros(CHUNK_POST_PAD, dtype=np.float32)])
            while st["pos"] + CHUNK_WINDOW <= len(st["pending"]):
                window = st["pending"][st["pos"] : st["pos"] + CHUNK_WINDOW]
                st["sdk"].run_chunk(window)
                st["pos"] += CHUNK_ADVANCE
            st["sdk"].finalize()
            st["finalized"] = True
        st["sdk"].close()
    st["done"] = True
    duration = 0.0
    try:
        probe = 'ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "{}"'.format(st["tmp_path"])
        out = subprocess.run(probe, shell=True, capture_output=True, text=True)
        duration = float((out.stdout or "").strip() or "0")
    except Exception:
        pass
    frames = int(getattr(st["sdk"], "gen_frame_idx", 0) or 0)
    offset = (frames * 640 - int(st["pos"])) / 16000.0
    return {"ok": True, "tmp_path": st["tmp_path"], "duration": duration, "frames": frames, "offset": max(0.0, offset)}


@app.get("/api/stream/{sid}/video")
async def stream_video(sid: str, request: Request):
    st = streams.get(sid)
    if not st or not os.path.exists(st["tmp_path"]):
        return JSONResponse({"ok": False}, status_code=404)
    path = st["tmp_path"]
    size = os.path.getsize(path)
    print(f"\n{ts()} [video] size={size}", flush=True)
    rng = request.headers.get("range", "")
    if rng.startswith("bytes="):
        try:
            start = int(rng[6:].split("-", 1)[0] or "0")
        except ValueError:
            start = 0
        if start >= size:
            return Response(content=b"", status_code=416, headers={"Content-Range": f"bytes */{size}"})
        def gen():
            with open(path, "rb") as f:
                f.seek(start)
                left = size - start
                while left > 0:
                    b = f.read(min(65536, left))
                    if not b:
                        break
                    left -= len(b)
                    yield b
        return StreamingResponse(gen(), media_type="video/mp4", status_code=206,
                                 headers={"Content-Length": str(size - start), "Content-Range": f"bytes {start}-{size - 1}/{size}"})
    return FileResponse(path, media_type="video/mp4")

@app.get("/api/health")
def health():
    return {"ok": True, "model_loaded": sdk is not None}

if __name__ == "__main__":
    import uvicorn
    import logging
    LOG_CONFIG = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {"format": "%(asctime)s %(levelname)s: %(message)s", "datefmt": "%H:%M:%S"},
            "access": {"format": "%(asctime)s %(levelname)s: %(message)s", "datefmt": "%H:%M:%S"},
        },
        "handlers": {
            "default": {"class": "logging.StreamHandler", "formatter": "default", "stream": "ext://sys.stderr"},
            "access": {"class": "logging.StreamHandler", "formatter": "access", "stream": "ext://sys.stdout"},
        },
        "loggers": {
            "uvicorn": {"handlers": ["default"], "level": "INFO"},
            "uvicorn.error": {"handlers": ["default"], "level": "INFO"},
            "uvicorn.access": {"handlers": ["access"], "level": "INFO", "propagate": False},
        },
    }
    uvicorn.run(app, host="0.0.0.0", port=8902, log_config=LOG_CONFIG)
