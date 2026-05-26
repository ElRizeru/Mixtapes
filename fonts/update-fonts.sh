#!/usr/bin/env bash
# Refresh bundled Adwaita Sans + Mono from upstream.
# Run from anywhere; output lands in the same directory as this script.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARBALL_URL="https://gitlab.gnome.org/GNOME/adwaita-fonts/-/archive/main/adwaita-fonts-main.tar.gz"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT

echo "→ Downloading ${TARBALL_URL}"
curl -sSL "${TARBALL_URL}" -o "${TMP_DIR}/adwaita-fonts.tar.gz"

echo "→ Extracting"
tar xzf "${TMP_DIR}/adwaita-fonts.tar.gz" -C "${TMP_DIR}"

SRC="${TMP_DIR}/adwaita-fonts-main"

echo "→ Installing fonts into ${SCRIPT_DIR}"
for f in \
    "sans/AdwaitaSans-Regular.ttf" \
    "sans/AdwaitaSans-Italic.ttf" \
    "mono/AdwaitaMono-Regular.ttf" \
    "mono/AdwaitaMono-Italic.ttf" \
    "mono/AdwaitaMono-Bold.ttf" \
    "mono/AdwaitaMono-BoldItalic.ttf"; do
    cp "${SRC}/${f}" "${SCRIPT_DIR}/$(basename "${f}")"
done

cp "${SRC}/LICENSE" "${SCRIPT_DIR}/LICENSE.adwaita-fonts"

echo "✓ Done. Bundled files:"
ls -lh "${SCRIPT_DIR}"/*.ttf "${SCRIPT_DIR}/LICENSE.adwaita-fonts"
