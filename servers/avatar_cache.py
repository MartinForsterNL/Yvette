import os
import json
import pickle
import time


class AvatarCache:
    """On-disk cache for DITTO avatar registration results, keyed by avatar_id.

    The JSON index maps avatar_id -> a pickled source_info file. Registration
    is skipped on a cache hit, which cuts the per-stream setup cost when the
    avatar does not change.
    """

    def __init__(self, cache_dir):
        self.cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)
        self.index_path = os.path.join(cache_dir, "index.json")
        self.index = self._load_index()

    def _load_index(self):
        if os.path.exists(self.index_path):
            try:
                with open(self.index_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def _save_index(self):
        tmp = self.index_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.index, f, indent=2)
            os.replace(tmp, self.index_path)
        except Exception:
            pass

    def _data_path(self, avatar_id):
        safe = "".join(c for c in avatar_id if c.isalnum() or c in "-_") or "default"
        return os.path.join(self.cache_dir, safe + ".pkl")

    def load(self, avatar_id, source_path):
        entry = self.index.get(avatar_id)
        if not entry:
            return None
        # stale if the underlying avatar file moved or changed on disk
        if entry.get("source_path") != source_path:
            return None
        try:
            mtime = int(os.path.getmtime(source_path))
        except OSError:
            return None
        if entry.get("mtime") != mtime:
            return None
        data_path = entry.get("data")
        if not data_path or not os.path.exists(data_path):
            return None
        try:
            with open(data_path, "rb") as f:
                return pickle.load(f)
        except Exception:
            return None

    def store(self, avatar_id, source_info, source_path):
        data_path = self._data_path(avatar_id)
        tmp = data_path + ".tmp"
        try:
            with open(tmp, "wb") as f:
                pickle.dump(source_info, f, protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(tmp, data_path)
        except Exception:
            return False
        try:
            mtime = int(os.path.getmtime(source_path))
        except OSError:
            mtime = 0
        self.index[avatar_id] = {
            "source_path": source_path,
            "mtime": mtime,
            "data": data_path,
            "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        self._save_index()
        return True
