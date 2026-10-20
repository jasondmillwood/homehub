"""Speed test module. Two tools, chosen in config:
  - librespeed: LibreSpeed CLI (open source, LGPL-3.0, not bundled; the installer can fetch it).
  - ookla: Ookla Speedtest CLI (proprietary). Only used when you have read and accepted Ookla's EULA, privacy
    policy and terms (https://www.speedtest.net/about/eula) and set ookla_license_accepted=true. The installer
    downloads it only after you type "accept"; it is never bundled or redistributed.
Runs at the configured local times (and on demand); results go to the hub database."""
import json, os, shutil, statistics, subprocess, threading, time

from ..core import Module
from ..util import when

SCHEMA = """
CREATE TABLE IF NOT EXISTS speedtest (
  id INTEGER PRIMARY KEY, ts_utc INTEGER NOT NULL, tool TEXT NOT NULL, iface TEXT, server TEXT, ok INTEGER NOT NULL,
  ping_ms REAL, jitter_ms REAL, loss_pct REAL, dl_bps REAL, ul_bps REAL, bytes_used INTEGER, public_ip TEXT, note TEXT);
CREATE INDEX IF NOT EXISTS speedtest_time ON speedtest(ts_utc);
"""


def parse_ookla(j):
    """Ookla JSON: bandwidth in bytes/s, totals in bytes."""
    if j.get("type") != "result":
        raise ValueError(j.get("message") or "not a result")
    s = j.get("server") or {}
    return dict(server=f"{s.get('name', '?')} ({s.get('location', '?')})", ok=1, ping_ms=j["ping"]["latency"],
                jitter_ms=j["ping"].get("jitter"), loss_pct=j.get("packetLoss"), dl_bps=j["download"]["bandwidth"] * 8,
                ul_bps=j["upload"]["bandwidth"] * 8, bytes_used=j["download"]["bytes"] + j["upload"]["bytes"],
                public_ip=(j.get("interface") or {}).get("externalIp"), note=(j.get("result") or {}).get("url"))


def parse_librespeed(j):
    """librespeed-cli --json: a list with one object; download/upload in Mbps."""
    r = j[0] if isinstance(j, list) else j
    return dict(server=(r.get("server") or {}).get("name"), ok=1, ping_ms=r.get("ping"), jitter_ms=r.get("jitter"),
                loss_pct=None, dl_bps=(r.get("download") or 0) * 1e6, ul_bps=(r.get("upload") or 0) * 1e6,
                bytes_used=(r.get("bytes_sent") or 0) + (r.get("bytes_received") or 0),
                public_ip=(r.get("client") or {}).get("ip"), note=None)


class Speedtest(Module):
    name, title, order = "speedtest", "Speed test", 15

    def __init__(self, ctx):
        super().__init__(ctx)
        self.running = threading.Lock()
        self.last_slot = None

    def init_db(self, db):
        db.executescript(SCHEMA)

    def start(self):
        self.ctx.every("speedtest-schedule", 30, self.maybe_run)

    def binary(self):
        tool = self.cfg["tool"]
        b = self.cfg.get("binary") or shutil.which("speedtest" if tool == "ookla" else "librespeed-cli")
        if not b:
            for p in (os.path.join(os.path.dirname(self.ctx.db_path), "bin", "speedtest" if tool == "ookla" else "librespeed-cli"),):
                if os.access(p, os.X_OK):
                    b = p
        return b

    def command(self):
        tool, b, iface = self.cfg["tool"], self.binary(), self.cfg.get("iface")
        if not b:
            raise RuntimeError(f"{tool} binary not found (re-run the installer with --speedtest {tool})")
        if tool == "ookla":
            if not self.cfg.get("ookla_license_accepted"):
                raise RuntimeError("Ookla EULA not accepted in config")
            cmd = [b, "--format=json", "--accept-license", "--accept-gdpr"] + (["-I", iface] if iface else [])
        else:
            cmd = [b, "--json"] + (["--interface", iface] if iface else [])
        return cmd

    def maybe_run(self, now=None):
        lt = time.localtime(now or time.time())
        slot = time.strftime("%Y-%m-%d %H:%M", lt)
        if time.strftime("%H:%M", lt) in (self.cfg.get("times") or []) and slot != self.last_slot:
            self.last_slot = slot
            self.run_once()

    def run_once(self, runner=None):
        if not self.running.acquire(blocking=False):
            return {"ok": False, "error": "already running"}
        try:
            row = dict(ts_utc=int(time.time()), tool=self.cfg["tool"], iface=self.cfg.get("iface") or None, ok=0)
            try:
                if self.ctx.demo:
                    from .. import demo
                    out = demo.speedtest(self.cfg["tool"])
                else:
                    out = (runner or (lambda c: subprocess.run(c, capture_output=True, text=True, timeout=180).stdout))(self.command())
                j = json.loads(out.strip().splitlines()[-1] if self.cfg["tool"] == "ookla" else out)
                row.update(parse_ookla(j) if self.cfg["tool"] == "ookla" else parse_librespeed(j))
            except Exception as e:
                row["note"] = str(e)[:200]
            with self.ctx.db() as db:
                db.execute(f"INSERT INTO speedtest({','.join(row)}) VALUES ({','.join('?' * len(row))})", tuple(row.values()))
                db.commit()
            return row
        finally:
            self.running.release()

    def recent(self, days=14):
        with self.ctx.db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM speedtest WHERE ts_utc>=? ORDER BY ts_utc DESC",
                                                (time.time() - days * 86400,))]

    def tile(self):
        rs = self.recent()
        if not rs:
            return {"level": "gray", "status": "no tests yet", "lines": [
                {"text": f"{self.cfg['tool']} at {', '.join(self.cfg.get('times') or []) or 'manual only'}", "level": "gray"}]}
        last = rs[0]
        ok = [r for r in rs if r["ok"]]
        mbps = lambda b: f"{b / 1e6:.0f}" if b else "—"
        lines = [{"text": (f"↓ {mbps(last['dl_bps'])} · ↑ {mbps(last['ul_bps'])} Mbps · {last['ping_ms']:.0f} ms"
                           if last["ok"] else f"last test failed: {last['note']}"), "level": "green" if last["ok"] else "amber"},
                 {"text": f"{when(last['ts_utc'])} · {last['tool']}" + (f" · {last['server']}" if last.get("server") else ""), "level": None}]
        if len(ok) >= 3:
            lines.append({"text": f"14-day median ↓ {mbps(statistics.median(r['dl_bps'] for r in ok))} Mbps ({len(ok)} tests)", "level": None})
        return {"level": "green" if last["ok"] else "amber", "status": mbps(last["dl_bps"]) + " Mbps" if last["ok"] else "failed",
                "lines": lines, "detail": {"tests": rs[:50]}}

    def run_now(self, req):
        if not (req.token_ok() or req.session_ok()):
            return 403, {"error": "needs the hub token or a dashboard session"}
        threading.Thread(target=self.run_once, daemon=True).start()
        return 202, {"ok": True, "started": True}

    def routes(self):
        return {("GET", "/api/speedtest"): lambda req: (200, self.recent(req.qi("days", 14, 1, 400))),
                ("POST", "/api/speedtest/run"): self.run_now}


MODULE = Speedtest
