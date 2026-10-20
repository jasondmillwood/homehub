"""Starlink module: reads dish-monitor's API (monitor_url). It never talks to a dish itself, so there is one
copy of the dish code (dish-monitor) whether it runs on this machine or another one."""
import json, time, urllib.error, urllib.request

from ..core import Module
from ..util import fmt_age
from . import sl_history

KEYS = ("slot", "label", "freshness", "reachable", "state", "pingMs", "downMbps", "upMbps", "dropRate",
        "obstructionPct", "softwareVersion", "hardwareVersion", "error", "lastLiveAt", "uptimeSeconds")


class Starlink(Module):
    name, title, order = "starlink", "Dish", 10

    def __init__(self, ctx):
        super().__init__(ctx)
        self.base = self.cfg["monitor_url"].rstrip("/")
        self.cache = {"t": 0.0, "v": None, "err": None}
        self.last_poll = {}

    # ---- source ----
    def get(self, path, timeout=6):
        if self.ctx.demo:
            from .. import demo
            return demo.monitor(path)
        with urllib.request.urlopen(self.base + path, timeout=timeout) as r:
            return json.load(r)

    def status(self, max_age=3):
        c = self.cache
        if time.time() - c["t"] < max_age:
            return c["v"], c["err"]
        try:
            v, err = self.get("/api/status", 4), None
        except Exception as e:
            v, err = None, str(e)[:150]
        c.update(t=time.time(), v=v, err=err)
        return v, err

    # ---- background ----
    def init_db(self, db):
        db.executescript(sl_history.SCHEMA)

    def start(self):
        self.ctx.every("starlink-poll", int(self.cfg.get("poll_interval_s", 60)), self.poll_once, first_delay=2)

    def poll_once(self):
        st, err = self.status(max_age=0)
        now = int(time.time())
        with self.ctx.db() as db:
            if st:
                db.executemany("INSERT INTO sl_sample VALUES(?,?,?,?,?,?,?,?,?,?,?)", [
                    (now, d.get("slot"), int(bool(d.get("reachable"))), d.get("freshness"), d.get("state"), d.get("downMbps"),
                     d.get("upMbps"), d.get("pingMs"), d.get("dropRate"), d.get("obstructionPct"), d.get("error"))
                    for d in st.get("dishes") or []])
            db.execute("DELETE FROM sl_sample WHERE ts < ?", (now - int(self.cfg.get("retain_days", 90)) * 86400,))
            db.execute("DELETE FROM sl_minute WHERE minute_utc < ?", (now - int(self.cfg.get("retain_days", 90)) * 86400,))
            db.commit()
            if not st or not self.cfg.get("history", True):
                return
            for d in st.get("dishes") or []:
                slot = d.get("slot")
                if d.get("freshness") != "LIVE":
                    self.last_poll[slot] = "away (not LIVE)"
                    continue
                try:
                    r = self.get(f"/api/dish/{slot}/history", 10)
                except urllib.error.HTTPError as e:
                    self.last_poll[slot] = f"history HTTP {e.code}"
                    continue
                h = r.get("history") or {}
                if not h.get("pop_ping_drop_rate"):
                    self.last_poll[slot] = "empty history"
                    continue
                res = sl_history.ingest_history(db, slot, h, poll_utc=(r.get("fetchedAt") or time.time() * 1000) / 1000)
                self.last_poll[slot] = f"+{res['new_samples']} samples, {res['new_outages']} new outages"

    # ---- views ----
    def outages(self, slot, hours=24):
        with self.ctx.db() as db:
            return [dict(r) for r in db.execute(
                "SELECT start_utc, duration_s, cause FROM sl_outage WHERE dish=? AND start_utc>=? ORDER BY start_utc DESC",
                (slot, time.time() - hours * 3600))]

    def tile(self):
        st, err = self.status()
        if not st:
            return [{"id": "starlink", "title": "Dish", "level": "red", "status": "monitor unreachable",
                     "lines": [{"text": f"dish-monitor at {self.base}: {err}", "level": "red"}]}]
        out = []
        for d in st.get("dishes") or []:
            slot = d.get("slot")
            live = d.get("freshness") == "LIVE"
            o = self.outages(slot)
            down_s = sum(x["duration_s"] for x in o)
            lvl = "gray" if not live else ("green" if d.get("state") == "CONNECTED" else "red")
            if live and lvl == "green" and ((d.get("dropRate") or 0) > 0.05 or down_s > 600):
                lvl = "amber"
            f = lambda v, n=0: "—" if v is None else f"{v:.{n}f}"
            lines = [
                {"text": f"{d.get('state') or '?'}" + ("" if live else f" · last live {d.get('lastLiveAt') or '—'}"), "level": lvl},
                {"text": f"↓ {f(d.get('downMbps'))} Mbps · ↑ {f(d.get('upMbps'))} Mbps · {f(d.get('pingMs'))} ms", "level": None},
                {"text": f"{len(o)} outage(s) in 24 h" + (f" · {int(down_s)} s total" if o else ""),
                 "level": "amber" if down_s > 600 else ("green" if not o else None)},
            ]
            out.append({"id": f"starlink-{slot}", "title": d.get("label") or slot, "level": lvl,
                        "status": "LIVE" if live else (d.get("freshness") or "?"), "lines": lines,
                        "detail": {"kv": [[k, d.get(k)] for k in KEYS], "outages": o[:50],
                                   "history": self.last_poll.get(slot, "—")}})
        return out

    def routes(self):
        return {("GET", "/api/starlink/status"): lambda req: (200, self.status()[0] or {"error": self.status()[1]}),
                ("GET", "/api/starlink/outages"): lambda req: (200, self.outages(req.q("dish", ""), req.qi("hours", 24, 1, 2160)))}


MODULE = Starlink
