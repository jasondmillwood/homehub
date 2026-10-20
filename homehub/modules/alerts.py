"""Alerts module: ntfy push (self-hosted ntfy or ntfy.sh). Watches every tile; sends one message when a tile
reaches min_level, a reminder every repeat_after_min while it stays there, and one when it recovers.
Messages carry only the tile name and its short status (no IPs, serials or paths). On public ntfy.sh the
topic name acts as a password: use a long random one (the installer generates it)."""
import json, os, threading, time, urllib.request

from ..config import secret
from ..core import Module, tiles
from ..util import RANK, read_json, write_json_atomic


def in_quiet(spec, now=None):
    """'22:00-07:00' -> True inside that local window."""
    if not spec or "-" not in spec:
        return False
    a, b = spec.split("-", 1)
    t = time.strftime("%H:%M", time.localtime(now or time.time()))
    return (a <= t < b) if a <= b else (t >= a or t < b)


class Alerts(Module):
    name, title, order = "alerts", "Alerts", 90

    def __init__(self, ctx):
        super().__init__(ctx)
        self.state_file = os.path.join(ctx.state_dir, "alerts.json")
        self.sent = []          # (title, body, priority) log of the last messages, for the tile / tests
        self.sender = None      # tests inject a fake

    def start(self):
        self.ctx.every("alerts", 60, self.evaluate, first_delay=90)   # let the other modules collect first

    def send(self, title, body, priority="default", tags=""):
        self.sent = (self.sent + [(title, body, priority)])[-20:]
        if self.sender:
            return self.sender(title, body, priority, tags)
        if self.ctx.demo:
            return True
        url = self.cfg["ntfy_url"].rstrip("/") + "/" + self.cfg["ntfy_topic"]
        h = {"Title": title.encode("utf-8").decode("latin-1", "replace"), "Priority": priority, "Tags": tags}
        tok = secret(self.ctx.cfg, "ntfy_token_file")
        if tok:
            h["Authorization"] = f"Bearer {tok}"
        req = urllib.request.Request(url, data=body.encode(), headers=h, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            return 200 <= r.status < 300

    def evaluate(self, now=None):
        now = now or time.time()
        st = read_json(self.state_file) or {}
        floor = RANK[self.cfg.get("min_level", "red")]
        quiet = in_quiet(self.cfg.get("quiet_hours"), now)
        seen = set()
        for t in tiles(self.ctx)["tiles"]:
            if t.get("module") == "alerts":
                continue
            k, lvl = t["id"], t.get("level", "gray")
            seen.add(k)
            prev = st.get(k) or {}
            bad = RANK.get(lvl, 0) >= floor and lvl != "gray"
            line = next((l["text"] for l in t.get("lines") or [] if l.get("level") == lvl), t.get("status", ""))
            if bad:
                due = not prev.get("bad") or prev.get("level") != lvl or now - prev.get("sent", 0) >= self.cfg.get("repeat_after_min", 360) * 60
                if due and not (quiet and lvl != "red"):
                    self.send(f"{t['title']}: {t.get('status', lvl)}", line[:200], "high" if lvl == "red" else "default",
                              "rotating_light" if lvl == "red" else "warning")
                    prev = {"bad": True, "level": lvl, "sent": now}
                else:
                    prev = dict(prev, bad=True, level=lvl)
            elif prev.get("bad"):
                if prev.get("sent"):
                    self.send(f"{t['title']}: OK again", line[:200] or "recovered", "low", "white_check_mark")
                prev = {"bad": False, "level": lvl}
            st[k] = prev
        for k in set(st) - seen:
            st.pop(k)
        write_json_atomic(self.state_file, st)
        return st

    def tile(self):
        return None

    def test(self, req):
        if not (req.token_ok() or req.session_ok()):
            return 403, {"error": "needs the hub token or a dashboard session"}
        try:
            return 200, {"ok": bool(self.send("homehub test", "If you can read this, ntfy alerts work.", "low", "bell"))}
        except Exception as e:
            return 502, {"ok": False, "error": str(e)[:150]}

    def routes(self):
        return {("POST", "/api/alerts/test"): self.test}


MODULE = Alerts
