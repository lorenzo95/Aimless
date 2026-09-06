#!/usr/bin/env python3
"""Release gate: the committed dist artifacts must match the source versions.

Run after `deploy/package.sh` and after committing — verifies that what's in
git (HEAD) is what a fresh wget from raw.githubusercontent.com serves.
"""
import io
import re
import subprocess
import sys
import zipfile
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def fail(msg):
    print(f"FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def head_blob(path):
    out = subprocess.run(["git", "show", f"HEAD:{path}"], cwd=ROOT,
                         capture_output=True)
    if out.returncode != 0:
        fail(f"{path} not committed to HEAD")
    return out.stdout


src = open(os.path.join(ROOT, "client/aimless/__init__.py")).read()
m = re.search(r'__version__ = "([^"]+)"', src)
client_ver = m and m.group(1)
src = open(os.path.join(ROOT, "daemon/api.go")).read()
m = re.search(r'buildVersion = "aimlessd/([^"]+)"', src)
daemon_ver = m and m.group(1)

if not client_ver or not daemon_ver:
    fail("could not parse source versions")

pyz = head_blob("dist/aimless.pyz")
z = zipfile.ZipFile(io.BytesIO(pyz))
init = z.read("aimless/__init__.py").decode()
m = re.search(r'__version__ = "([^"]+)"', init)
pyz_ver = m and m.group(1)
if pyz_ver != client_ver:
    fail(f"dist/aimless.pyz is {pyz_ver}, source is {client_ver} — rerun deploy/package.sh and commit")

bin = head_blob("dist/aimlessd-linux-amd64")
if f"aimlessd/{daemon_ver}".encode() not in bin:
    fail(f"dist/aimlessd-linux-amd64 is not {daemon_ver} — rerun deploy/package.sh and commit")

print(f"OK: dist artifacts match source (client {client_ver}, daemon {daemon_ver})")