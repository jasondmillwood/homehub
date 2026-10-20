import json, os, socket, time, unittest

from helpers import core, ctx_with
from homehub import demo
from homehub.modules import drives as D, gpu as G, speedtest as S, alerts as A, explain as E


class StarlinkTest(unittest.TestCase):
    def mk(self, dishes):
        ctx, _ = ctx_with({"starlink": {"monitor_url": "http://monitor.invalid:8787"}})
        m = ctx.modules["starlink"]
        def get(path, timeout=6):
            if path == "/api/status":
                return {"dishes": dishes}
            slot = path.split("/")[3]
            return {"fetchedAt": time.time() * 1000, "history": demo.history(slot, time.time())}
        m.get = get
        return ctx, m

    def test_one_dish(self):
        ctx, m = self.mk([demo._dish("solo", 0, time.time())])
        m.poll_once()
        t = core.tiles(ctx)["tiles"]
        self.assertEqual([x["id"] for x in t], ["starlink-solo"])
        self.assertEqual(t[0]["level"], "green")

    def test_two_dishes_with_outage_and_away(self):
        a, b = demo._dish("roof", 0, time.time()), demo._dish("mini", 1, time.time())
        ctx, m = self.mk([a, b])
        m.poll_once()
        m.poll_once()                                   # second poll: outages deduplicated
        self.assertEqual(len(m.outages("mini")), 1)
        b["freshness"] = "STALE"
        m.cache["t"] = 0
        t = {x["id"]: x for x in core.tiles(ctx)["tiles"]}
        self.assertEqual(t["starlink-mini"]["level"], "gray")
        self.assertEqual(t["starlink-roof"]["level"], "green")

    def test_monitor_down_is_red(self):
        ctx, m = self.mk([])
        def boom(path, timeout=6):
            raise OSError("connection refused")
        m.get = boom
        t = core.tiles(ctx)["tiles"]
        self.assertEqual((t[0]["level"], t[0]["status"]), ("red", "monitor unreachable"))


class DrivesTest(unittest.TestCase):
    def setUp(self):
        self.ctx, self.root = ctx_with({"drives": {"dump_globs": []}})
        self.m = self.ctx.modules["drives"]
        os.makedirs(f"{self.root}/smart", exist_ok=True)
        self.m.cfg["dump_globs"] = [f"{self.root}/smart/*.json"]

    def put(self, name, j):
        json.dump(j, open(f"{self.root}/smart/{name}.json", "w"))

    def test_levels(self):
        self.put("ok", demo.smart("S-OK", "Good", 35, 1000))
        self.put("pend", demo.smart("S-PEND", "Pending", 35, 1000, pending=3))
        self.put("hot", demo.smart("S-HOT", "Hot", 51, 1000))
        self.m.ingest()
        lv = {d["serial"]: d["level"] for d in self.m.summary()["drives"]}
        self.assertEqual(lv, {"S-OK": "green", "S-PEND": "amber", "S-HOT": "amber"})

    def test_new_bad_sectors_in_7d_is_red(self):
        j = demo.smart("S-GROW", "Grow", 35, 1000)
        j["local_time"]["time_t"] = int(time.time()) - 3 * 86400
        self.put("a", j)
        self.m.ingest()
        j2 = demo.smart("S-GROW", "Grow", 35, 1072)
        j2["ata_smart_attributes"]["table"][0]["raw"]["value"] = 8
        self.put("a", j2)
        self.m.ingest()
        d = self.m.summary()["drives"][0]
        self.assertEqual(d["level"], "red")
        self.assertIn("reallocated sectors +8 in 7 d", d["reasons"])

    def test_sleep_safe_usb(self):
        """A sleeping dump (no SMART data) never replaces the last good row; USB drives go gray after 26 h, not 2 h."""
        self.m.cfg["devices"] = {"USB-1": {"label": "USB", "usb_sleep_safe": True}}
        j = demo.smart("USB-1", "Usb", 40, 500)
        j["local_time"]["time_t"] = int(time.time()) - 10 * 3600
        self.put("usb", j)
        self.m.ingest()
        self.put("usb", {"serial_number": "USB-1", "smart_status": None, "power_mode": "STANDBY"})
        self.assertFalse(D.usable(json.load(open(f"{self.root}/smart/usb.json"))))
        self.m.ingest()
        d = self.m.summary()["drives"][0]
        self.assertEqual(d["level"], "green")
        self.assertEqual(self.m.summary(now=time.time() + 17 * 3600)["drives"][0]["level"], "gray")

    def test_skipped_files_ignored(self):
        self.put("x.skipped", demo.smart("S-SKIP", "Skip", 35, 1))
        self.m.ingest()
        self.assertEqual(self.m.summary()["drives"], [])


