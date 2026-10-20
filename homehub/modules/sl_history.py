"""Dish history -> per-minute aggregates + dish-reported outages (stdlib only).
Input is the raw get_history message that dish-monitor serves at /api/dish/<slot>/history."""
import math, time

GPS_UNIX_OFFSET_S = 315964800      # 1980-01-06T00:00:00Z
GPS_LEAP_S = 18                    # GPS-UTC offset since 2017; re-check if IERS adds a leap second

def gps_ns_to_unix(ns: int) -> float:
    return ns / 1e9 + GPS_UNIX_OFFSET_S - GPS_LEAP_S

def _f(d, snake, camel=None, default=None):
    if snake in d: return d[snake]
    camel = camel or ''.join(w if i == 0 else w.title() for i, w in enumerate(snake.split('_')))
    return d.get(camel, default)

def _hist(resp):
    """Accept the full Handle response ({'dishGetHistory': {...}}) or the inner message."""
    for k in ('dish_get_history', 'dishGetHistory'):
        if k in resp: return resp[k]
    return resp

def unroll(h, last_counter):
    """Return (samples, current, buffer_len, gap) with samples = [(counter, drop, lat, dl, ul)] oldest-first,
    containing only counters > last_counter that are still in the ring buffer."""
    drop = _f(h, 'pop_ping_drop_rate'); lat = _f(h, 'pop_ping_latency_ms')
    dl = _f(h, 'downlink_throughput_bps'); ul = _f(h, 'uplink_throughput_bps')
    n = len(drop); current = int(_f(h, 'current'))
    if last_counter is None or current < last_counter:     # first poll or dish rebooted (counter reset)
        start = max(0, current - n)
    else:
        start = max(last_counter, current - n)
    gap = last_counter is not None and current >= last_counter and (current - last_counter) > n
    out = []
    for c in range(start, current):
        i = c % n
        l = lat[i] if i < len(lat) else None
        out.append((c, float(drop[i]), (float(l) if l is not None and drop[i] < 1 else None),
                    float(dl[i]) if i < len(dl) else 0.0, float(ul[i]) if i < len(ul) else 0.0))
    return out, current, n, gap

def _pct(vals, p):
    if not vals: return None
    s = sorted(vals); k = (len(s) - 1) * p; f = math.floor(k); c = math.ceil(k)
    return s[f] if f == c else s[f] + (s[c] - s[f]) * (k - f)

