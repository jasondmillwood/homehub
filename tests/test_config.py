import json, os, stat, tempfile, unittest

from helpers import C


class ConfigTest(unittest.TestCase):
    def write(self, obj, mode=0o600):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "config.json")
        json.dump(obj, open(p, "w"))
        os.chmod(p, mode)
        return p

    def test_defaults_without_file(self):
        c = C.load("/nonexistent/config.json", env={})
        self.assertEqual(c["site"]["port"], 8090)
        self.assertTrue(C.enabled(c, "starlink") and C.enabled(c, "drives"))
        for m in ("speedtest", "backups", "machines", "gpu", "alerts", "explain"):
            self.assertFalse(C.enabled(c, m), m)

    def test_example_is_valid_and_has_no_values_for_secrets(self):
        c = json.load(open(C.EXAMPLE))
        for k, v in c["secrets"].items():
            self.assertTrue(v == "" or v.startswith("~/") or v.startswith("/"), k)

    def test_merge_and_env(self):
        p = self.write({"site": {"title": "Cabin"}, "modules": {"gpu": {"enabled": True}}})
        c = C.load(p, env={"HOMEHUB_PORT": "9999"})
        self.assertEqual((c["site"]["title"], c["site"]["port"]), ("Cabin", 9999))
        self.assertTrue(C.enabled(c, "gpu"))
        self.assertFalse(c["paths"]["db"].startswith("~"))

    def test_warns_on_world_readable(self):
        msgs = []
        C.load(self.write({}, 0o644), env={}, warn=msgs.append)
        self.assertTrue(any("chmod 600" in m for m in msgs))

    def test_rejects_bad_config(self):
        bad = [
            {"secrets": {"hub_token_file": "abcdef-a-literal-token"}},
            {"modules": {"speedtest": {"enabled": True, "tool": "ookla"}}},          # EULA not accepted
            {"modules": {"alerts": {"enabled": True}}},                              # no topic
            {"modules": {"explain": {"enabled": True}}},                             # no key file
            {"modules": {"nosuch": {"enabled": True}}},
            {"modules": {"backups": {"jobs": [{"id": "a", "status_file": "x"}, {"id": "a", "status_file": "y"}]}}},
        ]
        for b in bad:
            with self.assertRaises(C.ConfigError, msg=b):
                C.load(self.write(b), env={})

    def test_secret_reads_file(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "k")
        open(p, "w").write("  s3cret \n")
        self.assertEqual(C.secret({"secrets": {"x": p}}, "x"), "s3cret")
        self.assertIsNone(C.secret({"secrets": {"x": ""}}, "x"))


if __name__ == "__main__":
    unittest.main()
