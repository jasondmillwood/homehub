# Security policy

## Reporting
Please report vulnerabilities privately through GitHub: **Security → Report a vulnerability** on this
repository (https://github.com/jasondmillwood/homehub/security/advisories/new). Don't open a public issue.
We aim to reply within 7 days.

## Design notes
- homehub is meant for a trusted LAN or private VPN. Every request is checked against `network.allowed_nets`
  (default: loopback and private ranges). **Never forward its port to the internet.**
- Read-only GET endpoints need no login. Anything that changes state or spends something (push endpoints,
  acknowledging events, running a speed test, sending a test alert, asking xAI) needs either the hub token
  (`X-Hub-Token`, read from `secrets.hub_token_file`) or the dashboard's session cookie **plus** a same-origin
  request. These are rate-limited.
- Responses carry a strict Content-Security-Policy, `X-Content-Type-Options: nosniff` and `X-Frame-Options: DENY`.
- Secrets live in separate files (mode 600) referenced by path; `config.json` itself is written mode 600 and
  homehub warns if it is readable by others.
- The explain tile (off by default) sends a **redacted** status digest to the xAI API: IP addresses, paths,
  serial numbers and hostnames are replaced before sending. It is rate-limited (5 min, 30 calls/day).
- The installer runs as your user. The only root step (SMART collection) prints the exact commands and asks
  before running them with sudo.
- Supported versions: the latest release on `main`.
