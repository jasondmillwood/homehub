"""Alerts tab: one list of every tile that is amber or red, worst first. Tapping an alert shows only its
culprit: the tile lines at that level, plus the rows of the tile's detail that are themselves amber/red
(e.g. only the failing drive, only the unreachable host). Read-only; it adds no background job.

Display names come from the tiles (which come from config) and can be overridden per tile id with
modules.alerts_tab.labels, e.g. {"starlink-roof": "Home dish"}."""
from ..core import Module, tiles
from ..util import RANK

BAD = ("amber", "red")
SKIP_KEYS = {"level", "id", "details", "deltas"}


def _rows(detail):
    """Yield (section, row) for every dict row in a detail list that carries a bad level."""
    if not isinstance(detail, dict):
        return
    for sec, v in detail.items():
        if isinstance(v, list):
            for r in v:
                if isinstance(r, dict) and r.get("level") in BAD:
                    yield sec, r


def culprit_row(r, hide):
    name = next((str(r[k]) for k in ("name", "label", "title", "id") if r.get(k) not in (None, "")), "?")
    why = r.get("reasons") or r.get("summary") or r.get("status") or ""
    if isinstance(why, list):
        why = ", ".join(str(x) for x in why)
    kv = [[k, v] for k, v in r.items()
          if k not in SKIP_KEYS and k not in hide and k not in ("name", "label", "title", "reasons", "summary")
          and (v is None or isinstance(v, (str, int, float, bool)))][:8]
    return {"name": name, "level": r["level"], "why": str(why)[:200], "kv": kv}


def build(tile_list, cfg):
    floor = RANK.get(cfg.get("min_level", "amber"), 1)
    labels, hide = cfg.get("labels") or {}, set(cfg.get("hide_fields") or [])
    out = []
    for t in tile_list:
        lvl = t.get("level", "gray")
        if t.get("module") == "alerts_tab" or lvl not in BAD or RANK.get(lvl, 0) < floor:
            continue
        lines = [l for l in t.get("lines") or [] if l.get("level") in BAD]
        rows = [culprit_row(r, hide) for _, r in _rows(t.get("detail"))]
        names = [r["name"] for r in rows]
        lines = [l for l in lines if not any(str(l.get("text", "")).startswith(n) for n in names)]  # row says it already
        if not lines and not rows:
            lines = [{"text": t.get("status") or lvl, "level": lvl}]
        out.append({"id": t["id"], "title": labels.get(t["id"], t.get("title", t["id"])), "level": lvl,
                    "status": t.get("status", ""), "lines": lines, "summary": [l for l in t.get("lines") or [] if l.get("level") in BAD][:3] or lines, "rows": rows[:12]})
    out.sort(key=lambda a: (-RANK.get(a["level"], 0), a["title"].lower()))
    red = sum(a["level"] == "red" for a in out)
    return {"alerts": out, "red": red, "amber": len(out) - red}


class AlertsTab(Module):
    name, title, order = "alerts_tab", "Alerts", 95

    def tile(self):
        return None

    def get(self, req):
        return 200, build(tiles(self.ctx)["tiles"], self.cfg)

    def routes(self):
        return {("GET", "/api/alerts"): self.get}


MODULE = AlertsTab
