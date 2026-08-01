#!/bin/bash
# Build toolathlon-harbor-base:v3 (mock track) from the repo's single sources.
#
# mocks/ and the SDK shims live at the repo root; this script stages them into
# the build context so there is exactly one copy to edit. Staged copies are
# gitignored.
#
#   tools/build_base_image.sh [--no-cache]
#
# MOCKS lists the mock MCP servers baked into the image. They run under the
# harness venv (/opt/toolathlon/.venv), so a mock may only depend on packages
# that venv already pins (mcp is there). Add a mock here once its task cluster
# is being ported.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CTX="$REPO/base-image"

MOCKS=(
  google-cloud-mock
  poste-mock
  woocommerce-mock
  github-mock
  huggingface-mock
  notion-mock
  google-sheets-mock
  google-drive-mock
  google-maps-mock
  google-calendar-mock
  google-forms-mock
  wandb-mock
  snowflake-mock
)

rm -rf "$CTX/mocks" "$CTX/sdk-shims" "$CTX/mcp-bridge"
mkdir -p "$CTX/mocks" "$CTX/sdk-shims" "$CTX/mcp-bridge"

for m in "${MOCKS[@]}"; do
  cp -R "$REPO/mocks/$m" "$CTX/mocks/$m"
  find "$CTX/mocks/$m" -name '__pycache__' -type d -prune -exec rm -rf {} +
done

cp -R "$REPO/mocks/gcp-sdk-shim" "$CTX/sdk-shims/gcp"
cp -R "$REPO/mocks/wandb-sdk-shim" "$CTX/sdk-shims/wandb"
cp -R "$REPO/mocks/snowflake-sdk-shim" "$CTX/sdk-shims/snowflake"
find "$CTX/sdk-shims" -name '__pycache__' -type d -prune -exec rm -rf {} +
cp "$REPO/mocks/mcp-bridge/mcp_tool.py" "$CTX/mcp-bridge/mcp_tool.py"
cp "$REPO/mocks/mcp-bridge/render_servers.py" "$CTX/mcp-bridge/render_servers.py"

# Shared HTTP facade + host redirection layer (used by every service whose
# clients dial a hardcoded public host).
cp -R "$REPO/mocks/api-facade" "$CTX/mocks/api-facade"
cp -R "$REPO/mocks/netredirect" "$CTX/mocks/netredirect"
find "$CTX/mocks" -name '__pycache__' -type d -prune -exec rm -rf {} +

echo "staged mocks: ${MOCKS[*]}"
docker build "$@" -f "$CTX/Dockerfile.v3" -t toolathlon-harbor-base:v3 "$CTX"
echo "built toolathlon-harbor-base:v3"
