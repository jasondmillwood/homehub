"""HTTP server: dashboard + JSON API for the enabled modules. Python stdlib only."""
import collections, hmac, ipaddress, json, logging, os, secrets, threading, time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import __version__, config as C, core

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
CLIENT_GONE = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)
COOKIE = "hub_s"
log = logging.getLogger("homehub")


class Security:
    def __init__(self, ctx):
        self.ctx = ctx
        self.nets = [ipaddress.ip_network(n) for n in ctx.cfg["network"]["allowed_nets"]]
        kf = os.path.join(ctx.state_dir, "session.key")
        if not os.path.exists(kf):
            fd = os.open(kf, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(secrets.token_hex(32))
        with open(kf) as f:
            self.session = hmac.new(f.read().strip().encode(), b"homehub-dash-v1", "sha256").hexdigest()
        self.rate = collections.defaultdict(collections.deque)
        self.lock = threading.Lock()

    def ip_allowed(self, ip):
        try:
            a = ipaddress.ip_address(ip.split("%")[0].removeprefix("::ffff:"))
        except ValueError:
            return False
        return any(a in n for n in self.nets)

    def token(self):
        return C.secret(self.ctx.cfg, "hub_token_file")

    def rate_limited(self, key, limit=60, window=3600):
        now = time.time()
        with self.lock:
            q = self.rate[key]
            while q and q[0] < now - window:
                q.popleft()
            if len(q) >= limit:
                return True
            q.append(now)
            return False


class Request:
    def __init__(self, h):
        self.h, u = h, urlparse(h.path)
        self.path, self.query = u.path, parse_qs(u.query)
        self._body = None

    def q(self, k, d=None):
        return self.query.get(k, [d])[0]

    def qi(self, k, d, lo, hi):
        try:
            return max(lo, min(hi, int(self.q(k, d))))
        except (TypeError, ValueError):
            return d

    def body(self, limit=8192):
        if self._body is None:
            n = int(self.h.headers.get("Content-Length") or 0)
            if n > limit:
                raise ValueError("body too large")
            self._body = self.h.rfile.read(n).decode("utf-8", "replace") if n else ""
        return self._body

    def form(self):
        return {k: v[0] for k, v in parse_qs(self.body()).items()}

    def token_ok(self):
        tok = self.h.sec.token()
        return bool(tok) and hmac.compare_digest(self.h.headers.get("X-Hub-Token", "").encode(), tok.encode())

    def session_ok(self):
        try:
            v = SimpleCookie(self.h.headers.get("Cookie") or "").get(COOKIE)
        except Exception:
            return False
        o = self.h.headers.get("Origin")
        same = bool(o) and urlparse(o).netloc.lower() == (self.h.headers.get("Host") or "").lower()
        return bool(v) and same and hmac.compare_digest(v.value.encode(), self.h.sec.session.encode())

    def rate_limited(self, key):
        return self.h.sec.rate_limited(key)


def build_routes(ctx):
    r = {("GET", "/api/health"): lambda req: (200, {"ok": True, "version": __version__}),
         ("GET", "/api/modules"): lambda req: (200, {"site": {k: ctx.cfg["site"][k] for k in ("title", "subtitle")},
                                                      "demo": ctx.demo, "modules": list(ctx.modules)}),
         ("GET", "/api/tiles"): lambda req: (200, core.tiles(ctx))}
    for m in ctx.modules.values():
        r.update(m.routes())
    return r


def make_handler(ctx):
    sec, routes = Security(ctx), build_routes(ctx)
    prefix = {k[0] + k[1][:-1]: v for k, v in routes.items() if k[1].endswith("/*")}

    class H(BaseHTTPRequestHandler):
        server_version = "homehub"
        sys_version = ""

        def log_message(self, *a):
            pass

        def send(self, code, body, ctype, headers=()):
            if isinstance(body, str):
                body = body.encode()
            try:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Content-Length", str(len(body)))
                for k, v in headers:
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)
            except CLIENT_GONE:
                self.close_connection = True

        def handle_any(self, method):
            self.sec = sec
            if not sec.ip_allowed(self.client_address[0]):
                return self.send(403, '{"error":"forbidden"}', "application/json")
            req = Request(self)
            try:
                if method == "GET" and req.path in ("/", "/index.html"):
                    cookie = f"{COOKIE}={sec.session}; Path=/; HttpOnly; SameSite=Strict; Max-Age=2592000"
                    with open(os.path.join(STATIC, "dash.html"), "rb") as f:
                        page = f.read()
                    return self.send(200, page, "text/html; charset=utf-8",
                                     (("Set-Cookie", cookie), ("Content-Security-Policy",
                                       "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:"),))
                fn = routes.get((method, req.path))
                if not fn:
                    fn = next((v for k, v in prefix.items() if (method + req.path).startswith(k)), None)
                if not fn:
                    return self.send(404, '{"error":"not found"}', "application/json")
                code, obj = fn(req)
                return self.send(code, json.dumps(obj, default=str), "application/json")
            except CLIENT_GONE:
                self.close_connection = True
            except ValueError as e:
                self.send(400, json.dumps({"error": str(e)[:200]}), "application/json")
            except Exception as e:
                log.exception("request failed")
                self.send(500, json.dumps({"error": str(e)[:200]}), "application/json")

        def do_GET(self):
            self.handle_any("GET")

        def do_POST(self):
            self.handle_any("POST")

    return H


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        import sys
        if isinstance(sys.exc_info()[1], CLIENT_GONE):
            return
        super().handle_error(request, client_address)


def serve(ctx, start_jobs=True):
    core.load_modules(ctx)
    if start_jobs:
        core.start_all(ctx)
    s = Server((ctx.cfg["site"]["bind"], int(ctx.cfg["site"]["port"])), make_handler(ctx))
    return s