class BackupsTest(unittest.TestCase):
    def setUp(self):
        self.ctx, self.root = ctx_with({"backups": {"jobs": [
            {"id": "a", "name": "A", "status_file": "a.json", "max_age_h": 26, "low_free_gb": 50}]}})
        self.m = self.ctx.modules["backups"]

    def put(self, **st):
        json.dump(st, open(os.path.join(self.ctx.status_dir, "a.json"), "w"))
        return self.m.job(self.m.cfg["jobs"][0])

    def test_levels(self):
        now = int(time.time())
        self.assertEqual(self.m.job(self.m.cfg["jobs"][0])["level"], "gray")
        self.assertEqual(self.put(end=now - 3600, ok=True, rc=0, driveFreeBytes=200e9)["level"], "green")
        self.assertEqual(self.put(end=now - 3600, ok=False, rc=3, step="backup")["level"], "red")
        self.assertEqual(self.put(end=now - 30 * 3600, ok=True)["level"], "amber")
        self.assertEqual(self.put(end=now - 3600, ok=True, mounted=False)["level"], "red")
        self.assertIn("low space", self.put(end=now - 3600, ok=True, driveFreeBytes=10e9)["summary"])

    def test_last_check_remembered(self):
        now = int(time.time())
        self.put(end=now - 7200, ok=True, checkRan=True, checkOk=False)
        r = self.put(end=now - 3600, ok=True)                     # this night: no check ran
        self.assertEqual(r["level"], "amber")
        self.assertIn("integrity check FAILED", r["summary"])


class MachinesTest(unittest.TestCase):
    def test_push_and_levels(self):
        ctx, _ = ctx_with({"machines": {"hosts": [{"name": "a", "host": "192.168.1.20", "ports": [22], "push": True}]}})
        m = ctx.modules["machines"]
        m.state["a"] = {"t": time.time(), "ports": {22: True}}
        self.assertEqual(m.summary()["machines"][0]["level"], "amber")      # health report missing
        os.makedirs(os.path.join(ctx.status_dir, "machines"), exist_ok=True)
        json.dump({"ts": time.time(), "disk_free_pct": 50}, open(m.health_path("a"), "w"))
        self.assertEqual(m.summary()["machines"][0]["level"], "green")
        m.state["a"] = {"t": time.time(), "ports": {22: False}}
        self.assertEqual(m.summary()["machines"][0]["level"], "red")


class GpuTest(unittest.TestCase):
    def test_parsers(self):
        g = G.parse_nvidia(demo.NVIDIA)
        self.assertEqual((g["util"], g["tempC"]), (97.0, 66.0))
        self.assertFalse(G.parse_nvidia("")["available"])
        f = G.parse_fah(demo.FAH)
        self.assertEqual((f["level"], f["status"], f["ppd"]), ("green", "Folding", 1250000))
        self.assertNotIn("passkey", json.dumps(G.parse_fah(dict(demo.FAH, config={"user": "u", "passkey": "SECRET"}))))
        self.assertEqual(G.parse_fah(None)["level"], "red")


