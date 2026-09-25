#!/bin/bash
# Refresh this app's bundled copy of odcs-ui from a release tag.
# vendor/odcs_ui is generated: never edit it by hand, change odcs-ui and re-sync.
#   packaging/sync-odcs-ui.sh v0.2.0
set -euo pipefail
tag="${1:?usage: $0 <odcs-ui tag, e.g. v0.2.0>}"
cd "$(dirname "$0")/.."
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
git -c advice.detachedHead=false clone --quiet --depth 1 --branch "$tag" https://github.com/tneohcl/odcs-ui "$tmp/odcs-ui"
commit="$(git -C "$tmp/odcs-ui" rev-parse HEAD)"
rm -rf vendor/odcs_ui
mkdir -p vendor
cp -r "$tmp/odcs-ui/odcs_ui" vendor/odcs_ui
cp "$tmp/odcs-ui/LICENSE" vendor/odcs_ui/LICENSE
find vendor/odcs_ui -name '__pycache__' -prune -exec rm -rf {} +
printf '%s %s\n' "$tag" "$commit" > vendor/ODCS_UI_VERSION
echo "vendor/odcs_ui synced to $tag ($commit)"
