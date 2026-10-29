import socketserver, threading, time, unittest

from helpers import core, ctx_with
from homehub.modules import ups as U

MODE = {"m": "ol"}
DATA = {"ol": ("OL", 100, 20), "ob": ("OB DISCHRG", 70, 30), "lb": ("OB LB DISCHRG", 12, 30), "chrg": ("OL CHRG", 80, 20)}


class Fake(socketserver.StreamRequestHandler):
    def handle(self):
        for raw in self.rfile:
            c = raw.decode().split(); w = lambda s: self.wfile.write((s + "\n").encode())
            if not c or c[0] == "LOGOUT":
                return
            m = MODE["m"]
            if m == "stale":
                w("ERR DATA-STALE"); continue
            st, ch, ld = DATA[m]; V = {"ups.status": st, "battery.charge": str(ch), "ups.load": str(ld), "input.voltage": "0"}
            if c[:2] == ["LIST", "VAR"]:
                if m == "nolist" or c[2] != "myups":
                    w("ERR ACCESS-DENIED" if m == "nolist" else "ERR UNKNOWN-UPS"); continue
                w(f"BEGIN LIST VAR {c[2]}"); [w(f'VAR {c[2]} {k} "{v}"') for k, v in V.items()]; w(f"END LIST VAR {c[2]}")
            elif c[:2] == ["GET", "VAR"]:
                w(f'VAR {c[2]} {c[3]} "{V[c[3]]}"' if c[3] in V else "ERR VAR-NOT-SUPPORTED")


DATA["nolist"] = DATA["ol"]


class UpsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        socketserver.ThreadingTCPServer.allow_reuse_address = True
        cls.srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Fake); cls.srv.daemon_threads = True
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.port = cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown(); cls.srv.server_close()

    def mk(self, **over):
        cfg = {"host": "127.0.0.1", "port": self.port, "name": "myups", "timeout_s": 1}
        cfg.update(over)
        ctx, _ = ctx_with({"ups": cfg})
        return ctx, ctx.modules["ups"]

    def poll(self, m, mode, now=None):
        MODE["m"] = mode; m.poll_once(now); return m.api(now)

    def test_off_by_default(self):
        ctx, _ = ctx_with({})
        self.assertNotIn("ups", ctx.modules)

    def test_states_and_levels(self):
        ctx, m = self.mk()
        self.assertEqual(self.poll(m, "ol")["level"], "green")
        self.assertEqual(self.poll(m, "ob")["level"], "amber")
        a = self.poll(m, "lb"); self.assertEqual((a["level"], a["charge"]), ("red", 12))
        self.assertEqual(self.poll(m, "stale")["state"], "stale")
        self.assertEqual(self.poll(m, "nolist")["flags"], ["OL"])          # GET VAR fallback
        t = core.tiles(ctx)["tiles"][0]; self.assertEqual((t["id"], t["status"]), ("ups", "online"))

    def test_no_estimates_unless_configured(self):
        _, m = self.mk(); a = self.poll(m, "ol")
        self.assertIsNone(a["watts_est"]); self.assertIsNone(a["runtime_est_min"])

    def test_estimates(self):
        _, m = self.mk(watts_per_load_pct=9, runtime_table=[[10, 40], [50, 10]]); a = self.poll(m, "ob")
        self.assertEqual(a["watts_est"], 270); self.assertAlmostEqual(a["runtime_est_min"], (40 - 30 * 20 / 40) * 0.7, places=1)

    def test_unreachable_keeps_last(self):
        _, m = self.mk(); self.poll(m, "ol"); m.cfg["port"] = 1
        a = m.api() if not m.poll_once() else None
        self.assertEqual((a["state"], a["charge"]), ("unreachable", 100))

    def test_events(self):
        _, m = self.mk(); t = int(time.time())
        self.poll(m, "ob", t); self.poll(m, "lb", t + 10); self.poll(m, "ol", t + 20)
        e = m.history(t + 20)["events"][0]
        self.assertEqual((e["start"], e["end"], e["low_battery"], e["charge_min"]), (t, t + 20, 1, 12))

    def test_validate(self):
        from homehub import config as C
        cfg = C._defaults(); cfg["modules"]["ups"].update(enabled=True, port="x")
        self.assertTrue(any("ups.port" in e for e in C.validate(cfg)))


if __name__ == "__main__":
    unittest.main()
