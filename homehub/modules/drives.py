"""Drives/SMART module. Reads `smartctl -x -j` dumps written by scripts/root/smart-dump (and the sleep-safe
scripts/root/smart-dump-usb), plus dumps other machines push to <status_dir>/smart/<host>/. Root is needed only
to *write* the dumps (installer step, shown and confirmed); the hub itself never runs smartctl."""
import glob, hmac, json, os, socket, time

from ..core import Module
from ..util import RANK, worst

SCHEMA = """
CREATE TABLE IF NOT EXISTS drive_smart (
  host TEXT NOT NULL, serial TEXT NOT NULL, ts_utc INTEGER NOT NULL, model TEXT, label TEXT, passed INTEGER,
  temp_c REAL, power_on_h INTEGER, a5_realloc INTEGER, a187_uncorr INTEGER, a188_cmd_timeout INTEGER,
  a197_pending INTEGER, a198_offline INTEGER, a199_crc INTEGER, ata_err_count INTEGER, nvme_used_pct INTEGER,
  nvme_media_err INTEGER, last_test_type TEXT, last_test_status TEXT, last_test_hours INTEGER,
  phy_crc_since_boot INTEGER, short_test_age_h INTEGER, long_test_age_h INTEGER, is_ssd INTEGER, source TEXT,
  PRIMARY KEY (host, serial, ts_utc)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS drive_event (
  id INTEGER PRIMARY KEY, ts_utc INTEGER NOT NULL, host TEXT NOT NULL, device TEXT,
  type TEXT NOT NULL, msg TEXT NOT NULL, ack INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS drive_event_time ON drive_event(ts_utc);
"""
PHY_ERR_IDS = {0x1, 0x2, 0x3, 0x4, 0x5, 0x6, 0x7, 0xb, 0xf, 0x10, 0x12, 0x13}   # CRC / R_ERR, not COMRESET


def _tests(j, poh):
    sl = j.get("ata_smart_self_test_log") or {}
    tab = (sl.get("extended") or {}).get("table") or (sl.get("standard") or {}).get("table") or []
    first = tab[0] if tab else None

    def age(pred):
        for t in tab:
            if pred((t.get("type") or {}).get("string", "")) and poh is not None and t.get("lifetime_hours") is not None:
                return max(0, poh - t["lifetime_hours"])
        return None
    return first, age(lambda s: "Short" in s), age(lambda s: "Extended" in s or "Long" in s)


def from_smartctl(host, j, source, label=None):
    attrs = {a["id"]: a["raw"]["value"] for a in (j.get("ata_smart_attributes") or {}).get("table", [])}
    nv = j.get("nvme_smart_health_information_log") or {}
    temp = (j.get("temperature") or {}).get("current")
    if temp is None and 194 in attrs:
        temp = attrs[194] & 0xFF
    poh = (j.get("power_on_time") or {}).get("hours")
    first, short_age, long_age = _tests(j, poh)
    el = j.get("ata_smart_error_log") or {}
    phy = (j.get("sata_phy_event_counters") or {}).get("table")
    return dict(host=host, serial=j.get("serial_number", "?"),
                ts_utc=int((j.get("local_time") or {}).get("time_t") or time.time()), model=j.get("model_name"),
                label=label, passed=int(bool((j.get("smart_status") or {}).get("passed", False))),
                temp_c=temp or None, power_on_h=poh, a5_realloc=attrs.get(5), a187_uncorr=attrs.get(187),
                a188_cmd_timeout=attrs.get(188), a197_pending=attrs.get(197), a198_offline=attrs.get(198),
                a199_crc=attrs.get(199), ata_err_count=(el.get("extended") or el.get("summary") or {}).get("count"),
                nvme_used_pct=nv.get("percentage_used"), nvme_media_err=nv.get("media_errors"),
                last_test_type=((first or {}).get("type") or {}).get("string"),
                last_test_status=((first or {}).get("status") or {}).get("string"),
                last_test_hours=(first or {}).get("lifetime_hours"),
                phy_crc_since_boot=(sum(c["value"] for c in phy if c["id"] in PHY_ERR_IDS) if phy else None),
                short_test_age_h=short_age, long_test_age_h=long_age,
                is_ssd=int(j.get("rotation_rate") == 0 or bool(nv)), source=source)


def usable(j):
    """A dump taken while the drive slept (smartctl -n standby) has no SMART data: keep the previous row."""
    return isinstance(j, dict) and bool(j.get("smart_status")) and bool(j.get("serial_number")) and \
        bool(j.get("ata_smart_attributes") or j.get("nvme_smart_health_information_log"))


