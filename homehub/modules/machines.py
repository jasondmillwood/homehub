"""Machines module: reachability (TCP ports, with ping as a fallback) and an optional health JSON that a
machine pushes (POST /api/machines/push). Linux, macOS or Windows can push;
the hub only reads the JSON: {"ts": unix, "uptime_s": .., "load": .., "disk_free_pct": .., "msg": ".."}.

Push auth (v2):
- Legacy (default): the one hub token (X-Hub-Token) and ?host=<name>.
- Per-host: when any push host sets "token_file", every push host must use its own token. The host is the one whose
  token matched; ?host= is optional and, if given, must name that same host (403 otherwise). The hub token is then
  refused for pushes (403). A push host without token_file cannot push in this mode.
Bodies over 16 KB (legacy) / 8 KB (per-host mode) get 413. The last 24 h of reports per host: GET /api/machines?history=<name>."""
import hmac, json, os, threading, time

from .. import config as C
from ..core import Module
from ..util import fmt_age, ping, read_json, tcp, worst, write_json_atomic


MAX_BODY = 8192          # per-host token mode
MAX_BODY_LEGACY = 16384  # legacy hub-token mode
HISTORY_S = 24 * 3600
HISTORY_MAX = 2000      # ~1 report/min for 24 h, plus slack


class Machines(Module):
    name, title, order = "machines", "Machines", 40

    def __init__(self, ctx):
        super().__init__(ctx)
        self.state, self.lock = {}, threading.Lock()

    def start(self):
        self.ctx.every("machines-check", int(self.cfg.get("check_interval_s", 30)), self.check_once)

    def check_once(self):
        for h in self.cfg.get("hosts") or []:
            if self.ctx.demo:
                up = {p: h["name"] != "printer" for p in h.get("ports") or []}
            else:
                up = {p: tcp(h["host"], p) for p in h.get("ports") or []}
                if not up:
                    up = {"ping": ping(h["host"])}
            with self.lock:
                self.state[h["name"]] = {"t": int(time.time()), "ports": up}

    def health_path(self, name):
        return os.path.join(self.ctx.status_dir, "machines", name + ".json")

    def history_path(self, name):
        return os.path.join(self.ctx.status_dir, "machines", name + ".history.jsonl")

    def push_hosts(self):
        return [h for h in self.cfg.get("hosts") or [] if h.get("push")]

    def per_host_mode(self):
        return any(h.get("token_file") for h in self.push_hosts())

    def who(self, req):
        """(status, host name or error). Host comes from the token that matched, never from ?host= alone."""
        sent = req.h.headers.get("X-Hub-Token", "")
        if not self.per_host_mode():
            if not req.token_ok():
                return 401, "token"
            name = req.q("host", "")
            if name not in {h["name"] for h in self.push_hosts()}:
                return 404, "unknown host (set push: true in config)"
            return 200, name
        match = None
        for h in self.push_hosts():
            tok = C.read_secret_file(h.get("token_file"))
            if sent and tok and hmac.compare_digest(sent.encode(), tok.encode()):
                match = h["name"]
        if not match:
            return (403, "hub token not accepted: this hub uses per-host push tokens") if sent and req.token_ok() else (401, "token")
        q = req.q("host")
        if q is not None and q != match:
            return 403, "token does not belong to host " + q
        return 200, match

    def add_history(self, name, hp, now):
        p, keep = self.history_path(name), []
        try:
            with open(p) as f:
                for line in f:
                    try:
                        x = json.loads(line)
                    except ValueError:
                        continue
                    if now - (x.get("ts") or 0) <= HISTORY_S:
                        keep.append(x)
        except OSError:
            pass
        keep = (keep + [hp])[-HISTORY_MAX:]
        tmp = p + ".tmp"
        with open(tmp, "w") as f:
            f.write("".join(json.dumps(x, separators=(",", ":")) + "\n" for x in keep))
        os.replace(tmp, p)

    def history(self, name):
        now, out = time.time(), []
        try:
            with open(self.history_path(name)) as f:
                for line in f:
                    try:
                        x = json.loads(line)
                    except ValueError:
                        continue
                    if now - (x.get("ts") or 0) <= HISTORY_S:
                        out.append(x)
        except OSError:
            pass
        return out

    def get(self, req):
        name = req.q("history")
        if name is None:
            return 200, self.summary()
        if name not in {h["name"] for h in self.push_hosts()}:
            return 404, {"error": "unknown push host"}
        return 200, {"host": name, "window_s": HISTORY_S, "reports": self.history(name)}

    def one(self, h, now):
        with self.lock:
            s = self.state.get(h["name"])
        if not s:
            return {"name": h["name"], "level": "gray", "summary": "not checked yet"}
        ports = s["ports"]
        ok = [p for p, v in ports.items() if v]
        lvl = "green" if ports and len(ok) == len(ports) else "amber" if ok else "red"
        summ = "up" if lvl == "green" else ("partly reachable" if ok else "unreachable")
        r = {"name": h["name"], "level": lvl, "summary": summ, "ports": {str(k): v for k, v in ports.items()}}
        if h.get("push"):
            hp = read_json(self.health_path(h["name"]))
            r["stale"] = not hp or now - (hp.get("ts") or 0) > int(h.get("push_max_age_s", 1800))
            if r["stale"]:
                r["level"] = worst([lvl, "amber"])
                r["summary"] += " · health report " + ("missing" if not hp else f"stale ({fmt_age(now - hp['ts'])})")
            else:
                r["health"] = hp
                if isinstance(hp.get("disk_free_pct"), (int, float)) and hp["disk_free_pct"] < 10:
                    r["level"] = worst([r["level"], "amber"])
                    r["summary"] += f" · disk {hp['disk_free_pct']:.0f}% free"
        return r

    def summary(self):
        now = time.time()
        ms = [self.one(h, now) for h in self.cfg.get("hosts") or []]
        return {"level": worst([m["level"] for m in ms]), "machines": ms}

    def tile(self):
        s = self.summary()
        up = sum(1 for m in s["machines"] if m["level"] == "green")
        return {"level": s["level"], "status": f"{up}/{len(s['machines'])} up",
                "lines": [{"text": f"{m['name']}: {m['summary']}", "level": m["level"]} for m in s["machines"]]
                or [{"text": "no machines configured", "level": "gray"}], "detail": s}

    def push(self, req):
        code, who = self.who(req)
        if code != 200:
            return code, {"error": who}
        try:
            n = int(req.h.headers.get("Content-Length") or 0)
        except ValueError:
            return 400, {"error": "bad Content-Length"}
        cap = MAX_BODY if self.per_host_mode() else MAX_BODY_LEGACY
        if n > cap:
            req.h.close_connection = True      # body not read
            return 413, {"error": f"body over {cap} bytes"}
        try:
            hp = json.loads(req.body(cap))
        except ValueError:
            return 400, {"error": "json"}
        if not isinstance(hp, dict):
            return 400, {"error": "json object expected"}
        now = int(time.time())
        hp["ts"] = now
        with self.lock:
            write_json_atomic(self.health_path(who), hp)
            self.add_history(who, hp, now)
        return 200, {"ok": True, "host": who}

    def routes(self):
        return {("GET", "/api/machines"): self.get, ("POST", "/api/machines/push"): self.push}


MODULE = Machines
