"""machines push v2: per-host tokens, wrong-host rejection, 413, legacy hub-token path, 24 h history, stale flag (stdlib unittest)."""
import json, os, tempfile, threading, time, unittest, urllib.error, urllib.request

from helpers import core, make_cfg
from homehub import server


def tokfile(root, name, val):
    p = os.path.join(root, f"{name}.token")
    with open(p, "w") as f:
        f.write(val + "\n")
    return p


class Hub:
    def __init__(self, hosts_fn):
        root = tempfile.mkdtemp(prefix="hh-mpv2-")
        cfg, self.root = make_cfg({"machines": {"hosts": hosts_fn(root)}})
        self.ctx = core.Context(cfg)
        self.srv = server.serve(self.ctx, start_jobs=False)     # loads the modules
        self.m = self.ctx.modules["machines"]
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown(); self.srv.server_close()

    def req(self, path, method="GET", data=None, tok=None):
        h = {"X-Hub-Token": tok} if tok else {}
        r = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")

    def push(self, q="", body=None, tok=None):
        return self.req("/api/machines/push" + q, "POST", json.dumps(body or {"disk_free_pct": 50}).encode(), tok)


class Legacy(unittest.TestCase):
    """No token_file anywhere: exactly the v0.1 behaviour (hub token + ?host=)."""
    @classmethod
    def setUpClass(cls):
        cls.h = Hub(lambda r: [{"name": "a", "host": "127.0.0.1", "ports": [1], "push": True},
                               {"name": "b", "host": "127.0.0.1", "ports": [1], "push": True}])

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def test_hub_token_and_host_param(self):
        self.assertEqual(self.h.push("?host=a", tok="test-token-123"), (200, {"ok": True, "host": "a"}))
        self.assertEqual(self.h.push("?host=b", tok="test-token-123")[0], 200)     # legacy: one token, any push host

    def test_bad_token_and_unknown_host(self):
        self.assertEqual(self.h.push("?host=a", tok="nope")[0], 401)
        self.assertEqual(self.h.push("?host=a")[0], 401)
        self.assertEqual(self.h.push("?host=zzz", tok="test-token-123")[0], 404)

    def test_413(self):
        self.assertEqual(self.h.push("?host=a", {"msg": "x" * 17000}, tok="test-token-123")[0], 413)   # legacy cap 16 KB
        self.assertEqual(self.h.push("?host=a", {"msg": "x" * 12000}, tok="test-token-123")[0], 200)


class PerHost(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = Hub(lambda r: [{"name": "a", "host": "127.0.0.1", "ports": [1], "push": True, "token_file": tokfile(r, "a", "tok-a")},
                               {"name": "b", "host": "127.0.0.1", "ports": [1], "push": True, "token_file": tokfile(r, "b", "tok-b")},
                               {"name": "c", "host": "127.0.0.1", "ports": [1], "push": True}])

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def test_host_from_token(self):
        self.assertEqual(self.h.push(tok="tok-a"), (200, {"ok": True, "host": "a"}))        # no ?host= needed
        self.assertEqual(self.h.push("?host=b", tok="tok-b"), (200, {"ok": True, "host": "b"}))
        self.assertTrue(os.path.exists(self.h.m.health_path("a")))

    def test_wrong_host_token_rejected(self):
        c, b = self.h.push("?host=b", tok="tok-a")
        self.assertEqual(c, 403); self.assertIn("does not belong", b["error"])
        before = open(self.h.m.health_path("b")).read() if os.path.exists(self.h.m.health_path("b")) else None
        self.h.push("?host=b", {"disk_free_pct": 1}, tok="tok-a")
        after = open(self.h.m.health_path("b")).read() if os.path.exists(self.h.m.health_path("b")) else None
        self.assertEqual(before, after)                                                    # b's report untouched

    def test_hub_token_refused_in_per_host_mode(self):
        self.assertEqual(self.h.push("?host=c", tok="test-token-123")[0], 403)
        self.assertEqual(self.h.push("?host=a", tok="test-token-123")[0], 403)
        self.assertEqual(self.h.push(tok="nope")[0], 401)
        self.assertEqual(self.h.push()[0], 401)

    def test_413_before_body(self):
        self.assertEqual(self.h.push(body={"msg": "x" * 9000}, tok="tok-a")[0], 413)                   # per-host cap 8 KB
        self.assertEqual(self.h.push(body={"msg": "x" * 7000}, tok="tok-a")[0], 200)

    def test_history_and_stale(self):
        for i in range(3):
            self.assertEqual(self.h.push(body={"n": i}, tok="tok-b")[0], 200)
        old = {"ts": int(time.time()) - 25 * 3600, "n": -1}
        with open(self.h.m.history_path("b")) as f:
            cur = f.read()
        with open(self.h.m.history_path("b"), "w") as f:
            f.write(json.dumps(old) + "\n" + cur)
        c, d = self.h.req("/api/machines?history=b")
        self.assertEqual(c, 200)
        self.assertEqual([x["n"] for x in d["reports"]][-3:], [0, 1, 2])
        self.assertNotIn(-1, [x.get("n") for x in d["reports"]])                           # >24 h dropped
        self.assertEqual(self.h.req("/api/machines?history=zzz")[0], 404)
        self.h.push(body={"n": 9}, tok="tok-b")
        self.assertNotIn('"n":-1', open(self.h.m.history_path("b")).read())                # pruned on write
        self.h.m.check_once()
        s = {m["name"]: m for m in self.h.req("/api/machines")[1]["machines"]}
        self.assertFalse(s["b"]["stale"])
        self.assertTrue(s["c"]["stale"])                                                   # never pushed


class Config(unittest.TestCase):
    def test_token_file_must_be_path(self):
        from homehub import config as C
        cfg, _ = make_cfg()
        cfg["modules"]["machines"]["hosts"] = [{"name": "a", "host": "x", "push": True, "token_file": "tok-a-literal"}]
        self.assertTrue(any("token_file" in e for e in C.validate(cfg)))
        cfg["modules"]["machines"]["hosts"] = [{"name": "a", "host": "x", "token_file": "/t"}, {"name": "b", "host": "y", "token_file": "/t"}]
        self.assertTrue(any("different" in e for e in C.validate(cfg)))


if __name__ == "__main__":
    unittest.main()
