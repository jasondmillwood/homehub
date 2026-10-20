"""Backups module. Each job writes (or pushes) a small status JSON; the hub turns it into a level.

Status contract (all optional except end/ok):
  {"start": unix, "end": unix, "ok": true, "rc": 0, "step": "backup", "msg": "...",
   "snapshot": "abc123..", "repoBytes": 0, "driveFreeBytes": 0, "mounted": true,
   "checkRan": false, "checkOk": true}
scripts/backup-status.sh wraps any command (a snapshot tool, rsync, ...) and writes this file for you.
"""
import json, os, time

from ..core import Module
from ..util import fmt_age, hbytes, read_json, sh, when, worst, write_json_atomic


class Backups(Module):
    name, title, order = "backups", "Backups", 30

    def path(self, job):
        p = job["status_file"]
        return p if os.path.isabs(p) else os.path.join(self.ctx.status_dir, p)

    def lastcheck(self, job, st, ts):
        """Newest run that actually ran an integrity check; persisted so nights without a check keep it."""
        f = os.path.join(self.ctx.state_dir, f"lastcheck-{job['id']}.json")
        prev = read_json(f) or {}
        if st.get("checkRan") and ts and ts >= prev.get("ts", 0):
            prev = {"ts": int(ts), "ok": bool(st.get("checkOk"))}
            try:
                write_json_atomic(f, prev)
            except OSError:
                pass
        return prev or None

    def missed_timer(self, job, last):
        """User timer fired but the job never wrote a status (e.g. a RequiresMountsFor dependency failed)."""
        unit = job.get("systemd_timer")
        if not unit or self.ctx.demo:
            return None
        lt = sh(["systemctl", "--user", "show", unit, "--timestamp=unix", "-p", "LastTriggerUSec", "--value"]).lstrip("@")
        if lt.isdigit() and last and int(lt) > last + 120:
            return int(lt)
        return None

    def job(self, job, now=None):
        now = now or time.time()
        r = {"id": job["id"], "name": job.get("name") or job["id"], "level": "gray", "summary": "no status yet", "last": None}
        st = read_json(self.path(job))
        if not isinstance(st, dict):
            return r
        ts = st.get("end") or st.get("start")
        ts = int(ts) if isinstance(ts, (int, float)) and ts > 0 else None
        chk = self.lastcheck(job, st, ts)
        age = now - ts if ts else None
        failed = st.get("ok") is False or st.get("rc") not in (0, None)
        w = when(ts, now) if ts else "unknown time"
        low = job.get("low_free_gb")
        free = st.get("driveFreeBytes")
        if failed:
            lvl, s = "red", f"FAILED {w}" + (f" at '{st['step']}'" if st.get("step") else "") + \
                (f" (exit {st.get('rc')})" if st.get("rc") is not None else "") + (f": {st['msg']}" if st.get("msg") else "")
        elif st.get("mounted") is False:
            lvl, s = "red", f"backup drive NOT MOUNTED ({w})"
        elif age is None or age > job.get("max_age_h", 26) * 3600:
            lvl, s = "amber", f"stale: last run {w} ({fmt_age(age)})"
        elif chk and not chk["ok"]:
            lvl, s = "amber", f"ok {w}, but last integrity check FAILED ({when(chk['ts'], now)})"
        elif low and isinstance(free, (int, float)) and free < low * 1e9:
            lvl, s = "amber", f"low space: {hbytes(free)} free (ok {w})"
        else:
            lvl, s = "green", f"ok {w}" + (f" · {hbytes(st['repoBytes'])}" if st.get("repoBytes") else "")
        missed = self.missed_timer(job, ts)
        if missed and lvl != "red":
            lvl, s = "red", f"scheduled run at {when(missed, now)} did not run"
        r.update(level=lvl, summary=s, last=ts, ago=fmt_age(age) if ts else None, details={
            "result": "failed" if failed else "ok", "rc": st.get("rc"), "step": st.get("step"),
            "durationS": int(st["end"] - st["start"]) if all(isinstance(st.get(k), (int, float)) for k in ("start", "end")) else None,
            "snapshot": (st.get("snapshot") or "")[:8] or None, "repo": hbytes(st.get("repoBytes")), "free": hbytes(free),
            "mounted": st.get("mounted"), "checkThisRun": ("ok" if st.get("checkOk") else "FAILED") if st.get("checkRan") else "not run",
            "lastCheck": {"ts": chk["ts"], "ok": chk["ok"]} if chk else None, "msg": st.get("msg")})
        return r

    def summary(self):
        jobs = [self.job(j) for j in self.cfg.get("jobs") or []]
        return {"level": worst([j["level"] for j in jobs]), "jobs": jobs}

    def tile(self):
        s = self.summary()
        lines = [{"text": f"{j['name']}: {j['summary']}", "level": j["level"]} for j in s["jobs"]] or \
            [{"text": "no backup jobs configured", "level": "gray"}]
        return {"level": s["level"], "status": {"green": "all OK", "amber": "check", "red": "FAILED", "gray": "no data"}[s["level"]],
                "lines": lines, "detail": s}

    def push(self, req):
        """POST /api/backups/push?job=<id>, body = status JSON (for jobs that run on another machine)."""
        if not req.token_ok():
            return 401, {"error": "token"}
        job = next((j for j in self.cfg.get("jobs") or [] if j["id"] == req.q("job", "")), None)
        if not job:
            return 404, {"error": "unknown job"}
        try:
            st = json.loads(req.body(65536))
        except ValueError:
            return 400, {"error": "json"}
        if not isinstance(st, dict):
            return 400, {"error": "json object expected"}
        write_json_atomic(self.path(job), st)
        return 200, {"ok": True}

    def routes(self):
        return {("GET", "/api/backups"): lambda req: (200, self.summary()), ("POST", "/api/backups/push"): self.push}


MODULE = Backups
