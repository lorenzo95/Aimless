#!/usr/bin/env bash
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
OUT="${1:-$ROOT/dist}"
STAGE="$OUT/aimless-dist"

rm -rf "$STAGE"
mkdir -p "$STAGE"

echo "building daemon …"
(cd "$ROOT/daemon" && go build -trimpath -ldflags "-s -w" -o "$STAGE/aimlessd" .)
echo "staging client …"
cp -r "$ROOT/client/aimless" "$STAGE/aimless"
cp "$ROOT/client/pyproject.toml" "$STAGE/pyproject.toml"
find "$STAGE" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true

cat > "$STAGE/install.sh" <<'INSTALLER'
#!/usr/bin/env bash
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"

if ! python3 -m pip --version >/dev/null 2>&1; then
    if command -v pip3 >/dev/null 2>&1 && pip3 --version >/dev/null 2>&1; then
        PIP="pip3"
    else
        echo "bootstrapping pip …"
        if command -v curl >/dev/null 2>&1; then
            curl -sSL https://bootstrap.pypa.io/get-pip.py -o /tmp/aimless-get-pip.py
        else
            wget -qO /tmp/aimless-get-pip.py https://bootstrap.pypa.io/get-pip.py
        fi
        python3 /tmp/aimless-get-pip.py --user --break-system-packages -q 2>/dev/null || python3 /tmp/aimless-get-pip.py --break-system-packages -q
        PIP="python3 -m pip"
    fi
else
    PIP="python3 -m pip"
fi

mkdir -p ~/.local/bin
install -m 755 "$HERE/aimlessd" ~/.local/bin/aimlessd

install_pkg() {
    $PIP install --user --no-deps -q "$1" 2>/dev/null || \
    $PIP install --user --break-system-packages --no-deps -q "$1" 2>/dev/null || \
    $PIP install --no-deps -q "$1"
}

install_pkg "$HERE"
if ! python3 -c "import nacl" 2>/dev/null; then
    echo "installing pynacl …"
    $PIP install --user -q pynacl 2>/dev/null || \
    $PIP install --user --break-system-packages -q pynacl 2>/dev/null || \
    $PIP install -q pynacl
fi

export PATH="$HOME/.local/bin:$PATH"
if python3 -c "import aimless, nacl" 2>/dev/null && command -v aimlessd >/dev/null 2>&1; then
    echo "OK: aimless + aimlessd installed (~/.local/bin)"
else
    echo "WARNING: verification failed — check python3/pip output above" >&2
    exit 1
fi
INSTALLER
chmod +x "$STAGE/install.sh"

cat > "$STAGE/README.txt" <<'README'
aimless — quick install on a new machine

  ./install.sh
  aimlessd &            (joins the public Yggdrasil network)
  aimless init          (or copy your old ~/.local/share/aimless to keep your identity)
  aimless gui

Requires: python3 (3.10+) with internet access for pynacl on first install.
README

tar -C "$OUT" -czf "$OUT/aimless-dist.tar.gz" aimless-dist
rm -rf "$STAGE"

echo "building release artifacts …"
VERSION="$(grep -oP '(?<=__version__ = ")[^"]+' "$ROOT/client/aimless/__init__.py")"
DAEMON_ART="aimlessd-$VERSION-linux-amd64"
PYZ_ART="aimless-$VERSION.pyz"
go build -trimpath -ldflags "-s -w" -o "$OUT/$DAEMON_ART" "$ROOT/daemon" 2>/dev/null || \
  (cd "$ROOT/daemon" && go build -trimpath -ldflags "-s -w" -o "$OUT/$DAEMON_ART" .)
python3 - <<PYEOF
import os, shutil, subprocess, sys, tempfile
root = "$ROOT"
out = "$OUT"
stage = tempfile.mkdtemp(prefix="aimless-pyz-")
shutil.copytree(os.path.join(root, "client", "aimless"), os.path.join(stage, "aimless"))
for junk in ("__pycache__",):
    p = os.path.join(stage, "aimless", junk)
    if os.path.isdir(p):
        shutil.rmtree(p)
main_src = '''import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aimless.cli import main
sys.exit(main())
'''
with open(os.path.join(stage, "__main__.py"), "w") as f:
    f.write(main_src)
subprocess.run([sys.executable, "-m", "zipapp", stage, "-o", os.path.join(out, "$PYZ_ART"),
                "-p", "/usr/bin/env python3"], check=True)
shutil.rmtree(stage)
print("zipapp built")
PYEOF
echo "release artifacts: $OUT/$DAEMON_ART $OUT/$PYZ_ART"
echo "packaged: $OUT/aimless-dist.tar.gz"
