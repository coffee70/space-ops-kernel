#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
KERNEL_ROOT=$(cd "${SCRIPT_DIR}/.." && pwd)
WORKSPACE_ROOT=$(cd "${KERNEL_ROOT}/.." && pwd)
PLAYWRIGHT_WORKSPACE="${WORKSPACE_ROOT}/space-ops-apps/tools/playwright"
PLAYWRIGHT_IMAGE="${PLAYWRIGHT_IMAGE:-mcr.microsoft.com/playwright:v1.58.2-noble}"
PLAYWRIGHT_BASE_URL="${PLAYWRIGHT_BASE_URL:-http://platform-edge-proxy:8080}"
PLAYWRIGHT_API_URL="${PLAYWRIGHT_API_URL:-http://platform-edge-proxy:8080}"
PLAYWRIGHT_DOCKER_NETWORK="${PLAYWRIGHT_DOCKER_NETWORK:-space-ops-kernel_default}"
TARGET="${1:-test}"

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "error: required command '$1' is not installed" >&2
    exit 1
  fi
}

require_command docker

if [[ ! -d "${PLAYWRIGHT_WORKSPACE}" ]]; then
  echo "error: playwright workspace not found at ${PLAYWRIGHT_WORKSPACE}" >&2
  exit 1
fi

case "${TARGET}" in
  test)
    runner_cmd=(npm test)
    ;;
  e2e)
    runner_cmd=(npm run test:e2e)
    ;;
  e2e-shell)
    runner_cmd=(npm run test:e2e:shell)
    ;;
  e2e-ui)
    runner_cmd=(npm run test:e2e:ui)
    ;;
  smoke)
    runner_cmd=(npm run test:smoke)
    ;;
  phase3-no-llm)
    runner_cmd=(npx playwright test tests/ai-engineer-phase3-no-llm.spec.ts)
    ;;
  --help|-h|help)
    cat <<'EOF'
Usage:
  ./scripts/validate-playwright.sh test
  ./scripts/validate-playwright.sh e2e
  ./scripts/validate-playwright.sh e2e-shell
  ./scripts/validate-playwright.sh e2e-ui
  ./scripts/validate-playwright.sh smoke
  ./scripts/validate-playwright.sh phase3-no-llm
  ./scripts/validate-playwright.sh tests/overview-smoke.spec.ts

Environment overrides:
  PLAYWRIGHT_IMAGE
  PLAYWRIGHT_BASE_URL
  PLAYWRIGHT_API_URL
  PLAYWRIGHT_PLATFORM_API_URL  # optional; edge-proxy HTTP test compares against raw platform-api (default http://platform-api:8000)
  PLAYWRIGHT_DOCKER_NETWORK
EOF
    exit 0
    ;;
  *)
    runner_cmd=(npx playwright test "$@")
    ;;
esac

echo "==> Playwright target: ${TARGET}"
echo "==> Playwright image: ${PLAYWRIGHT_IMAGE}"
echo "==> Base URL: ${PLAYWRIGHT_BASE_URL}"
echo "==> API URL: ${PLAYWRIGHT_API_URL}"
echo "==> Docker network: ${PLAYWRIGHT_DOCKER_NETWORK}"

docker run --rm \
  --init \
  --ipc=host \
  --network "${PLAYWRIGHT_DOCKER_NETWORK}" \
  --user "$(id -u):$(id -g)" \
  --volume "${WORKSPACE_ROOT}:/workspace" \
  --workdir /workspace/space-ops-apps/tools/playwright \
  --env HOME=/tmp/playwright-home \
  --env npm_config_cache=/tmp/npm-cache \
  --env PLAYWRIGHT_BASE_URL="${PLAYWRIGHT_BASE_URL}" \
  --env PLAYWRIGHT_API_URL="${PLAYWRIGHT_API_URL}" \
  --env PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
  "${PLAYWRIGHT_IMAGE}" \
  bash -lc 'npm ci && exec "$@"' bash "${runner_cmd[@]}"
