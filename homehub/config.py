"""Config loader: built-in defaults <- config.json <- HOMEHUB_* env. No personal values live in code."""
import copy, json, os

HERE = os.path.dirname(os.path.abspath(__file__))
EXAMPLE = os.path.join(os.path.dirname(HERE), "config.example.json")
MODULE_NAMES = ("starlink", "speedtest", "drives", "backups", "machines", "gpu", "alerts", "explain", "alerts_tab")
TILE_MODULES = ("starlink", "speedtest", "drives", "backups", "machines", "gpu")
LEVELS = {"gray": 0, "green": 0, "amber": 1, "red": 2}


class ConfigError(ValueError):
    pass


def _defaults():
    with open(EXAMPLE) as f:
        d = json.load(f)
    for m in d["modules"].values():           # examples are documentation, not defaults
        m.pop("_comment", None)
    d["modules"]["drives"]["devices"] = {}
    d["modules"]["backups"]["jobs"] = []
    d["modules"]["machines"]["hosts"] = []
    return d


def _merge(a, b):
    for k, v in b.items():
        if k.startswith("_"):
            continue
        if isinstance(v, dict) and isinstance(a.get(k), dict) and k != "devices":
            _merge(a[k], v)
        else:
            a[k] = v
    return a


def _expand(o):
    if isinstance(o, dict):
        return {k: _expand(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_expand(v) for v in o]
    return os.path.expanduser(o) if isinstance(o, str) and o.startswith("~") else o


def default_path():
    return os.environ.get("HOMEHUB_CONFIG") or os.path.join(
        os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "homehub", "config.json")


def load(path=None, env=None, warn=print):
    env = os.environ if env is None else env
    cfg = _defaults()
    path = path or default_path()
    if os.path.exists(path):
        if os.stat(path).st_mode & 0o077:
            warn(f"warning: {path} is readable by other users; run: chmod 600 {path}")
        with open(path) as f:
            _merge(cfg, json.load(f))
    if env.get("HOMEHUB_PORT"):
        cfg["site"]["port"] = int(env["HOMEHUB_PORT"])
    if env.get("HOMEHUB_BIND"):
        cfg["site"]["bind"] = env["HOMEHUB_BIND"]
    if env.get("HOMEHUB_DEMO") == "1":
        cfg["demo"] = True
    cfg = _expand(cfg)
    errs = validate(cfg)
    if errs:
        raise ConfigError("; ".join(errs))
    return cfg


def enabled(cfg, name):
    v = cfg["modules"].get(name, {}).get("enabled")
    if v == "auto":         # on only when some module that makes tiles (its data source) is on
        return any(enabled(cfg, m) for m in TILE_MODULES)
    return bool(v)


def secret(cfg, key):
    """Read a secret from the file named in cfg.secrets[key]; None if unset or missing."""
    return read_secret_file(cfg["secrets"].get(key))


def read_secret_file(p):
    """Contents of a secret file (stripped), None if unset, missing or empty."""
    if not p:
        return None
    try:
        with open(p) as f:
            return f.read().strip() or None
    except OSError:
        return None


def validate(cfg):
    errs = []
    unknown = set(cfg["modules"]) - set(MODULE_NAMES)
    if unknown:
        errs.append(f"unknown module(s): {', '.join(sorted(unknown))}")
    for k, v in cfg["secrets"].items():
        if v and not isinstance(v, str):
            errs.append(f"secrets.{k} must be a file path")
        if isinstance(v, str) and v and not (v.startswith("/") or v.startswith("~")):
            errs.append(f"secrets.{k} must be an absolute file path, not a value")
    m = cfg["modules"]
    if m["speedtest"]["enabled"]:
        if m["speedtest"]["tool"] not in ("ookla", "librespeed"):
            errs.append("speedtest.tool must be ookla or librespeed")
        if m["speedtest"]["tool"] == "ookla" and not m["speedtest"]["ookla_license_accepted"]:
            errs.append("speedtest.tool=ookla needs ookla_license_accepted=true (read https://www.speedtest.net/about/eula)")
    if m["alerts"]["enabled"] and not m["alerts"]["ntfy_topic"]:
        errs.append("alerts.ntfy_topic is empty")
    if m["alerts"]["min_level"] not in ("amber", "red"):
        errs.append("alerts.min_level must be amber or red")
    if m["alerts_tab"]["enabled"] not in (True, False, "auto"):
        errs.append("alerts_tab.enabled must be true, false or \"auto\"")
    if m["alerts_tab"]["min_level"] not in ("amber", "red"):
        errs.append("alerts_tab.min_level must be amber or red")
    if not isinstance(m["alerts_tab"]["labels"], dict):
        errs.append("alerts_tab.labels must map tile ids to names")
    if m["explain"]["enabled"] and not cfg["secrets"].get("xai_api_key_file"):
        errs.append("explain needs secrets.xai_api_key_file")
    ids = [j.get("id") for j in m["backups"]["jobs"]]
    if len(ids) != len(set(ids)) or not all(isinstance(i, str) and i.isidentifier() for i in ids):
        errs.append("backups.jobs[].id must be unique identifiers")
    names = [h.get("name") for h in m["machines"]["hosts"]]
    if len(names) != len(set(names)) or not all(isinstance(n, str) and n.replace("-", "").isalnum() for n in names):
        errs.append("machines.hosts[].name must be unique, letters/digits/-")
    tfs = [h.get("token_file") for h in m["machines"]["hosts"] if h.get("token_file")]
    if not all(isinstance(t, str) and os.path.isabs(t) for t in tfs):
        errs.append("machines.hosts[].token_file must be an absolute file path, not a value")
    if len(tfs) != len(set(tfs)):
        errs.append("machines.hosts[].token_file must be different for every host")
    return errs
