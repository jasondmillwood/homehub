"""UPS module: reads a NUT upsd (Network UPS Tools) over its plain TCP protocol (stdlib only), on this host or another one on the LAN.
Off by default. Reads only ups.status (OL / OB / LB / CHRG ...), battery.charge and ups.load, so it works with UPSes that report no
battery.runtime / ups.realpower. Optional ESTIMATES, each only when configured:
  watts   = ups.load % x watts_per_load_pct           (watts_per_load_pct = the UPS's rated watts / 100)
  runtime = runtime_table interpolated at the current load (minutes at 100 % charge) x battery.charge / 100
States: ok | stale (upsd says DATA-STALE / DRIVER-NOT-CONNECTED, or no fresh reading for stale_after_s) | unreachable (connect/read failed;
last values kept). Keeps 24 h of samples and the on-battery events (90 days). Status only; the alerts module can read the tile level."""
import socket, time

from ..core import Module
from ..util import when

KEEP_S = 24 * 3600
EVENT_KEEP_S = 90 * 86400
VARS = ("ups.status", "battery.charge", "ups.load")
SCHEMA = """
CREATE TABLE IF NOT EXISTS ups_samples(ts INTEGER NOT NULL, reachable INTEGER NOT NULL, status TEXT, charge REAL, load REAL);
CREATE INDEX IF NOT EXISTS ups_samples_ts ON ups_samples(ts);
CREATE TABLE IF NOT EXISTS ups_events(id INTEGER PRIMARY KEY, start INTEGER NOT NULL, end INTEGER, low_battery INTEGER NOT NULL DEFAULT 0,
  charge_start REAL, charge_min REAL, load_max REAL);
"""

# ---- NUT network protocol client (RFC 9271): one TCP connection per poll ----
class NutError(Exception): pass

class Nut:
    def __init__(self, host, port, timeout):
        self.s = socket.create_connection((host, port), timeout=timeout); self.s.settimeout(timeout); self.buf = b""
    def close(self):
        try: self.s.sendall(b"LOGOUT\n")
        except OSError: pass
        try: self.s.close()
        except OSError: pass
    def __enter__(self): return self
    def __exit__(self, *a): self.close()
    def line(self):
        while b"\n" not in self.buf:
            if len(self.buf) > 65536: raise NutError("reply too long")
            d = self.s.recv(4096)
            if not d: raise NutError("connection closed by upsd")
            self.buf += d
        l, self.buf = self.buf.split(b"\n", 1)
        return l.decode("utf-8", "replace").rstrip("\r")
    def cmd(self, c): self.s.sendall((c + "\n").encode())
    def list_var(self, ups):
        self.cmd(f"LIST VAR {ups}"); first = self.line()
        if first.startswith("ERR"): raise NutError(first[4:] or "ERR")
        if first != f"BEGIN LIST VAR {ups}": raise NutError("unexpected reply")
        out = {}
        for _ in range(1000):
            l = self.line()
            if l == f"END LIST VAR {ups}": return out
            p = _split(l)
            if len(p) >= 4 and p[0] == "VAR" and p[1] == ups: out[p[2]] = p[3]
        raise NutError("list too long")
    def get_var(self, ups, var):
        self.cmd(f"GET VAR {ups} {var}"); l = self.line()
        if l.startswith("ERR"): raise NutError(l[4:] or "ERR")
        p = _split(l)
        if len(p) >= 4 and p[0] == "VAR" and p[1] == ups and p[2] == var: return p[3]
        raise NutError("unexpected reply")

def _split(l):
    """Split a NUT reply line: words, with "double quoted" values (\\" and \\\\ escapes)."""
    out, cur, q, esc, have = [], "", False, False, False
    for ch in l:
        if esc: cur += ch; esc = False
        elif ch == "\\" and q: esc = True
        elif ch == '"': q = not q; have = True
        elif ch == " " and not q:
            if cur or have: out.append(cur)
            cur, have = "", False
        else: cur += ch
    if cur or have: out.append(cur)
    return out

def read_ups(host, port, ups, timeout):
    """{var: value} for VARS. LIST VAR first; if upsd refuses it, GET VAR one by one. Raises OSError / NutError."""
    with Nut(host, port, timeout) as n:
        try: v = n.list_var(ups)
        except NutError as e:
            if any(k in str(e) for k in ("DATA-STALE", "DRIVER-NOT-CONNECTED", "UNKNOWN-UPS")): raise
            v = {}
            for k in VARS:
                try: v[k] = n.get_var(ups, k)
                except NutError as e2:
                    if "VAR-NOT-SUPPORTED" not in str(e2): raise
    return {k: v[k] for k in VARS if k in v}

