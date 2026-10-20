"""Module registry, shared context and the background-job runner."""
import contextlib, importlib, logging, os, sqlite3, threading, time

from . import config as C

log = logging.getLogger("homehub")


class Context:
    def __init__(self, cfg):
        self.cfg = cfg
        self.demo = bool(cfg.get("demo"))
        p = cfg["paths"]
        self.db_path, self.state_dir, self.status_dir = p["db"], p["state_dir"], p["status_dir"]
        for d in (os.path.dirname(self.db_path), self.state_dir, self.status_dir):
            os.makedirs(d, exist_ok=True)
        self.threads = []
        self.stop = threading.Event()
        self.modules = {}

    @contextlib.contextmanager
    def db(self):
        """Connection that commits on success and is always closed."""
        c = sqlite3.connect(self.db_path, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout=5000")
        try:
            with c:
                yield c
        finally:
            c.close()

    def mcfg(self, name):
        return self.cfg["modules"][name]

    def every(self, name, interval_s, fn, first_delay=0):
        """Start a daemon loop. Only enabled modules call this, so disabled modules run nothing."""
        def loop():
            if first_delay:
                self.stop.wait(first_delay)
            while not self.stop.is_set():
                try:
                    fn()
                except Exception as e:  # never kill the loop
                    log.warning("%s: %s", name, e)
                self.stop.wait(max(1, interval_s))
        t = threading.Thread(target=loop, name=f"job:{name}", daemon=True)
        t.start()
        self.threads.append(t)
        return t


class Module:
    name = "?"
    title = "?"
    order = 50

    def __init__(self, ctx):
        self.ctx = ctx
        self.cfg = ctx.mcfg(self.name)

    def init_db(self, db):
        pass

    def start(self):
        """Start background jobs (called only when the module is enabled)."""

    def routes(self):
        """{(method, path): handler(req) -> (status, obj)}; paths may end with '/*' for a prefix."""
        return {}

    def tile(self):
        """{'id', 'title', 'level', 'status', 'lines': [{'text','level'}], 'detail': {...}}"""
        raise NotImplementedError


def load_modules(ctx):
    mods = []
    for name in C.MODULE_NAMES:
        if not C.enabled(ctx.cfg, name):
            continue
        m = importlib.import_module(f"homehub.modules.{name}").MODULE(ctx)
        mods.append(m)
    mods.sort(key=lambda m: m.order)
    with ctx.db() as db:
        for m in mods:
            m.init_db(db)
        db.commit()
    ctx.modules = {m.name: m for m in mods}
    return mods


def start_all(ctx):
    for m in ctx.modules.values():
        m.start()


def tiles(ctx):
    out = []
    for m in ctx.modules.values():
        try:
            t = m.tile()
        except Exception as e:
            t = {"level": "gray", "status": "error", "lines": [{"text": str(e)[:150], "level": "gray"}]}
        if t is None:      # e.g. the alerts module has no tile of its own
            continue
        for x in (t if isinstance(t, list) else [t]):
            x.setdefault("id", m.name)
            x.setdefault("title", m.title)
            x["module"] = m.name
            out.append(x)
    return {"generated": int(time.time()), "tiles": out}
