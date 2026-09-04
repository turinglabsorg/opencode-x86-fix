#!/bin/sh
# opencode-x86-fix — run opencode on an Intel Mac without AVX2 (pre-Haswell).
#
# The shipped macOS x64 binary embeds a Bun 1.3.14 runtime that requires AVX2,
# so it dies with SIGILL (exit 132) on Ivy Bridge and older. The official
# installer's "baseline" asset is byte-identical to the regular one, so it
# does not help. See README.md for the full root cause.
#
# What this does, all locally with your own downloads:
#   1. fetches a Bun runtime that works without AVX2 (>= 1.4.0)
#   2. downloads the official opencode release archive for your platform
#   3. extracts the embedded JS bundle out of the binary (bunfs_extract.py)
#   4. rewrites the /$bunfs/root/ virtual paths to the install directory
#   5. installs an `opencode` launcher that runs the bundle on that runtime
#
# No opencode code is redistributed by this repo: the release archive is
# downloaded from GitHub at install time, on your machine.
#
# Re-run to update. Env overrides:
#   OPENCODE_FIX_HOME  install root  (default: ~/.opencode-x86)
#   OPENCODE_FIX_BIN   launcher path (default: ~/.opencode/bin/opencode if that
#                      directory exists, else /usr/local/bin/opencode)
#   OPENCODE_VERSION   version to install (default: latest release)
#   BUN_VERSION        Bun runtime version (default: 1.4.1)

set -eu

HOME_DIR="${OPENCODE_FIX_HOME:-$HOME/.opencode-x86}"
APP="$HOME_DIR/app"
RUNTIME="$HOME_DIR/runtime"
BUN_VERSION="${BUN_VERSION:-1.4.1}"
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

if [ -n "${OPENCODE_FIX_BIN:-}" ]; then
    BIN_PATH="$OPENCODE_FIX_BIN"
elif [ -d "$HOME/.opencode/bin" ]; then
    # the official installer put this ahead of /usr/local/bin on PATH, so the
    # launcher has to live here or the crashing binary keeps winning
    BIN_PATH="$HOME/.opencode/bin/opencode"
else
    BIN_PATH="/usr/local/bin/opencode"
fi

for cmd in python3 curl unzip tar; do
    command -v "$cmd" >/dev/null 2>&1 || { echo "error: $cmd is required"; exit 1; }
done

case "$(uname -s)-$(uname -m)" in
    Darwin-x86_64) ;;
    Darwin-arm64)
        echo "error: this fix is for Intel Macs. Apple Silicon runs the official build fine."
        exit 1 ;;
    Linux-x86_64)
        echo "error: on Linux the official opencode-linux-x64-baseline asset is a genuine"
        echo "       baseline build and works without AVX2 — install opencode normally."
        exit 1 ;;
    *) echo "error: unsupported platform $(uname -s)-$(uname -m)"; exit 1 ;;
esac

if ! sysctl -n hw.optional.avx2_0 2>/dev/null | grep -q '^0$'; then
    echo "note: this CPU reports AVX2 support — you do not need this fix."
    echo "      continuing anyway (set OPENCODE_FIX_HOME to keep it isolated)."
fi

VER="${OPENCODE_VERSION:-}"
if [ -z "$VER" ]; then
    VER=$(curl -fsSL https://api.github.com/repos/anomalyco/opencode/releases/latest |
          sed -n 's/.*"tag_name": *"v\([^"]*\)".*/\1/p' | head -1)
    [ -n "$VER" ] || { echo "error: could not resolve the latest opencode version"; exit 1; }
fi

CUR=$("$BIN_PATH" --version 2>/dev/null | tail -1 || true)
if [ "$CUR" = "$VER" ]; then
    echo "already up to date ($VER)"
    exit 0
fi
echo "installing opencode $VER (current: ${CUR:-none})"

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
cd "$WORK"

echo "-> downloading Bun $BUN_VERSION runtime (works without AVX2)"
curl -fsSL -o bun.zip \
    "https://github.com/oven-sh/bun/releases/download/bun-v${BUN_VERSION}/bun-darwin-x64-baseline.zip"
unzip -oq bun.zip
mkdir -p "$RUNTIME"
mv -f bun-darwin-x64-baseline/bun "$RUNTIME/bun"
chmod +x "$RUNTIME/bun"
if ! "$RUNTIME/bun" --version >/dev/null 2>&1; then
    echo "error: the Bun $BUN_VERSION runtime does not run on this CPU."
    echo "       Bun >= 1.4.0 is required (1.3.x macOS builds need AVX2)."
    exit 1
fi
echo "   bun $("$RUNTIME/bun" --version)"

echo "-> downloading opencode $VER from GitHub releases"
curl -fsSL -o opencode.zip \
    "https://github.com/anomalyco/opencode/releases/download/v${VER}/opencode-darwin-x64.zip"
mkdir -p shipped && (cd shipped && unzip -oq ../opencode.zip)
SHIPPED=$(find shipped -name opencode -type f | head -1)
[ -n "$SHIPPED" ] || { echo "error: no opencode binary inside the release archive"; exit 1; }

echo "-> extracting the embedded JS bundle"
EXTRACT_OUT=$(python3 "$SCRIPT_DIR/bunfs_extract.py" "$SHIPPED" "$WORK/app-new" --rewrite-prefix "$APP")
echo "$EXTRACT_OUT"
ENTRY=$(printf '%s\n' "$EXTRACT_OUT" | awk -F': ' '/^entry point:/{print $2}')
[ -n "$ENTRY" ] || { echo "error: could not determine the entry point"; exit 1; }

rm -rf "$HOME_DIR/app.old"
[ -d "$APP" ] && mv "$APP" "$HOME_DIR/app.old"
mkdir -p "$HOME_DIR"
mv "$WORK/app-new" "$APP"

echo "-> installing launcher at $BIN_PATH"
if [ -f "$BIN_PATH" ] && ! head -1 "$BIN_PATH" 2>/dev/null | grep -q '^#!'; then
    mv "$BIN_PATH" "$BIN_PATH.avx2-broken"
    echo "   kept the crashing binary as $(basename "$BIN_PATH").avx2-broken"
fi
LAUNCHER="#!/bin/sh
# installed by opencode-x86-fix — https://github.com/turinglabsorg/opencode-x86-fix
exec \"$RUNTIME/bun\" \"$APP/$ENTRY\" \"\$@\""
if printf '%s\n' "$LAUNCHER" > "$BIN_PATH" 2>/dev/null; then :; else
    echo "   (need sudo for $BIN_PATH)"
    printf '%s\n' "$LAUNCHER" | sudo tee "$BIN_PATH" >/dev/null
fi
chmod +x "$BIN_PATH" 2>/dev/null || sudo chmod +x "$BIN_PATH"

echo
echo "done: $("$BIN_PATH" --version | tail -1)"
echo "the first run is slow (cold transpile of the bundle); later runs are ~2s"
