import os, sys, shutil

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
os.chdir(BASE_DIR)

import numpy as np
import librosa
from stream_pipeline_offline import StreamSDK

img_path = sys.argv[1]
out_path = sys.argv[2]
alpha = float(sys.argv[3]) if len(sys.argv) > 3 else 1.5
length = float(sys.argv[4]) if len(sys.argv) > 4 else 60.0

DATA_ROOT = os.environ.get("DITTO_DATA_ROOT", "checkpoints/ditto_trt_custom")

# generate a breathing murmur of the requested length
sr = 16000
t = np.arange(int(sr * length)) / sr
breath = 0.5 + 0.35 * np.sin(2 * np.pi * 0.25 * t)
freq = 150 + 15 * np.sin(2 * np.pi * 0.12 * t) + 8 * np.sin(2 * np.pi * 0.03 * t)
phase = 2 * np.pi * np.cumsum(freq) / sr
carrier = np.sin(phase) + 0.4 * np.sin(2 * phase) + 0.2 * np.sin(3 * phase)
audio_data = (0.06 * breath * carrier).astype(np.float32)

num_f = int(np.ceil(len(audio_data) / sr * 25))

sdk = StreamSDK("checkpoints/ditto_cfg/v0.4_hubert_cfg_trt.pkl", DATA_ROOT)
sdk.setup(img_path, out_path, overall_ctrl_info={"alpha_pitch": alpha, "alpha_yaw": alpha, "alpha_roll": alpha})
sdk.setup_Nd(N_d=num_f, fade_in=-1, fade_out=-1, ctrl_info={})
aud_feat = sdk.wav2feat.wav2feat(audio_data)
sdk.audio2motion_queue.put(aud_feat)
sdk.close()
tmp = out_path + ".tmp.mp4"
if os.path.exists(tmp):
    shutil.move(tmp, out_path)
    print("OK", out_path)
else:
    print("FAILED")
