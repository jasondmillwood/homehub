#!/usr/bin/env python3
"""License check: homehub must stay stdlib-only, MIT, and list every external program it calls.

  python3 scripts/license-check.py
"""
import ast, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXTERNAL = ["dish-monitor", "smartmontools", "LibreSpeed", "Ookla", "nvidia-smi", "Folding@home", "ntfy", "xAI", "curl"]
problems = []

lic = open(os.path.join(ROOT, "LICENSE")).read()
if not lic.startswith("MIT License") or "Copyright (c) 2026 jasondmillwood" not in lic:
    problems.append("LICENSE is not the expected MIT text")

std = set(sys.stdlib_module_names)
for d, _, files in os.walk(os.path.join(ROOT, "homehub")):
    for f in files:
        if not f.endswith(".py"):
            continue
        p = os.path.join(d, f)
        for node in ast.walk(ast.parse(open(p).read(), p)):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                    [node.module] if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module else []
            for n in names:
                top = n.split(".")[0]
                if top not in std and top != "homehub":
                    problems.append(f"{os.path.relpath(p, ROOT)}: non-stdlib import '{n}'")

for extra in ("requirements.txt", "pyproject.toml", "setup.py", "package.json"):
    if os.path.exists(os.path.join(ROOT, extra)):
        problems.append(f"{extra} present: add its dependencies' licenses to THIRD_PARTY_LICENSES and update this check")

tpl = open(os.path.join(ROOT, "THIRD_PARTY_LICENSES")).read()
problems += [f"THIRD_PARTY_LICENSES does not mention {e}" for e in EXTERNAL if e not in tpl]

for p in problems:
    print(p)
print("license check:", "FAILED" if problems else "ok (stdlib only, MIT, external programs listed)")
sys.exit(1 if problems else 0)
