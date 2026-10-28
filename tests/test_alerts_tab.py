import json, unittest, urllib.request

from helpers import C, core, ctx_with, make_cfg
from homehub import demo, server
from homehub.modules import alerts_tab as AT


def tile(id, level, lines=(), detail=None, title=None, status=""):
    return {"id": id, "title": title or id, "level": level, "status": status, "module": "x",
            "lines": [{"text": t, "level": l} for t, l in lines], "detail": detail}


class BuildTest(unittest.TestCase):
    CFG = {"min_level": "amber", "labels": {}, "hide_fields": ["host"]}

    def test_only_bad_tiles_worst_first(self):
        r = AT.build([tile("a", "green"), tile("b", "amber", [("x", "amber")]), tile("c", "red", [("y", "red")]),
                      tile("d", "gray")], self.CFG)
        self.assertEqual([a["id"] for a in r["alerts"]], ["c", "b"])
        self.assertEqual((r["red"], r["amber"]), (1, 1))

    def test_culprit_only(self):
        det = {"drives": [{"name": "Data HDD", "level": "amber", "reasons": ["2 pending sectors"], "host": "box1", "temp_c": 38},
                          {"name": "SSD", "level": "green", "summary": "ok"}]}
        r = AT.build([tile("drives", "amber", [("Data HDD: 2 pending", "amber"), ("SSD ok", "green")], det)], self.CFG)
        a = r["alerts"][0]
        self.assertEqual([l["text"] for l in a["summary"]], ["Data HDD: 2 pending"])
        self.assertEqual(a["lines"], [])          # the row already says it
        self.assertEqual([x["name"] for x in a["rows"]], ["Data HDD"])
        self.assertEqual(a["rows"][0]["why"], "2 pending sectors")
        keys = [k for k, _ in a["rows"][0]["kv"]]
        self.assertNotIn("host", keys)
        self.assertIn("temp_c", keys)

    def test_status_fallback_and_labels(self):
        cfg = dict(self.CFG, labels={"starlink-roof": "Home dish"})
        a = AT.build([tile("starlink-roof", "red", status="monitor unreachable")], cfg)["alerts"][0]
        self.assertEqual(a["title"], "Home dish")
        self.assertEqual(a["lines"], [{"text": "monitor unreachable", "level": "red"}])

    def test_min_level_red(self):
        r = AT.build([tile("b", "amber", [("x", "amber")])], dict(self.CFG, min_level="red"))
        self.assertEqual(r["alerts"], [])


class ConfigTest(unittest.TestCase):
    def test_auto(self):
        cfg, _ = make_cfg()
        cfg["modules"]["alerts_tab"]["enabled"] = "auto"
        self.assertFalse(C.enabled(cfg, "alerts_tab"))       # no data source: stays off
        cfg["modules"]["machines"]["enabled"] = True
        self.assertTrue(C.enabled(cfg, "alerts_tab"))

    def test_example_default_is_auto_and_valid(self):
        self.assertEqual(C._defaults()["modules"]["alerts_tab"]["enabled"], "auto")
        cfg, _ = make_cfg()
        cfg["modules"]["alerts_tab"]["enabled"] = "yes"
        self.assertTrue(any("alerts_tab.enabled" in e for e in C.validate(cfg)))


class DemoEndToEnd(unittest.TestCase):
    def test_api_and_page(self):
        cfg = demo.config(0)
        ctx = core.Context(cfg)
        s = server.serve(ctx, start_jobs=False)
        try:
            demo.seed(ctx)
            import threading
            threading.Thread(target=s.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{s.server_address[1]}"
            with urllib.request.urlopen(base + "/api/alerts") as r:
                j = json.load(r)
            ids = [a["id"] for a in j["alerts"]]
            self.assertEqual(ids[0], "machines")                   # red first
            m = j["alerts"][0]
            self.assertEqual([x["name"] for x in m["rows"]], ["printer"])   # only the culprit host
            with urllib.request.urlopen(base + "/api/modules") as r:
                self.assertIn("alerts_tab", json.load(r)["modules"])
            with urllib.request.urlopen(base + "/") as r:
                page = r.read().decode()
            self.assertIn('id="ab"', page)
            self.assertIn("/api/alerts", page)
        finally:
            s.shutdown()
            s.server_close()
            ctx.stop.set()


if __name__ == "__main__":
    unittest.main()