class Drives(Module):
    name, title, order = "drives", "Drives", 20

    def init_db(self, db):
        db.executescript(SCHEMA)

    def start(self):
        self.ctx.every("drives-ingest", 300, self.ingest, first_delay=1)

    def dev(self, serial):
        return (self.cfg.get("devices") or {}).get(serial) or {}

    def sources(self):
        local = socket.gethostname().split(".")[0].lower()
        for g in self.cfg.get("dump_globs") or []:
            for f in sorted(glob.glob(g)):
                if not f.endswith(".skipped.json"):
                    yield local, f, "smart-dump"
        for f in sorted(glob.glob(os.path.join(self.ctx.status_dir, "smart", "*", "*.json"))):
            yield os.path.basename(os.path.dirname(f)), f, "pushed smart-dump"

    def ingest(self):
        n = 0
        with self.ctx.db() as db:
            for host, f, src in self.sources():
                try:
                    with open(f) as fh:
                        j = json.load(fh)
                except (OSError, ValueError):
                    continue
                if not usable(j):
                    continue
                r = from_smartctl(host, j, src, self.dev(j.get("serial_number")).get("label"))
                cols = ",".join(r)
                n += db.execute(f"INSERT OR IGNORE INTO drive_smart({cols}) VALUES ({','.join('?' * len(r))})",
                                tuple(r.values())).rowcount
            db.execute("DELETE FROM drive_smart WHERE ts_utc < ?", (int(time.time()) - 400 * 86400,))
            db.commit()
        return n

    # ---- rules ----
    def _delta(self, db, h, s, col, since, latest):
        if latest is None:
            return None
        r = db.execute(f"SELECT {col} FROM drive_smart WHERE host=? AND serial=? AND ts_utc>=? AND {col} IS NOT NULL "
                       "ORDER BY ts_utc LIMIT 1", (h, s, since)).fetchone()
        return None if r is None else latest - r[0]

    def summary(self, now=None):
        now = int(now or time.time())
        with self.ctx.db() as db:
            latest = db.execute("""SELECT d.* FROM drive_smart d JOIN (SELECT host, serial, MAX(ts_utc) m FROM drive_smart
                GROUP BY host, serial) x ON d.host=x.host AND d.serial=x.serial AND d.ts_utc=x.m ORDER BY d.host, d.serial""").fetchall()
            events = [dict(e) for e in db.execute("SELECT * FROM drive_event ORDER BY ts_utc DESC LIMIT 50")]
            out = []
            for r in map(dict, latest):
                h, s, dv = r["host"], r["serial"], self.dev(r["serial"])
                d = {c + "_7d": self._delta(db, h, s, c, now - 7 * 86400, r[c])
                     for c in ("a5_realloc", "a187_uncorr", "a197_pending", "a198_offline", "a199_crc")}
                crit, warn, info = [], [], []
                if not r["passed"]:
                    crit.append("SMART health FAILED")
                for c, n in (("a5_realloc", "reallocated"), ("a187_uncorr", "uncorrectable"), ("a197_pending", "pending"),
                             ("a198_offline", "offline-uncorrectable")):
                    if (d[c + "_7d"] or 0) > 0:
                        crit.append(f"{n} sectors +{d[c + '_7d']} in 7 d")
                    elif c in ("a5_realloc", "a197_pending") and (r[c] or 0) > 0:
                        warn.append(f"{r[c]} {n} sectors")
                st = (r["last_test_status"] or "").lower()
                if st and ("fail" in st or "error" in st) and "without error" not in st:
                    crit.append("last self-test: " + r["last_test_status"])
                if (d["a199_crc_7d"] or 0) > 0:
                    warn.append(f"cable/link CRC errors +{d['a199_crc_7d']} in 7 d")
                if (r["phy_crc_since_boot"] or 0) > 0:
                    warn.append(f"{r['phy_crc_since_boot']} SATA link errors since boot")
                hot = dv.get("hot_c") or (self.cfg.get("hot_c_ssd", 60) if r["is_ssd"] else self.cfg.get("hot_c", 45))
                if r["temp_c"] is not None and r["temp_c"] >= hot:
                    warn.append(f"hot {r['temp_c']:.0f}°C")
                if r["nvme_used_pct"] is not None and r["nvme_used_pct"] >= 90:
                    warn.append(f"SSD wear {r['nvme_used_pct']}%")
                unack = [e for e in events if not e["ack"] and e["host"] == h and (s in (e["device"] or "") or not e["device"])]
                if unack:
                    warn.append(f"{len(unack)} unacknowledged drive event(s)")
                age = now - r["ts_utc"]
                stale = dv.get("stale_after_s") or (93600 if dv.get("usb_sleep_safe") else self.cfg.get("stale_after_s", 7200))
                level = "red" if crit else "amber" if warn else "green"
                if age > stale:
                    level = "gray"
                    info.insert(0, f"stale: data {age // 3600} h old" if age >= 7200 else f"stale: data {age // 60} min old")
                if dv.get("usb_sleep_safe"):
                    info.append("read once a day while awake (sleep-safe)")
                out.append({"host": h, "serial": s, "name": dv.get("label") or f"{h} {r['model'] or s}", "model": r["model"],
                            "temp_c": r["temp_c"], "passed": bool(r["passed"]), "power_on_h": r["power_on_h"], "age_s": age,
                            "level": level, "reasons": crit + warn + info, "deltas": d})
        return {"generated_utc": now, "level": worst([o["level"] for o in out]), "drives": out, "events": events[:20]}

    def tile(self):
        s = self.summary()
        ok = lambda d: "SMART OK" + (f" · {d['temp_c']:.0f}°C" if d["temp_c"] is not None else "")
        lines = [{"text": f"{d['name']}: " + (d["reasons"][0] if d["level"] != "green" and d["reasons"] else ok(d)),
                  "level": d["level"]} for d in s["drives"]]
        if not lines:
            lines = [{"text": "no SMART data yet (run the drives setup step)", "level": "gray"}]
        bad = sum(1 for d in s["drives"] if RANK[d["level"]] > 0)
        return {"level": s["level"] if s["drives"] else "gray", "status": f"{bad} need attention" if bad else f"{len(s['drives'])} OK",
                "lines": lines, "detail": s}

    # ---- SMART daemon events (POST from scripts/root/smart-event, token required) ----
    def post_event(self, req):
        if not req.token_ok():
            return 401, {"error": "token"}
        f = req.form()
        host = (f.get("host") or "")[:32].strip().lower()
        if not host or not host.replace("-", "").isalnum():
            return 400, {"error": "host"}
        if req.rate_limited(host):
            return 429, {"error": "rate"}
        typ = (f.get("type") or "unknown")[:64]
        with self.ctx.db() as db:
            cur = db.execute("INSERT INTO drive_event(ts_utc,host,device,type,msg,ack) VALUES (?,?,?,?,?,?)",
                             (int(time.time()), host, (f.get("device") or "")[:128], typ, (f.get("msg") or "")[:500],
                              1 if typ == "EmailTest" else 0))
            db.commit()
            return 200, {"ok": True, "id": cur.lastrowid}

    def ack(self, req):
        if not (req.token_ok() or req.session_ok()):
            return 403, {"error": "ack needs the hub token or a dashboard session (reload the page)"}
        eid = req.path.split("/")[3]
        if not eid.isdigit():
            return 404, {"error": "not found"}
        with self.ctx.db() as db:
            n = db.execute("UPDATE drive_event SET ack=1 WHERE id=?", (int(eid),)).rowcount
            db.commit()
        return (200 if n else 404), {"ok": bool(n)}

    def push_dump(self, req):
        """Another machine pushes its smart-dump JSON: POST /api/drives/push?host=<name>, body = dump."""
        if not req.token_ok():
            return 401, {"error": "token"}
        host = req.q("host", "").lower()
        if not host or not host.replace("-", "").isalnum() or len(host) > 32:
            return 400, {"error": "host"}
        try:
            j = json.loads(req.body(2 << 20))
        except ValueError:
            return 400, {"error": "json"}
        if not usable(j):
            return 202, {"ok": True, "stored": False, "reason": "no SMART data (drive asleep?)"}
        name = "".join(c for c in str(j.get("serial_number")) if c.isalnum() or c in "-_")[:64]
        from ..util import write_json_atomic
        write_json_atomic(os.path.join(self.ctx.status_dir, "smart", host, name + ".json"), j)
        return 200, {"ok": True, "stored": True}

    def routes(self):
        return {("GET", "/api/drives"): lambda req: (200, self.summary()),
                ("POST", "/api/drive-event"): self.post_event,
                ("POST", "/api/drive-event/*"): self.ack,
                ("POST", "/api/drives/push"): self.push_dump}


MODULE = Drives
