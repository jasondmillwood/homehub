import json, threading, time, unittest, urllib.error, urllib.request

from helpers import core, make_cfg
from homehub import demo, server


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cfg = demo.config(port=0)
        cls.ctx = core.Context(cfg)
        cls.srv = server.serve(cls.ctx, start_jobs=False)
        demo.seed(cls.ctx)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        cls.token = open(cfg["secrets"]["hub_token_file"], "w")
        cls.token.write("tok-abc"); cls.token.close()
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def req(self, path, method="GET", data=None, headers=None):
        r = urllib.request.Request(self.base + path, data=data, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers

    def test_dashboard_neutral(self):
        code, body, h = self.req("/")
        self.assertEqual(code, 200)
        self.assertIn("hub_s=", h["Set-Cookie"])
        self.assertIn("HttpOnly", h["Set-Cookie"])
        html = body.decode()
        self.assertNotIn("STAR" + "LINK", html)
        self.assertNotIn("fonts." + "googleapis", html)
        self.assertIn("system-ui", html)

    def test_tiles_all_demo_modules(self):
        code, body, _ = self.req("/api/tiles")
        mods = {t["module"] for t in json.loads(body)["tiles"]}
        self.assertEqual(mods, {"starlink", "speedtest", "drives", "backups", "machines", "gpu"})
        ids = [t["id"] for t in json.loads(body)["tiles"]]
        self.assertIn("starlink-roof", ids)
        self.assertIn("starlink-mini", ids)

    def test_token_protected_posts(self):
        self.assertEqual(self.req("/api/drive-event", "POST", b"host=pc&type=Health&msg=x")[0], 401)
        code, body, _ = self.req("/api/drive-event", "POST", b"host=pc&type=Health&msg=x", {"X-Hub-Token": "tok-abc"})
        self.assertEqual(code, 200)
        eid = json.loads(body)["id"]
        self.assertEqual(self.req(f"/api/drive-event/{eid}/ack", "POST")[0], 403)          # no session, no token
        self.assertEqual(self.req(f"/api/drive-event/{eid}/ack", "POST", b"", {"X-Hub-Token": "tok-abc"})[0], 200)
        self.assertEqual(self.req("/api/backups/push?job=home", "POST", b"{}", {"X-Hub-Token": "nope"})[0], 401)
        self.assertEqual(self.req("/api/backups/push?job=home", "POST", json.dumps({"end": time.time(), "ok": True}).encode(),
                                  {"X-Hub-Token": "tok-abc"})[0], 200)

    def test_session_cookie_needs_same_origin(self):
        _, _, h = self.req("/")
        cookie = h["Set-Cookie"].split(";")[0]
        host = self.base.split("//")[1]
        self.assertEqual(self.req("/api/speedtest/run", "POST", b"", {"Cookie": cookie})[0], 403)
        self.assertEqual(self.req("/api/speedtest/run", "POST", b"", {"Cookie": cookie, "Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.req("/api/speedtest/run", "POST", b"", {"Cookie": cookie, "Origin": f"http://{host}"})[0], 202)

    def test_unknown_and_disabled_routes(self):
        self.assertEqual(self.req("/api/nope")[0], 404)
        self.assertEqual(self.req("/api/explain", "POST", b"", {"X-Hub-Token": "tok-abc"})[0], 404)   # module off


class ForbiddenNetTest(unittest.TestCase):
    def test_outside_allowed_nets(self):
        cfg, _ = make_cfg({"drives": {"dump_globs": []}})
        cfg["network"]["allowed_nets"] = ["192.168.1.0/24"]
        cfg["site"].update(port=0, bind="127.0.0.1")
        ctx = core.Context(cfg)
        s = server.serve(ctx, start_jobs=False)
        threading.Thread(target=s.serve_forever, daemon=True).start()
        try:
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(f"http://127.0.0.1:{s.server_address[1]}/api/tiles", timeout=5)
            self.assertEqual(e.exception.code, 403)
        finally:
            s.shutdown()
            s.server_close()


if __name__ == "__main__":
    unittest.main()
