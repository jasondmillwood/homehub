"""Demo mode: simulated data for every module, so the dashboard can be tried (and tested) with no hardware.
All names, addresses and serials here are fictional."""
import json, math, os, tempfile, time

from . import config as C

NVIDIA = "NVIDIA GeForce RTX 3060, 97, 66, 148.2, 1830, 12288"
FAH = {"info": {"version": "8.4.9"}, "config": {"user": "demo-folder", "team": 0},
       "groups": {"": {"config": {"paused": False, "cpus": 4}}},
       "units": [{"state": "RUN", "paused": False, "wu_progress": 0.62, "ppd": 1250000, "eta": "1h 12m",
                  "assignment": {"project": 18201}}]}


def config(port=None):
    root = tempfile.mkdtemp(prefix="homehub-demo-")
    cfg = C._defaults()
    cfg["site"].update(title="Home", subtitle="HUB · DEMO", port=8090 if port is None else port, bind="127.0.0.1")
    cfg["paths"] = {"db": f"{root}/hub.db", "state_dir": f"{root}/state", "status_dir": f"{root}/status"}
    cfg["secrets"]["hub_token_file"] = f"{root}/hub.token"
    m = cfg["modules"]
    m["starlink"].update(enabled=True, monitor_url="http://demo.invalid:8787")
    m["speedtest"].update(enabled=True, tool="librespeed")
    m["drives"].update(enabled=True, dump_globs=[f"{root}/smart/*.json"],
                       devices={"DEMO-USB-0003": {"label": "USB backup drive", "usb_sleep_safe": True, "hot_c": 50},
                                "DEMO-HDD-0001": {"label": "Data HDD"}, "DEMO-SSD-0002": {"label": "System SSD"}})
    m["backups"].update(enabled=True, jobs=[
        {"id": "home", "name": "Home → data drive", "status_file": "backup-home.json", "max_age_h": 26},
        {"id": "usb", "name": "USB copy", "status_file": "usb-copy.json", "max_age_h": 26, "low_free_gb": 50},
        {"id": "cloud", "name": "Offsite", "status_file": "offsite.json", "max_age_h": 30}])
    m["machines"].update(enabled=True, hosts=[
        {"name": "desktop", "host": "192.168.1.20", "ports": [22], "push": True},
        {"name": "laptop", "host": "192.168.1.21", "ports": [22]},
        {"name": "printer", "host": "192.168.1.30", "ports": [631]}])
    m["gpu"].update(enabled=True, folding_at_home=True)
    m["alerts"].update(enabled=False)
    m["explain"].update(enabled=False)
    cfg["demo"] = True
    return cfg


