import json, os, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from homehub import config as C, core  # noqa: E402


def make_cfg(modules=None, **over):
    root = tempfile.mkdtemp(prefix="hh-test-")
    cfg = C._defaults()
    cfg["paths"] = {"db": f"{root}/hub.db", "state_dir": f"{root}/state", "status_dir": f"{root}/status"}
    tok = f"{root}/hub.token"
    open(tok, "w").write("test-token-123")
    cfg["secrets"]["hub_token_file"] = tok
    for m in cfg["modules"].values():
        m["enabled"] = False
    for name, mc in (modules or {}).items():
        cfg["modules"][name].update(mc, enabled=True)
    cfg.update(over)
    cfg = C._expand(cfg)
    assert not C.validate(cfg), C.validate(cfg)
    return cfg, root


def ctx_with(modules=None, **over):
    cfg, root = make_cfg(modules, **over)
    ctx = core.Context(cfg)
    core.load_modules(ctx)
    return ctx, root