class SpeedtestTest(unittest.TestCase):
    def test_parsers_and_run(self):
        o = S.parse_ookla(json.loads(demo.speedtest("ookla")))
        self.assertGreater(o["dl_bps"], 1e8)
        l = S.parse_librespeed(json.loads(demo.speedtest("librespeed")))
        self.assertGreater(l["dl_bps"], 1e8)
        ctx, _ = ctx_with({"speedtest": {"tool": "librespeed", "binary": "/bin/true"}})
        m = ctx.modules["speedtest"]
        row = m.run_once(runner=lambda cmd: demo.speedtest("librespeed"))
        self.assertEqual(row["ok"], 1)
        bad = m.run_once(runner=lambda cmd: "not json")
        self.assertEqual(bad["ok"], 0)
        self.assertEqual(core.tiles(ctx)["tiles"][0]["level"], "amber")

    def test_ookla_needs_acceptance(self):
        ctx, _ = ctx_with({"speedtest": {"tool": "ookla", "ookla_license_accepted": True, "binary": "/bin/true"}})
        m = ctx.modules["speedtest"]
        self.assertIn("--accept-license", m.command())
        m.cfg["ookla_license_accepted"] = False
        with self.assertRaises(RuntimeError):
            m.command()


class AlertsTest(unittest.TestCase):
    def test_transitions_repeat_recovery_quiet(self):
        ctx, root = ctx_with({"backups": {"jobs": [{"id": "a", "name": "A", "status_file": "a.json"}]},
                              "alerts": {"ntfy_topic": "t", "min_level": "red", "repeat_after_min": 60}})
        al, sent = ctx.modules["alerts"], []
        al.sender = lambda *a: sent.append(a) or True
        p = os.path.join(ctx.status_dir, "a.json")
        now = time.time()
        json.dump({"end": now, "ok": False, "rc": 1}, open(p, "w"))
        al.evaluate(now)
        al.evaluate(now + 60)                                    # no duplicate
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][2], "high")
        al.evaluate(now + 3700)                                  # reminder
        self.assertEqual(len(sent), 2)
        json.dump({"end": now, "ok": True, "rc": 0}, open(p, "w"))
        al.evaluate(now + 3800)                                  # recovery
        self.assertEqual(len(sent), 3)
        self.assertIn("OK again", sent[2][0])
        for msg in sent:
            self.assertNotIn(root, json.dumps(msg))
        self.assertTrue(A.in_quiet("22:00-07:00", time.mktime((2026, 1, 1, 23, 0, 0, 0, 0, -1))))
        self.assertFalse(A.in_quiet("22:00-07:00", time.mktime((2026, 1, 1, 12, 0, 0, 0, 0, -1))))


class ExplainTest(unittest.TestCase):
    def test_redaction_cache_and_cap(self):
        kf = os.path.join(os.path.dirname(__file__), "..", "LICENSE")
        ctx, _ = ctx_with({"backups": {"jobs": [{"id": "a", "name": "A", "status_file": "a.json"}]}, "explain": {}},
                          secrets={"hub_token_file": "", "ntfy_token_file": "", "xai_api_key_file": os.path.abspath(kf)})
        json.dump({"end": time.time(), "ok": False, "rc": 2, "msg": f"repo 192.168.1.77 at /srv/backup/repo serial ABC123 on {socket.gethostname()}"},
                  open(os.path.join(ctx.status_dir, "a.json"), "w"))
        ex, seen = ctx.modules["explain"], []
        ex.poster = lambda text: seen.append(text) or "The backup failed; check the repository."
        r = ex.explain()
        self.assertEqual(r["text"], "The backup failed; check the repository.")
        self.assertNotIn("192.168.1.77", seen[0])
        self.assertNotIn("/srv/backup", seen[0])
        self.assertNotIn("ABC123", seen[0])
        self.assertNotIn(socket.gethostname(), seen[0])
        self.assertTrue(ex.explain()["cached"])
        self.assertEqual(len(seen), 1)


class DisabledModulesTest(unittest.TestCase):
    def test_disabled_modules_draw_nothing_and_start_nothing(self):
        ctx, _ = ctx_with({"drives": {"dump_globs": []}})
        self.assertEqual(list(ctx.modules), ["drives"])
        core.start_all(ctx)
        names = [t.name for t in ctx.threads]
        ctx.stop.set()
        self.assertEqual(names, ["job:drives-ingest"])
        self.assertEqual([t["module"] for t in core.tiles(ctx)["tiles"]], ["drives"])

    def test_nothing_enabled(self):
        ctx, _ = ctx_with({})
        core.start_all(ctx)
        self.assertEqual((ctx.threads, core.tiles(ctx)["tiles"]), ([], []))


if __name__ == "__main__":
    unittest.main()
