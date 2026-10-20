"""Explain module (off by default): asks xAI's Grok to explain the current problems in plain English.
Only on demand (button), at most once per 5 minutes and 30 times a day. The digest is redacted (no IPs,
serials, paths or hostnames) and the API key stays on the server (secrets.xai_api_key_file)."""
import hashlib, json, re, socket, threading, time, urllib.request

from ..config import secret
from ..core import Module, tiles

SYSTEM = ("You explain a home monitoring dashboard to its owner. Plain English, short. Use only the facts in the "
          "digest; if unsure, say what to check. Prefer safe, reversible fixes and flag anything that needs root or a "
          "purchase. At most 3 issues, most urgent first. If everything is OK, say so in one line.")
REDACT = [(re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "<ip>"), (re.compile(r"(?i)\b(serial|sn)\b\W*[:=]?\s*\w+"), r"\1 <redacted>"),
          (re.compile(r"(?:/[\w.-]+){2,}"), "<path>")]
MIN_INTERVAL_S, DAILY_CAP = 300, 30


def private_names(cfg):
    """Hostnames to hide: this machine and every configured machine host."""
    names = {socket.gethostname(), socket.gethostname().split(".")[0]}
    for h in cfg["modules"].get("machines", {}).get("hosts", []):
        names.add(str(h.get("host", "")))
    return sorted((n for n in names if len(n) >= 3), key=len, reverse=True)


def digest(t, names=()):
    lines = []
    for x in t["tiles"]:
        if x.get("module") == "explain":
            continue
        lines.append(f"[{x['title']}] {x.get('level')}: {x.get('status', '')}")
        lines += [f"  - {l['text']}" for l in x.get("lines") or [] if l.get("level") in ("amber", "red")]
    s = "\n".join(lines)
    for rx, rep in REDACT:
        s = rx.sub(rep, s)
    for n in names:
        s = re.sub(rf"(?i)\b{re.escape(n)}\b", "<host>", s)
    return s


class Explain(Module):
    name, title, order = "explain", "Explain", 80

    def __init__(self, ctx):
        super().__init__(ctx)
        self.last = {"t": 0, "text": None, "error": None, "fp": None}
        self.day = {"d": None, "n": 0}
        self.lock = threading.Lock()
        self.poster = None   # tests inject a fake

    def call(self, text):
        if self.poster:
            return self.poster(text)
        key = secret(self.ctx.cfg, "xai_api_key_file")
        if not key:
            raise RuntimeError("xAI API key file missing or empty")
        body = {"model": self.cfg["model"], "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": text}],
                "max_tokens": 500}
        req = urllib.request.Request(self.cfg["api_base"].rstrip("/") + "/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)["choices"][0]["message"]["content"].strip()

    def explain(self, now=None):
        now = now or time.time()
        d = digest(tiles(self.ctx), private_names(self.ctx.cfg))
        fp = hashlib.sha256(d.encode()).hexdigest()[:16]
        with self.lock:
            if self.last["text"] and (fp == self.last["fp"] or now - self.last["t"] < MIN_INTERVAL_S):
                return dict(self.last, cached=True)
            today = time.strftime("%Y-%m-%d", time.localtime(now))
            if self.day["d"] != today:
                self.day = {"d": today, "n": 0}
            if self.day["n"] >= DAILY_CAP:
                return dict(self.last, error="daily limit reached")
            self.day["n"] += 1
            try:
                self.last = {"t": now, "text": self.call(d), "error": None, "fp": fp}
            except Exception as e:
                self.last = dict(self.last, error=str(e)[:200], t=now)
            return dict(self.last, cached=False)

    def tile(self):
        l = self.last
        return {"level": "gray", "status": "Grok", "action": {"label": "Explain", "post": "/api/explain"},
                "lines": [{"text": l["text"] or (l["error"] and f"error: {l['error']}") or "Ask Grok what needs attention.", "level": None}]}

    def post(self, req):
        if not (req.token_ok() or req.session_ok()):
            return 403, {"error": "needs the hub token or a dashboard session"}
        r = self.explain()
        return 200, {k: r.get(k) for k in ("text", "error", "cached", "t")}

    def routes(self):
        return {("POST", "/api/explain"): self.post}


MODULE = Explain