# ---- derived values ----
def _num(x):
    try: f = float(x); return f if f == f and abs(f) < 1e6 else None
    except (TypeError, ValueError): return None

def flags(status): return [t for t in str(status or "").upper().split() if t.isalnum()][:12]

def level(fl):
    if "LB" in fl: return "red"
    if "OB" in fl: return "amber"
    if "OL" in fl: return "green"
    return "gray"

def runtime_est(load, charge, table):
    """Minutes, linear interpolation of the table at `load` %, scaled by charge %. None if load/charge unknown."""
    t = table or []
    if load is None or charge is None or not t: return None
    load = min(max(load, t[0][0]), t[-1][0])
    for (a, ma), (b, mb) in zip(t, t[1:]):
        if a <= load <= b: m = ma + (mb - ma) * ((load - a) / (b - a) if b > a else 0); break
    else: m = t[0][1]
    return round(m * min(max(charge, 0), 100) / 100, 1)


FLAG_TEXT = {"OL": "on line", "OB": "on battery", "LB": "low battery", "CHRG": "charging", "DISCHRG": "discharging", "RB": "replace battery",
             "BYPASS": "bypass", "OVER": "overload", "OFF": "off", "TRIM": "trimming", "BOOST": "boosting", "CAL": "calibrating"}


class Ups(Module):
    name, title, order = "ups", "UPS", 20

    def __init__(self, ctx):
        super().__init__(ctx)
        c = self.cfg
        self.title = c.get("label") or "UPS"
        t = []
        for row in c.get("runtime_table") or []:
            try:
                if 0 <= float(row[0]) <= 100 and float(row[1]) >= 0:
                    t.append([float(row[0]), float(row[1])])
            except (TypeError, ValueError, IndexError):
                pass
        self.table = sorted(t)
        w = c.get("watts_per_load_pct")
        self.wpp = float(w) if isinstance(w, (int, float)) and not isinstance(w, bool) and w > 0 else None
        self.st = {"vars": {}, "polled": None, "ok_at": None, "error": None, "reach": None}

    def init_db(self, db):
        db.executescript(SCHEMA)

    def start(self):
        self.ctx.every("ups", max(5, int(self.cfg.get("interval_s", 15))), self.poll_once)

    def read(self):
        if self.ctx.demo:
            return {"ups.status": "OL", "battery.charge": "100", "ups.load": "23"}
        c = self.cfg
        return read_ups(c["host"], int(c["port"]), c["name"], min(30.0, max(0.5, float(c.get("timeout_s", 4)))))

    def poll_once(self, now=None):
        now = int(now or time.time())
        try:
            v = self.read(); ok, err, reach = True, None, "ok"
            if "ups.status" not in v:
                raise NutError("no ups.status")
        except NutError as e:
            v, ok, err = {}, False, "upsd: " + str(e)[:120]
            reach = "stale" if any(k in str(e) for k in ("DATA-STALE", "DRIVER-NOT-CONNECTED")) else "error"
        except (OSError, ValueError) as e:
            v, ok, err, reach = {}, False, f"cannot reach {self.cfg['host']}:{self.cfg['port']} ({type(e).__name__})", "unreachable"
        self.st.update(polled=now, error=err, reach=reach)
        if ok:
            self.st.update(vars=v, ok_at=now)
        fl = flags(v.get("ups.status")) if ok else []
        ch, ld = (_num(v.get("battery.charge")), _num(v.get("ups.load"))) if ok else (None, None)
        with self.ctx.db() as db:
            db.execute("INSERT INTO ups_samples VALUES(?,?,?,?,?)", (now, int(ok), " ".join(fl) if ok else None, ch, ld))
            db.execute("DELETE FROM ups_samples WHERE ts < ?", (now - KEEP_S,))
            db.execute("DELETE FROM ups_events WHERE end IS NOT NULL AND end < ?", (now - EVENT_KEEP_S,))
            if ok:
                o = db.execute("SELECT id FROM ups_events WHERE end IS NULL ORDER BY id DESC LIMIT 1").fetchone()
                if "OB" in fl:
                    if o:
                        db.execute("UPDATE ups_events SET low_battery=MAX(low_battery,?), charge_min=MIN(COALESCE(charge_min,?),?), "
                                   "load_max=MAX(COALESCE(load_max,?),?) WHERE id=?", (int("LB" in fl), ch, ch, ld, ld, o[0]))
                    else:
                        db.execute("INSERT INTO ups_events(start,low_battery,charge_start,charge_min,load_max) VALUES(?,?,?,?,?)",
                                   (now, int("LB" in fl), ch, ch, ld))
                elif o:
                    db.execute("UPDATE ups_events SET end=? WHERE id=?", (now, o[0]))
            db.commit()
        return ok

    def state(self, now=None):
        now = int(now or time.time()); s = self.st
        age = now - s["ok_at"] if s["ok_at"] else None
        stale_after = max(30, int(self.cfg.get("stale_after_s", 90)))
        if s["polled"] is None:
            return "loading", age
        if s["reach"] == "unreachable" or (s["reach"] == "error" and not s["ok_at"]):
            return "unreachable", age
        if s["reach"] in ("stale", "error") or age is None or age > stale_after:
            return "stale", age
        return "ok", age

    def api(self, now=None):
        st, age = self.state(now); v = self.st["vars"]; fl = flags(v.get("ups.status"))
        ch, ld = _num(v.get("battery.charge")), _num(v.get("ups.load"))
        return {"state": st, "status": v.get("ups.status"), "flags": fl, "level": level(fl) if st == "ok" else "gray",
                "charge": ch, "load": ld, "watts_est": round(ld * self.wpp) if ld is not None and self.wpp else None,
                "runtime_est_min": runtime_est(ld, ch, self.table), "age_s": age, "ok_at": self.st["ok_at"], "error": self.st["error"]}

    def history(self, now=None):
        now = int(now or time.time()); b = 300
        with self.ctx.db() as db:
            pts = [{"t": r[0] * b, "charge": r[1], "load": r[2], "on_battery": bool(r[3])} for r in db.execute(
                "SELECT ts/? k, ROUND(AVG(charge),1), ROUND(AVG(load),1), MAX(instr(COALESCE(status,''),'OB')>0) FROM ups_samples "
                "WHERE ts>=? GROUP BY k ORDER BY k", (b, now - KEEP_S))]
            ev = [dict(r) for r in db.execute("SELECT start,end,low_battery,charge_start,charge_min,load_max FROM ups_events ORDER BY start DESC LIMIT 20")]
        return {"bucket_s": b, "points": pts, "events": ev}

    def tile(self):
        a = self.api(); st = a["state"]
        if st == "loading":
            return {"level": "gray", "status": "waiting", "lines": [{"text": f"first poll of {self.cfg['name']}@{self.cfg['host']} pending", "level": "gray"}]}
        pill = {"unreachable": "unreachable", "stale": "stale"}.get(st) or ("LOW BATTERY" if "LB" in a["flags"] else "on battery" if "OB" in a["flags"]
                                                                      else "online" if "OL" in a["flags"] else (a["status"] or "unknown"))
        pct = lambda x: "—" if x is None else f"{x:.0f}%"
        lines = [{"text": f"charge {pct(a['charge'])} · load {pct(a['load'])}" + (" · charging" if "CHRG" in a["flags"] else ""),
                  "level": "red" if a["charge"] is not None and a["charge"] <= 15 else "amber" if a["charge"] is not None and a["charge"] <= 25 else None}]
        est = []
        if a["watts_est"] is not None:
            est.append(f"≈ {a['watts_est']} W est.")
        if a["runtime_est_min"] is not None:
            est.append(f"≈ {a['runtime_est_min']:.0f} min runtime est.")
        if est:
            lines.append({"text": " · ".join(est), "level": None})
        if st != "ok":
            lines.append({"text": (a["error"] or "no fresh reading") + (f" · last reading {when(a['ok_at'])}" if a["ok_at"] else ""), "level": "gray"})
        h = self.history()
        ev = [{"start": when(e["start"]), "duration": f"{((e['end'] or time.time()) - e['start']) / 60:.0f} min" + ("" if e["end"] else " so far"),
               "charge": f"{pct(e['charge_start'])} → {pct(e['charge_min'])}", "low battery": "yes" if e["low_battery"] else "no"} for e in h["events"]]
        kv = [["Status", ", ".join(f"{f} ({FLAG_TEXT.get(f, f)})" for f in a["flags"]) or "—"], ["Charge", pct(a["charge"])], ["Load", pct(a["load"])],
              ["Power (est.)", f"{a['watts_est']} W" if a["watts_est"] is not None else "not configured (watts_per_load_pct)"],
              ["Runtime (est.)", f"{a['runtime_est_min']} min" if a["runtime_est_min"] is not None else "not configured (runtime_table)"],
              ["Source", f"NUT {self.cfg['name']}@{self.cfg['host']}:{self.cfg['port']}"], ["Last reading", when(a["ok_at"]) if a["ok_at"] else "never"]]
        return {"level": a["level"], "status": pill, "lines": lines, "detail": {"kv": kv, "on-battery events": ev}}

    def routes(self):
        return {("GET", "/api/ups"): lambda req: (200, dict(self.api(), **(self.history() if req.q("history") else {})))}


MODULE = Ups
