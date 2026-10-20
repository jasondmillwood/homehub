# Contributing

Thanks for helping. Small, focused pull requests are easiest to review.

## Setup
```bash
python3 -m homehub --demo --port 8099     # fictional data, every module on
python3 -m unittest discover -s tests     # unit + server tests
bash tests/test_install.sh                # installer, throwaway HOME, no root
python3 scripts/public-scrub.py --history .
python3 scripts/license-check.py
```

## Rules
- **No personal data.** Never commit your `config.json`, real IPs (use `192.168.1.x`), hostnames, drive
  serials, Wi-Fi or tailnet names, ntfy topics, tokens, or screenshots of a real setup. Use `--demo`.
  CI runs `scripts/public-scrub.py` over the tree and the history.
- **Standard library only.** No pip dependencies; `scripts/license-check.py` fails otherwise.
- **Modules stay optional.** A disabled module must draw no tile, register no route that does work, and start
  no background job (there is a test for this). New modules go in `homehub/modules/` with defaults in
  `config.example.json`, off by default.
- **Secrets are file paths** in `config.json`, never values.
- **No new root steps** without printing the exact commands and asking first (see `install.sh --smart`).
- **Read-only towards devices.** homehub observes; it doesn't change dish, drive or backup settings.
- By contributing you agree your contribution is licensed under the MIT License.
