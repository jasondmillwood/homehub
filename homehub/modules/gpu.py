"""GPU module: NVIDIA GPU via nvidia-smi, plus optional Folding@home (v8 client) state over its local
WebSocket API and optional public account stats from api.foldingathome.org. Whitelisted fields only:
the F@h passkey and account token are never read out."""
import base64, json, os, shutil, socket, struct, threading, time, urllib.request
from datetime import datetime, timezone
from urllib.parse import quote, urlparse

from ..core import Module
from ..util import read_json, sh, write_json_atomic

FAH_STATS_URL = "https://api.foldingathome.org"
FAH_UNIT_STATE = {"RUN": "Folding", "DOWNLOAD": "Downloading", "UPLOAD": "Uploading", "ASSIGN": "Assigning", "CORE": "Getting core",
                  "FINISH": "Finishing", "CLEAN": "Cleaning up", "WAIT": "Waiting", "DUMP": "Dumping"}


def ws_first_message(url, timeout=4, limit=8 << 20):
    """Minimal stdlib WebSocket client: handshake, read one (possibly fragmented) text message, close.
    fah-client v8 sends its full state as the first message after the upgrade."""
    u = urlparse(url)
    host, port = u.hostname, u.port or 80
    with socket.create_connection((host, port), timeout=timeout) as s:
        s.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        s.sendall((f"GET {u.path or '/'} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                   f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        f = s.makefile("rb")
        line = f.readline()
        if b" 101" not in line: raise RuntimeError("websocket handshake failed: " + line.decode("latin1").strip()[:80])
        while f.readline() not in (b"\r\n", b"\n", b""): pass
        def rd(n):
            b = f.read(n)
            if b is None or len(b) < n: raise RuntimeError("websocket closed early")
            return b
        msg = bytearray()
        while True:
            b0, b1 = rd(2)
            op, n = b0 & 0x0F, b1 & 0x7F
            if n == 126: n = struct.unpack(">H", rd(2))[0]
            elif n == 127: n = struct.unpack(">Q", rd(8))[0]
            mask = rd(4) if b1 & 0x80 else None
            if len(msg) + n > limit: raise RuntimeError("websocket message too large")
            data = rd(n) if n else b""
            if mask: data = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
            if op == 8: raise RuntimeError("websocket closed by the F@h client")
            if op in (0, 1, 2):            # ping/pong control frames are ignored
                msg += data
                if b0 & 0x80: break
        try: s.sendall(bytes([0x88, 0x80]) + os.urandom(4))   # masked close frame, empty payload
        except OSError: pass
        return json.loads(msg.decode("utf-8"))

def _iso_ts(v):
    try: return int(datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()) if v else None
    except ValueError: return None

def _utc_ts(v):   # api.foldingathome.org "last": "2026-10-19 18:02:22" (UTC)
    try: return int(datetime.strptime(v, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()) if v else None
    except (TypeError, ValueError): return None


def parse_nvidia(o):
    if not o:
        return {"available": False}
    f = [x.strip() for x in o.splitlines()[0].split(",")]
    num = lambda x: float(x) if x.replace(".", "", 1).isdigit() else None
    return {"available": True, "name": f[0], "util": num(f[1]), "tempC": num(f[2]), "powerW": num(f[3]),
            "memUsed": num(f[4]), "memTotal": num(f[5])}


def parse_fah(st):
    """Reduce fah-client v8 state to whitelisted fields + a level."""
    out = {"ok": False, "level": "red", "status": "client unreachable", "units": [], "ppd": None}
    if not st:
        return out
    info, cfg, groups = st.get("info") or {}, st.get("config") or {}, st.get("groups") or {}
    gcfg = [(g.get("config") or {}) for g in groups.values()]
    units = []
    for u in st.get("units") or []:
        asg = u.get("assignment") or {}
        prog = u.get("wu_progress") if u.get("wu_progress") is not None else u.get("progress")
        units.append({"state": u.get("state"), "paused": bool(u.get("paused")),
                      "status": "Paused" if u.get("paused") else FAH_UNIT_STATE.get(u.get("state"), u.get("state")),
                      "project": asg.get("project"), "progress": round(100 * prog, 1) if isinstance(prog, (int, float)) else None,
                      "eta": u.get("eta"), "ppd": u.get("ppd")})
    run = [u for u in units if u["state"] == "RUN" and not u["paused"]]
    paused = any(c.get("paused") for c in gcfg)
    if run:
        lvl, status = "green", "Folding"
    elif paused:
        lvl, status = "amber", "Paused"
    elif units:
        lvl, status = "amber", units[0]["status"]
    else:
        lvl, status = "amber", "No work unit"
    out.update(ok=True, level=lvl, status=status, units=units, ppd=sum(u["ppd"] or 0 for u in run) if run else None,
               client={"version": info.get("version"), "user": cfg.get("user"), "team": cfg.get("team"), "paused": paused})
    return out


class Gpu(Module):
    name, title, order = "gpu", "GPU", 50

    def __init__(self, ctx):
        super().__init__(ctx)
        self.fah_cache = {"t": 0.0, "v": None, "err": None}
        self.lock = threading.Lock()
        self.acct_file = os.path.join(ctx.state_dir, "fah-account.json")

    def start(self):
        if self.cfg.get("folding_at_home") and self.cfg.get("fah_stats") and not self.ctx.demo:
            self.ctx.every("fah-stats", 2700, self.fah_account_once, first_delay=30)

    def nvidia(self):
        if self.ctx.demo:
            from .. import demo
            return parse_nvidia(demo.NVIDIA)
        if not shutil.which("nvidia-smi"):
            return {"available": False}
        return parse_nvidia(sh(["nvidia-smi", "--query-gpu=name,utilization.gpu,temperature.gpu,power.draw,memory.used,memory.total",
                                "--format=csv,noheader,nounits"], 4))

    def fah_state(self, max_age=10):
        if self.ctx.demo:
            from .. import demo
            return demo.FAH
        with self.lock:
            c = self.fah_cache
            if time.time() - c["t"] < max_age:
                return c["v"]
            try:
                c.update(t=time.time(), v=ws_first_message(self.cfg["fah_ws"]), err=None)
            except Exception as e:
                c.update(t=time.time(), v=None, err=str(e)[:150])
            return c["v"]

    def fah_account_once(self):
        user = ((self.fah_state() or {}).get("config") or {}).get("user")
        if not user:
            return
        req = urllib.request.Request(f"{FAH_STATS_URL}/user/{quote(user, safe='')}", headers={"User-Agent": "homehub/0.1", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.load(r)
        write_json_atomic(self.acct_file, {"fetched": int(time.time()), "data": {k: d.get(k) for k in ("name", "score", "wus", "rank", "users", "last")}})

    def summary(self):
        g = self.nvidia()
        out = {"gpu": g}
        if self.cfg.get("folding_at_home"):
            f = parse_fah(self.fah_state())
            if not f["ok"] and self.fah_cache.get("err"):
                f["error"] = self.fah_cache["err"]
            acct = read_json(self.acct_file)
            if acct:
                f["account"] = acct
            out["fah"] = f
        return out

    def tile(self):
        s = self.summary()
        g, lines = s["gpu"], []
        f = lambda v: "—" if v is None else f"{v:.0f}"
        if g.get("available"):
            hot = (g.get("tempC") or 0) >= 85
            lines.append({"text": f"{g['name']}: {f(g['util'])}% · {f(g['tempC'])}°C · {f(g['powerW'])} W", "level": "amber" if hot else "green"})
            lvl = "amber" if hot else "green"
        else:
            lines.append({"text": "no NVIDIA GPU (nvidia-smi not found)", "level": "gray"})
            lvl = "gray"
        status = "OK" if lvl == "green" else "—"
        if "fah" in s:
            fa = s["fah"]
            lines.append({"text": f"Folding@home: {fa['status']}" + (f" · {fa['ppd']:,.0f} PPD" if fa.get("ppd") else ""), "level": fa["level"]})
            lvl = fa["level"] if lvl == "gray" else max((lvl, fa["level"]), key=lambda l: {"gray": 0, "green": 0, "amber": 1, "red": 2}[l])
            status = fa["status"]
        return {"level": lvl, "status": status, "lines": lines, "detail": s}

    def routes(self):
        return {("GET", "/api/gpu"): lambda req: (200, self.summary())}


MODULE = Gpu
