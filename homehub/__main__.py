"""python3 -m homehub [--config PATH] [--demo] [--check]"""
import argparse, logging, sys

from . import __version__, config as C, core, server


def main(argv=None):
    ap = argparse.ArgumentParser(prog="homehub", description="Modular home monitoring hub")
    ap.add_argument("--config", help="config.json path (default ~/.config/homehub/config.json)")
    ap.add_argument("--demo", action="store_true", help="simulated data for every module (no hardware, no network)")
    ap.add_argument("--check", action="store_true", help="validate the config and exit")
    ap.add_argument("--port", type=int)
    ap.add_argument("--version", action="version", version=__version__)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        if a.demo:
            from . import demo
            cfg = demo.config(a.port)
        else:
            cfg = C.load(a.config)
            if a.port:
                cfg["site"]["port"] = a.port
    except (C.ConfigError, ValueError) as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    on = [m for m in C.MODULE_NAMES if C.enabled(cfg, m)]
    if a.check:
        print("config OK; enabled modules: " + (", ".join(on) or "none"))
        return 0
    ctx = core.Context(cfg)
    s = server.serve(ctx)
    if a.demo:
        from . import demo
        demo.seed(ctx)
    print(f"homehub {__version__} on http://{cfg['site']['bind']}:{cfg['site']['port']} · modules: {', '.join(on) or 'none'}"
          + (" · DEMO" if a.demo else ""), flush=True)
    try:
        s.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
