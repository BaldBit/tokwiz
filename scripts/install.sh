#!/usr/bin/env bash
# TokWiz Zero-Clone Release Installer (`scripts/install.sh`)
# Usage:
#   curl -fsSL https://github.com/BaldBit/tokwiz/releases/latest/download/install.sh | bash
#   or with a specific version:
#   TOKWIZ_VERSION=v0.2.0 curl -fsSL https://github.com/BaldBit/tokwiz/releases/latest/download/install.sh | bash
set -euo pipefail

REPO="BaldBit/tokwiz"
INSTALL_ROOT="${TOKWIZ_HOME:-$HOME/.tokwiz/runtime}"
BIN_DIR="${TOKWIZ_BIN_DIR:-$HOME/.local/bin}"
VERSION="${TOKWIZ_VERSION:-latest}"

if [[ "$VERSION" == "latest" ]]; then
  BUNDLE_URL="https://github.com/${REPO}/releases/latest/download/tokwiz-bundle.tar.gz"
else
  BUNDLE_URL="https://github.com/${REPO}/releases/download/${VERSION}/tokwiz-bundle.tar.gz"
fi

echo "⚡ Installing TokWiz (${VERSION}) from GitHub Release bundle..."
mkdir -p "$INSTALL_ROOT" "$BIN_DIR"

TMP_TAR="$(mktemp -t tokwiz-bundle.XXXXXX.tar.gz)"
trap 'rm -f "$TMP_TAR"' EXIT

curl -fsSL "$BUNDLE_URL" -o "$TMP_TAR"
rm -rf "${INSTALL_ROOT:?}/tokwiz"
tar -xzf "$TMP_TAR" -C "$INSTALL_ROOT"

cat > "${BIN_DIR}/tokwiz" <<EOF
#!/usr/bin/env bash
exec python3 "${INSTALL_ROOT}/tokwiz/scripts/cli.py" "\$@"
EOF
chmod +x "${BIN_DIR}/tokwiz"

echo "✓ Installed TokWiz runtime to: ${INSTALL_ROOT}/tokwiz"
echo "✓ Installed CLI binary to    : ${BIN_DIR}/tokwiz"

if [[ ":$PATH:" != *":${BIN_DIR}:"* ]]; then
  echo "ℹ️  Add ${BIN_DIR} to your PATH if not already present:"
  echo "   export PATH=\"${BIN_DIR}:\$PATH\""
fi
echo "🚀 Run 'tokwiz init --agent all' inside any project to get started!"
