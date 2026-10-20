#!/usr/bin/env python3
"""Generic public scrub: fail if the tree (and optionally git history) contains private network
addresses, tailnet names, credentials or personal e-mail addresses.

  python3 scripts/public-scrub.py [--history] [path]

Allowed on purpose: 127.0.0.1, 0.0.0.0, the dish address 192.168.100.1, the documentation/default
router range 192.168.1.0/24, and the CGNAT network address 100.64.0.0/10 itself.
"""
import ipaddress, os, re, subprocess, sys

SKIP_DIRS = {".git", "node_modules", "dist", "__pycache__"}
SKIP_FILES = {"package-lock.json", "THIRD_PARTY_LICENSES", "screenshot.png"}
IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")
ALLOW_NETS = [ipaddress.ip_network(n) for n in ("127.0.0.0/8", "192.168.1.0/24", "192.168.100.1/32", "0.0.0.0/32")]
CHECKS = [
    ("tailnet name", re.compile(r"\b[\w-]+\.ts\.net\b|\btail[0-9a-f]{5,}\b", re.I)),
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b|\bgithub_pat_\w{40,}")),
    ("xAI/OpenAI-style key", re.compile(r"\b(?:xai|sk)-[A-Za-z0-9_-]{24,}\b")),
    ("AWS key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Slack token", re.compile(r"\bxox[abpr]-[A-Za-z0-9-]{10,}")),
    ("ntfy token", re.compile(r"\btk_[a-z0-9]{29}\b")),
    ("secret assignment", re.compile(r"""(?i)\b(?:password|passwd|secret|api[_-]?key|token)\b["']?\s*[:=]\s*["'][^"'\s<>$]{12,}["']""")),
    ("real dish id", re.compile(r"\but[0-9a-f]{8}-[0-9a-f]{8}-[0-9a-f]{8}\b", re.I)),
    ("MAC address", re.compile(r"\b(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\b", re.I)),
    ("e-mail", re.compile(r"[\w.+-]+@(?!example\.(?:invalid|com|org)\b|users\.noreply\.github\.com\b)[\w-]+\.[\w.-]+")),
]

def ip_hits(line):
    for m in IPV4.finditer(line):
        try: ip = ipaddress.ip_address(m.group(1))
        except ValueError: continue
        if str(ip) == "100.64.0.0" or any(ip in n for n in ALLOW_NETS): continue
        if ip.is_private or ip in ipaddress.ip_network("100.64.0.0/10"):
            yield f"private IP {ip}"

def scan_text(label, text):
    hits = []
    for n, line in enumerate(text.splitlines(), 1):
        found = list(ip_hits(line)) + [name for name, rx in CHECKS if rx.search(line)]
        hits += [f"{label}:{n}: {f}" for f in found]
    return hits

def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    root = args[0] if args else "."
    hits = []
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
        for f in files:
            if f in SKIP_FILES or f.endswith((".png", ".jpg", ".ico")): continue
            p = os.path.join(d, f)
            try: hits += scan_text(p, open(p, encoding="utf-8", errors="ignore").read())
            except OSError: pass
    if "--history" in sys.argv:
        log = subprocess.run(["git", "-C", root, "log", "--all", "-p",
                              "--format=commit %H%nAuthor: %an <%ae>%nCommitter: %cn <%ce>%n%n%B",
                              "--", ".", ":!package-lock.json", ":!THIRD_PARTY_LICENSES", ":!docs/*.png"],
                             capture_output=True, text=True, check=True).stdout
        hits += scan_text("git-history", log)
    for h in hits: print(h)
    print(f"{len(hits)} hit(s)")
    sys.exit(1 if hits else 0)

if __name__ == "__main__":
    main()
