#!/bin/bash
# Adds VeloCoder to the desktop menu for this source checkout: installs
# data/io.github.tneohcl.VeloCoder.desktop with Exec pointing at launch.sh,
# and the app icon under its app-ID name. Per-user (~/.local/share); run it
# again after moving the checkout. Removes the old pre-app-ID entry
# (velocoder.desktop) if it points at this checkout, so the menu doesn't
# show VeloCoder twice.
set -euo pipefail
repo="$(cd "$(dirname "$0")/.." && pwd)"
id=io.github.tneohcl.VeloCoder
apps="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
icons="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps"
mkdir -p "$apps" "$icons"
install -m 644 "$repo/src/velocoder/ui/assets/app_icon.svg" "$icons/$id.svg"
sed "s|^Exec=.*|Exec=$repo/launch.sh|" "$repo/data/$id.desktop" > "$apps/$id.desktop"
old="$apps/velocoder.desktop"
if [ -f "$old" ] && grep -q "^Exec=$repo/launch.sh$" "$old"; then
    rm "$old"
    echo "removed the old entry $old"
fi
command -v update-desktop-database >/dev/null && update-desktop-database -q "$apps" || true
echo "installed $apps/$id.desktop (runs $repo/launch.sh)"
