"""Small shared helpers (stdlib only)."""
import json, os, socket, subprocess, time

RANK = {"gray": 0, "green": 0, "amber": 1, "red": 2}


def worst(levels, default="gray"):
    levels = [l for l in levels if l]
    if not levels:
        return default
    if all(l == "gray" for l in levels):
        return "gray"
    return max(levels, key=lambda l: RANK.get(l, 0))


def sh(cmd, timeout=8):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def tcp(host, port, t=1.5):
    try:
        with socket.create_connection((host, port), timeout=t):
            return True
    except OSError:
        return False


def fmt_age(sec):
    if sec is None:
        return "—"
    sec = int(sec)
    if sec < 90:
        return f"{sec} s ago"
    if sec < 5400:
        return f"{sec // 60} min ago"
    if sec < 172800:
        return f"{sec // 3600} h ago"
    return f"{sec // 86400} d ago"


def when(ts, now=None):
    now = now or time.time()
    lt, today = time.localtime(ts), time.localtime(now)
    hm = time.strftime("%H:%M", lt)
    if lt[:3] == today[:3]:
        return f"today {hm}"
    if lt[:3] == time.localtime(now - 86400)[:3]:
        return f"yesterday {hm}"
    return time.strftime("%b %d %H:%M", lt)


def hbytes(n):
    if not isinstance(n, (int, float)) or n < 0:
        return "—"
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or u == "TB":
            return f"{n:.0f} {u}" if u in ("B", "KB") or n >= 100 else f"{n:.1f} {u}"
        n /= 1000


def read_json(path):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_json_atomic(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def ping(host, timeout_s=1):
    try:
        return subprocess.run(["ping", "-c1", f"-W{timeout_s}", host], capture_output=True, timeout=timeout_s + 2).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