def ingest_history(db, dish, resp, poll_utc=None):
    """Store complete minutes + dish outages. Returns dict summary. Partial trailing minute is left
    for the next poll (last_counter is only advanced to the end of the last complete minute)."""
    poll_utc = poll_utc or time.time()
    h = _hist(resp)
    row = db.execute('SELECT last_counter, anchor_utc FROM sl_poll_state WHERE dish=?', (dish,)).fetchone()
    last, anchor = (row if row else (None, None))
    samples, current, n, gap = unroll(h, last)
    # Counter ticks 1/s; newest sample (current-1) is ~poll time. Keep one anchor per dish so a sample
    # always lands in the same minute across polls; re-anchor on first poll, reboot, or >5 s drift.
    fresh = poll_utc - (current - 1)
    if anchor is None or (last is not None and current < last) or abs(anchor - fresh) > 5:
        anchor = fresh
    ts = lambda c: anchor + c
    minutes = {}
    for s in samples:
        minutes.setdefault(int(ts(s[0]) // 60) * 60, []).append(s)
    now_min = int(ts(current - 1) // 60) * 60   # minute containing newest sample is incomplete
    complete = sorted(m for m in minutes if m < now_min)
    advance_to = last if last is not None and last <= current else max(0, current - n)
    for m in complete:
        ss = minutes[m]
        lats = [x[2] for x in ss if x[2] is not None]
        # Never let a partial re-read (e.g. right after a reboot/re-anchor) overwrite a fuller minute.
        db.execute('''INSERT INTO sl_minute VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                      ON CONFLICT(dish, minute_utc) DO UPDATE SET samples=excluded.samples,
                        drop_full_s=excluded.drop_full_s, drop_mean=excluded.drop_mean, lat_p50_ms=excluded.lat_p50_ms,
                        lat_p95_ms=excluded.lat_p95_ms, lat_max_ms=excluded.lat_max_ms, dl_max_bps=excluded.dl_max_bps,
                        ul_max_bps=excluded.ul_max_bps, dl_bytes=excluded.dl_bytes, ul_bytes=excluded.ul_bytes
                      WHERE excluded.samples >= sl_minute.samples''', (
            dish, m, len(ss), sum(1 for x in ss if x[1] >= 1), sum(x[1] for x in ss) / len(ss),
            _pct(lats, .5), _pct(lats, .95), max(lats) if lats else None,
            max(x[3] for x in ss), max(x[4] for x in ss),
            int(sum(x[3] for x in ss) / 8), int(sum(x[4] for x in ss) / 8)))
        advance_to = ss[-1][0] + 1
    new_out = 0
    for o in _f(h, 'outages', default=[]) or []:
        start_ns = int(_f(o, 'start_timestamp_ns'))
        cur = db.execute('INSERT OR IGNORE INTO sl_outage VALUES (?,?,?,?,?,?)', (
            dish, start_ns, gps_ns_to_unix(start_ns), int(_f(o, 'duration_ns')) / 1e9,
            str(_f(o, 'cause', default='UNKNOWN')), int(bool(_f(o, 'did_switch', default=False)))))
        new_out += cur.rowcount
    db.execute('''INSERT INTO sl_poll_state(dish,last_counter,last_poll_utc,buffer_len,anchor_utc,gaps) VALUES (?,?,?,?,?,?)
                  ON CONFLICT(dish) DO UPDATE SET last_counter=excluded.last_counter,
                  last_poll_utc=excluded.last_poll_utc, buffer_len=excluded.buffer_len,
                  anchor_utc=excluded.anchor_utc, gaps=sl_poll_state.gaps+?''',
               (dish, advance_to, int(poll_utc), n, anchor, int(gap), int(gap)))
    db.commit()
    return {'dish': dish, 'new_samples': len(samples), 'minutes_written': len(complete),
            'new_outages': new_out, 'gap': gap, 'last_counter': advance_to}

SCHEMA = """
CREATE TABLE IF NOT EXISTS sl_poll_state (
  dish TEXT PRIMARY KEY, last_counter INTEGER NOT NULL, last_poll_utc INTEGER NOT NULL,
  buffer_len INTEGER, anchor_utc REAL, gaps INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS sl_outage (
  dish TEXT NOT NULL, start_gps_ns INTEGER NOT NULL, start_utc REAL NOT NULL, duration_s REAL NOT NULL,
  cause TEXT NOT NULL, did_switch INTEGER, PRIMARY KEY (dish, start_gps_ns)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS sl_outage_time ON sl_outage(dish, start_utc);
CREATE TABLE IF NOT EXISTS sl_minute (
  dish TEXT NOT NULL, minute_utc INTEGER NOT NULL, samples INTEGER NOT NULL, drop_full_s INTEGER NOT NULL,
  drop_mean REAL NOT NULL, lat_p50_ms REAL, lat_p95_ms REAL, lat_max_ms REAL, dl_max_bps REAL, ul_max_bps REAL,
  dl_bytes INTEGER, ul_bytes INTEGER, PRIMARY KEY (dish, minute_utc)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS sl_sample (
  ts INTEGER NOT NULL, slot TEXT NOT NULL, reachable INTEGER, freshness TEXT, state TEXT,
  down REAL, up REAL, ping REAL, drop_rate REAL, obstruction REAL, error TEXT);
CREATE INDEX IF NOT EXISTS sl_sample_slot_ts ON sl_sample(slot, ts);
"""
