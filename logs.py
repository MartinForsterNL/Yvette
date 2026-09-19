"""Simple timestamped file logger for model/inference events.

Keeps a single append-only log file so the admin Status tab can show model
loading, loading errors, and inference errors. Retention trimming is done by
the caller (server.py) via trim_log().
"""
import datetime
import os
import threading

_LOCK = threading.Lock()
ROOT = os.path.dirname(os.path.abspath(__file__))


def _log_path() -> str:
    return os.path.join(ROOT, "logs", "system.log")


def log_event(level: str, msg: str) -> None:
    """Append a timestamped log line (best-effort, never raises)."""
    try:
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"{ts} [{level.upper()}] {msg}\n"
        p = _log_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with _LOCK:
            with open(p, "a", encoding="utf-8") as f:
                f.write(line)
    except Exception:
        pass


def read_log(max_lines: int = 500) -> str:
    """Return the most recent log lines, newest first."""
    p = _log_path()
    try:
        with open(p, "r", encoding="utf-8") as f:
            lines = f.readlines()
        lines = lines[-max_lines:]
        lines.reverse()
        return "".join(lines)
    except OSError:
        return ""


def clear_log() -> bool:
    p = _log_path()
    try:
        if os.path.exists(p):
            os.remove(p)
        return True
    except OSError:
        return False


def trim_log(retention_hours: float) -> None:
    """Drop log lines older than retention_hours (0 = keep forever)."""
    if retention_hours <= 0:
        return
    p = _log_path()
    if not os.path.exists(p):
        return
    try:
        with open(p, "r", encoding="utf-8") as f:
            lines = f.readlines()
        cutoff = datetime.datetime.now() - datetime.timedelta(hours=retention_hours)
        kept = []
        for line in lines:
            try:
                ts = datetime.datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S")
                if ts >= cutoff:
                    kept.append(line)
            except ValueError:
                kept.append(line)
        with _LOCK:
            with open(p, "w", encoding="utf-8") as f:
                f.writelines(kept)
    except Exception:
        pass