def _dish(slot, i, t):
    w = lambda mid, amp, per: mid + amp * math.sin(t / per + i)
    return {"slot": slot, "label": ["Roof dish", "Mini"][i], "freshness": "LIVE", "reachable": True, "state": "CONNECTED",
            "downMbps": w(190 - 100 * i, 40, 23), "upMbps": w(24 - 10 * i, 5, 19), "pingMs": w(31 + 8 * i, 6, 11),
            "dropRate": 0.004 + 0.01 * i, "obstructionPct": 0.4 + i, "softwareVersion": "demo", "hardwareVersion": ["rev4", "mini1"][i],
            "lastLiveAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t)), "uptimeSeconds": 600000 - 200000 * i, "error": None}


def history(slot, t):
    n = 900
    cur = int(t) % 100000 + n
    drop = [1.0 if (k % 600) < 4 and slot == "mini" else 0.0 for k in range(n)]
    lat = [30.0 + (k % 17) for k in range(n)]
    dl = [5e6 + 1e5 * (k % 50) for k in range(n)]
    gps_now_ns = int((int(t // 3600) * 3600 - 315964800 + 18) * 1e9)
    outs = [{"cause": "OBSTRUCTED", "start_timestamp_ns": str(gps_now_ns - 3600 * 10**9), "duration_ns": str(14 * 10**9), "did_switch": True}] \
        if slot == "mini" else []
    return {"current": cur, "pop_ping_drop_rate": drop, "pop_ping_latency_ms": lat, "downlink_throughput_bps": dl,
            "uplink_throughput_bps": [x / 8 for x in dl], "outages": outs}


def monitor(path):
    t = time.time()
    if path == "/api/status":
        return {"mock": True, "dishes": [_dish("roof", 0, t), _dish("mini", 1, t)]}
    if path.startswith("/api/dish/") and path.endswith("/history"):
        slot = path.split("/")[3]
        return {"dish": slot, "fetchedAt": t * 1000, "history": history(slot, t)}
    raise OSError("demo: unknown path " + path)


def speedtest(tool):
    k = int(time.time()) % 7
    if tool == "ookla":
        return json.dumps({"type": "result", "ping": {"latency": 28.5, "jitter": 3.1}, "packetLoss": 0,
                           "download": {"bandwidth": 22_000_000 + k * 10**6, "bytes": 2 * 10**8},
                           "upload": {"bandwidth": 2_500_000, "bytes": 3 * 10**7}, "server": {"name": "Demo", "location": "Nowhere"}})
    return json.dumps([{"server": {"name": "Demo LibreSpeed"}, "ping": 29.0 + k, "jitter": 2.5,
                        "download": 170.0 + 9 * k, "upload": 21.0 + k, "bytes_sent": 3 * 10**7, "bytes_received": 2 * 10**8}])


def smart(serial, model, temp, poh, ssd=False, crc=0, pending=0, usb=False):
    return {"serial_number": serial, "model_name": model, "smart_status": {"passed": True}, "rotation_rate": 0 if ssd else 5400,
            "temperature": {"current": temp}, "power_on_time": {"hours": poh}, "local_time": {"time_t": int(time.time()) - (3 * 3600 if usb else 60)},
            "ata_smart_attributes": {"table": [{"id": 5, "raw": {"value": 0}}, {"id": 197, "raw": {"value": pending}},
                                                {"id": 199, "raw": {"value": crc}}, {"id": 194, "raw": {"value": temp}}]},
            "ata_smart_self_test_log": {"standard": {"table": [{"type": {"string": "Short offline"},
                                                                "status": {"string": "Completed without error"}, "lifetime_hours": poh - 10}]}}}


def _w(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f)


def seed(ctx):
    """Write demo source files, then run each enabled module's collector once."""
    root = os.path.dirname(ctx.db_path)
    os.makedirs(f"{root}/smart", exist_ok=True)
    for name, j in (("ata-DEMO_HDD", smart("DEMO-HDD-0001", "Demo HDD 2TB", 38, 21000, pending=2)),
                    ("ata-DEMO_SSD", smart("DEMO-SSD-0002", "Demo SSD 500GB", 33, 9000, ssd=True)),
                    ("usb-DEMO_USB", smart("DEMO-USB-0003", "Demo USB 2TB", 44, 3100, usb=True))):
        _w(f"{root}/smart/{name}.json", j)
    now = int(time.time())
    st = ctx.status_dir
    _w(f"{st}/backup-home.json", {"start": now - 7 * 3600 - 300, "end": now - 7 * 3600, "ok": True, "rc": 0})
    _w(f"{st}/usb-copy.json", {"start": now - 5 * 3600 - 900, "end": now - 5 * 3600, "ok": True, "rc": 0, "snapshot": "4f1c2a9be0",
                                 "repoBytes": 412e9, "driveFreeBytes": 41e9, "mounted": True, "checkRan": True, "checkOk": True})
    _w(f"{st}/offsite.json", {"start": now - 4 * 3600 - 2000, "end": now - 4 * 3600, "ok": True, "rc": 0, "snapshot": "9a7d00c311",
                                   "repoBytes": 388e9})
    os.makedirs(f"{st}/machines", exist_ok=True)
    _w(f"{st}/machines/desktop.json", {"ts": now - 120, "uptime_s": 86400 * 3, "disk_free_pct": 41})
    m = ctx.modules
    if "drives" in m:
        m["drives"].ingest()
    if "machines" in m:
        m["machines"].check_once()
    if "starlink" in m:
        m["starlink"].poll_once()
    if "speedtest" in m:
        for _ in range(4):
            m["speedtest"].run_once()
