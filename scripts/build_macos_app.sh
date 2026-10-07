#!/bin/bash
# Builds a standalone PolyCopy server executable for this Mac (no Python needed to run it).
# Output: dist/PolyCopy/PolyCopy  (double-click it, or run it from Terminal)
#         dist/PolyCopy-macos.zip (the same folder, zipped for sharing between your Macs)
set -euo pipefail
cd "$(dirname "$0")/.."
UV="$(command -v uv || echo "$HOME/.local/bin/uv")"
if [ ! -x "$UV" ]; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  UV="$HOME/.local/bin/uv"
fi
"$UV" sync --locked --python 3.12 --extra build
rm -rf build dist
(cd packaging && "$UV" run --locked --python 3.12 --extra build pyinstaller --noconfirm \
    --distpath ../dist --workpath ../build polycopy.spec)
ditto -c -k --keepParent dist/PolyCopy dist/PolyCopy-macos.zip
echo
echo "Built dist/PolyCopy/PolyCopy and dist/PolyCopy-macos.zip"
