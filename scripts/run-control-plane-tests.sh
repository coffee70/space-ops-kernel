#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KERNEL_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CONTROL_PLANE_ROOT="${KERNEL_ROOT}/control-plane"
VENVDIR="${CONTROL_PLANE_ROOT}/.venv"
PYTHON="${PYTHON:-python3}"
CONTROL_PLANE_TEST_WORKERS="${CONTROL_PLANE_TEST_WORKERS:-serial}"

export KERNEL_TEST_DATABASE_URL="${KERNEL_TEST_DATABASE_URL:-postgresql://telemetry:telemetry@localhost:5432/postgres}"

if [[ ! -d "${VENVDIR}" ]]; then
  "${PYTHON}" -m venv "${VENVDIR}"
fi

if [[ "${SKIP_PIP_INSTALL:-0}" == "1" ]]; then
  echo "==> skip pip install (SKIP_PIP_INSTALL=1)"
else
  echo "==> pip install (control-plane deps)"
  "${VENVDIR}/bin/pip" install -q -r "${CONTROL_PLANE_ROOT}/requirements.txt"
fi

# Default to the entire suite. If the first arg is tests/...(.py|.py::...), use args as selectors (focused run).
PYTEST_SELECTOR=(tests)
if [[ "$#" -gt 0 ]] && [[ "${1}" == tests/* ]]; then
  PYTEST_SELECTOR=("$@")
elif [[ "$#" -gt 0 ]]; then
  PYTEST_SELECTOR=(tests "$@")
fi

PYTEST_ARGS=("${PYTEST_SELECTOR[@]}")
case "${CONTROL_PLANE_TEST_WORKERS}" in
  ""|"serial"|"1")
    ;;
  "auto")
    PYTEST_ARGS=(-n auto "${PYTEST_ARGS[@]}")
    ;;
  *)
    if [[ "${CONTROL_PLANE_TEST_WORKERS}" =~ ^[0-9]+$ ]] && [[ "${CONTROL_PLANE_TEST_WORKERS}" -gt 1 ]]; then
      PYTEST_ARGS=(-n "${CONTROL_PLANE_TEST_WORKERS}" "${PYTEST_ARGS[@]}")
    else
      echo "error: CONTROL_PLANE_TEST_WORKERS must be auto, serial, 1, or an integer greater than 1" >&2
      exit 1
    fi
    ;;
esac

echo "==> pytest control-plane/tests"
cd "${CONTROL_PLANE_ROOT}"
exec "${VENVDIR}/bin/pytest" "${PYTEST_ARGS[@]}"
