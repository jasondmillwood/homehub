# Migrating machine pushes to per-host tokens

Nothing changes unless you opt in. With no `token_file` on any host, `POST /api/machines/push?host=<name>` with the hub
token works exactly as before. The only differences are that bodies over 16 KB now get 413 (was 400 over 64 KB), the 24 h
history, and the `stale` flag. In per-host mode the cap is 8 KB.

## Why
With one shared hub token, any machine holding it can write any other machine's report (`?host=` is just a claim).
Per-host tokens tie each report to the machine that owns the token. Leaking one machine's token then exposes only that machine.

## Steps
1. On the hub, make one token per pushing host (files, never values in config):
   `install -m 600 /dev/null ~/.config/homehub/machines-nas.token && openssl rand -hex 32 > ~/.config/homehub/machines-nas.token`
2. In `config.json`, add `"token_file"` to **every** host with `"push": true`:
   `{ "name": "nas", "host": "192.168.1.20", "ports": [22], "push": true, "token_file": "~/.config/homehub/machines-nas.token" }`
   (`~` is expanded; paths must be absolute and different per host; `homehub --check` validates this).
3. Copy each token to its machine only, and point the push script at it. `scripts/push-health.sh` needs no change:
   `HOMEHUB_TOKEN_FILE=/path/to/machines-nas.token`. It still sends `?host=$(hostname -s)`, and that must match the
   config name. Drop `?host=` if they differ.
4. Restart the hub. Pushes with the old hub token now get **403** ("hub token not accepted"). A wrong host's token gets 403,
   and an unknown token gets 401.
5. Keep the hub token for everything else (backups push, drive events). Only machine pushes change.

**Switch all push hosts at once.** In per-host mode a push host without a `token_file` **cannot push at all: there is no
fallback to the hub token** (its pushes get 403). Its report goes stale, `stale: true`, and its tile turns amber
("health report stale"). This is deliberate: it is the visible signal that a host was missed. Fix it by giving that host a `token_file`.

## Rollback
Remove the `token_file` keys and restart. The hub token works again.

## New read
`GET /api/machines?history=<name>` returns `{"host", "window_s": 86400, "reports": [...]}`, oldest first, at most 2000.
Stored in `<status_dir>/machines/<name>.history.jsonl` and pruned on every push.
