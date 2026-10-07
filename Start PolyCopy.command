#!/bin/bash
# PolyCopy launcher for macOS. Double-click this file in Finder to start the server.
# First run: installs "uv" (a small Python manager), which downloads Python 3.12 and
# PolyCopy's dependencies into this folder. Later runs start in a few seconds.
# Pass --demo to run against a simulated market (e.g. ./Start\ PolyCopy.command --demo).

cd "$(dirname "$0")" || exit 1

find_uv() {
  if command -v uv >/dev/null 2>&1; then command -v uv; return; fi
  for candidate in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv" /opt/homebrew/bin/uv /usr/local/bin/uv; do
    if [ -x "$candidate" ]; then echo "$candidate"; return; fi
  done
}

UV="$(find_uv)"
if [ -z "$UV" ]; then
  echo "First launch: installing uv (Python package manager from astral.sh)..."
  curl -LsSf https://astral.sh/uv/install.sh | sh || {
    echo "Could not install uv. Check your internet connection and try again."
    read -r -p "Press Return to close." _
    exit 1
  }
  UV="$(find_uv)"
fi

echo "Preparing PolyCopy (first launch can take a minute)..."
"$UV" sync --locked --python 3.12 --quiet || {
  echo "Dependency installation failed. See the messages above."
  read -r -p "Press Return to close." _
  exit 1
}

"$UV" run --locked --python 3.12 python -m polycopy "$@"
status=$?
if [ $status -ne 0 ]; then
  echo
  echo "PolyCopy exited with an error (code $status). Logs: ~/Library/Application Support/PolyCopy/polycopy.log"
  read -r -p "Press Return to close." _
fi
