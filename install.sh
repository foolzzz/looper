#!/usr/bin/env bash
set -euo pipefail

REPO="foolzzz/looper"
MIN_PYTHON_MAJOR=3
MIN_PYTHON_MINOR=11

red()   { printf '\033[0;31m%s\033[0m\n' "$*"; }
green() { printf '\033[0;32m%s\033[0m\n' "$*"; }
dim()   { printf '\033[0;90m%s\033[0m\n' "$*"; }

info()  { echo "  $*"; }
ok()    { green "  ✓ $*"; }
fail()  { red "  ✗ $*"; exit 1; }

echo ""
echo "  Looper Installer"
echo "  ────────────────"
echo ""

# --- Step 1: Find Python 3.11+ ---------------------------------------------------

find_python() {
  for cmd in python3 python3.14 python3.13 python3.12 python3.11; do
    if command -v "$cmd" >/dev/null 2>&1; then
      local ver
      ver="$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || true)"
      if [ -n "$ver" ]; then
        local major minor
        major="${ver%%.*}"
        minor="${ver#*.}"
        if [ "$major" -ge "$MIN_PYTHON_MAJOR" ] && [ "$minor" -ge "$MIN_PYTHON_MINOR" ]; then
          PYTHON_BIN="$(command -v "$cmd")"
          PYTHON_VER="$ver"
          return 0
        fi
      fi
    fi
  done
  return 1
}

PYTHON_BIN=""
PYTHON_VER=""

if find_python; then
  ok "Python $PYTHON_VER ($PYTHON_BIN)"
else
  info "Python 3.11+ not found."
  if command -v uv >/dev/null 2>&1; then
    info "Installing Python via uv..."
    uv python install 3.11 >/dev/null 2>&1
    if find_python; then
      ok "Python $PYTHON_VER installed via uv"
    else
      fail "Could not install Python 3.11+. Install manually: https://python.org"
    fi
  else
    echo ""
    info "Install uv first (it manages Python for you):"
    echo ""
    dim "  curl -LsSf https://astral.sh/uv/install.sh | sh"
    echo ""
    fail "Then re-run this installer."
  fi
fi

# --- Step 2: Get latest wheel URL from GitHub Releases ----------------------------

info "Fetching latest release..."

# Exit status 3 means the release exists but carries no wheel; any other
# failure means the release could not be read.
WHEEL_STATUS=0
WHEEL_URL="$("$PYTHON_BIN" -c "
import json, sys, urllib.request
url = 'https://api.github.com/repos/$REPO/releases/latest'
req = urllib.request.Request(url, headers={'Accept': 'application/vnd.github+json'})
data = json.loads(urllib.request.urlopen(req, timeout=15).read())
for asset in data.get('assets', []):
    if asset['name'].endswith('.whl'):
        print(asset['browser_download_url'])
        sys.exit(0)
sys.exit(3)
" 2>/dev/null)" || WHEEL_STATUS=$?

if [ "$WHEEL_STATUS" -eq 3 ] || { [ "$WHEEL_STATUS" -eq 0 ] && [ -z "$WHEEL_URL" ]; }; then
  red "  ✗ The latest github.com/$REPO release has no wheel asset, so nothing was installed."
  echo ""
  info "Install from a release tag instead:"
  echo ""
  dim "  git clone https://github.com/$REPO.git && cd ${REPO#*/} && git checkout vX.Y.Z"
  dim "  uv sync"
  echo ""
  info "Or wait for a $REPO release that carries a wheel."
  exit 1
fi
[ "$WHEEL_STATUS" -eq 0 ] || fail "Could not fetch release from github.com/$REPO. Check your network."

RELEASE_VER="$(echo "$WHEEL_URL" | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)"
ok "Latest release: v${RELEASE_VER}"

# --- Step 3: Install via uv / pipx / pip -----------------------------------------

# Before the rename the distribution was loopx. Looper still ships a `loopx`
# alias for one release, so an old loopx installed by the same tool is removed
# first: otherwise the two fight over that command. This removes the package
# only; ~/.loopx is moved to ~/.looper when Looper first starts.
OLD_DIST_NOTE="removed the old loopx package (its data in ~/.loopx is kept; Looper moves it to ~/.looper on first start)"

install_with_uv() {
  if uv tool list 2>/dev/null | grep -qE '^loopx '; then
    uv tool uninstall loopx >/dev/null 2>&1 && ok "$OLD_DIST_NOTE"
  fi
  info "Installing with uv..."
  uv tool install --force --python "$PYTHON_BIN" "looper @ $WHEEL_URL" >/dev/null 2>&1
}

install_with_pipx() {
  if pipx list --short 2>/dev/null | grep -qE '^loopx '; then
    pipx uninstall loopx >/dev/null 2>&1 && ok "$OLD_DIST_NOTE"
  fi
  info "Installing with pipx..."
  pipx install --force --python "$PYTHON_BIN" "$WHEEL_URL" >/dev/null 2>&1
}

install_with_pip() {
  if "$PYTHON_BIN" -m pip show loopx >/dev/null 2>&1; then
    "$PYTHON_BIN" -m pip uninstall --yes --quiet loopx >/dev/null 2>&1 && ok "$OLD_DIST_NOTE"
  fi
  info "Installing with pip..."
  "$PYTHON_BIN" -m pip install --user --quiet --upgrade "$WHEEL_URL" 2>/dev/null
}

if command -v uv >/dev/null 2>&1; then
  install_with_uv
elif command -v pipx >/dev/null 2>&1; then
  install_with_pipx
elif "$PYTHON_BIN" -m pip --version >/dev/null 2>&1; then
  install_with_pip
else
  fail "No package installer found. Install uv: curl -LsSf https://astral.sh/uv/install.sh | sh"
fi

# --- Step 4: Verify & PATH -------------------------------------------------------

LOOPER_BIN=""
for p in "$HOME/.local/bin/looper" "$(command -v looper 2>/dev/null || true)"; do
  if [ -n "$p" ] && [ -x "$p" ]; then
    LOOPER_BIN="$p"
    break
  fi
done

if [ -z "$LOOPER_BIN" ]; then
  echo ""
  info "Add ~/.local/bin to your PATH:"
  echo ""
  dim "  export PATH=\"\$HOME/.local/bin:\$PATH\""
  echo ""
  info "Then re-open your terminal or source your profile."
  echo ""
  ok "Looper $RELEASE_VER installed (not yet on PATH)"
else
  INSTALLED_VER="$("$LOOPER_BIN" --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' || echo "$RELEASE_VER")"
  ok "Looper $INSTALLED_VER installed ($LOOPER_BIN)"
fi

echo ""
green "  Next: run 'looper serve' to start."
echo ""
