#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KERNEL_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CONTROL_PLANE_ROOT="${KERNEL_ROOT}/control-plane"
VENVDIR="${CONTROL_PLANE_ROOT}/.venv"
PYTHON="${PYTHON:-python3}"

export KERNEL_TEST_DATABASE_URL="${KERNEL_TEST_DATABASE_URL:-postgresql://telemetry:telemetry@localhost:5432/postgres}"

if [[ ! -d "${VENVDIR}" ]]; then
  "${PYTHON}" -m venv "${VENVDIR}"
fi

echo "==> pip install (control-plane deps)"
"${VENVDIR}/bin/pip" install -q -r "${CONTROL_PLANE_ROOT}/requirements.txt"

echo "==> pytest control-plane/tests"
cd "${CONTROL_PLANE_ROOT}"
exec "${VENVDIR}/bin/pytest" tests "$@"
