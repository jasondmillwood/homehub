"""Machines module: reachability (TCP ports, with ping as a fallback) and an optional health JSON that a
machine pushes with the hub token (POST /api/machines/push?host=<name>). Linux, macOS or Windows can push;
the hub only reads the JSON: {"ts": unix, "uptime_s": .., "load": .., "disk_free_pct": .., "msg": ".."}."""
import json, os, threading, time

from ..core import Module
from ..util import fmt_age, ping, read_json, tcp, worst, write_json_atomic


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
            if not hp or now - (hp.get("ts") or 0) > int(h.get("push_max_age_s", 1800)):
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
        if not req.token_ok():
            return 401, {"error": "token"}
        name = req.q("host", "")
        if name not in {h["name"] for h in self.cfg.get("hosts") or [] if h.get("push")}:
            return 404, {"error": "unknown host (set push: true in config)"}
        try:
            hp = json.loads(req.body(65536))
        except ValueError:
            return 400, {"error": "json"}
        if not isinstance(hp, dict):
            return 400, {"error": "json object expected"}
        hp["ts"] = int(time.time())
        write_json_atomic(self.health_path(name), hp)
        return 200, {"ok": True}

    def routes(self):
        return {("GET", "/api/machines"): lambda req: (200, self.summary()), ("POST", "/api/machines/push"): self.push}


MODULE = Machines
