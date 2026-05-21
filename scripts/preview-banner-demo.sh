#!/usr/bin/env bash

set -euo pipefail

COMMAND="${1:-status}"
KERNEL_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKSPACE_ROOT="$(cd "${KERNEL_ROOT}/.." && pwd)"
APPS_ROOT="${APPS_ROOT:-${WORKSPACE_ROOT}/space-ops-apps}"
POSTGRES_CONTAINER="${POSTGRES_CONTAINER:-telemetry-postgres}"
POSTGRES_USER="${POSTGRES_USER:-telemetry}"
CONTROL_PLANE_DB="${CONTROL_PLANE_DB:-control_plane_db}"
UNIT_ID="${PREVIEW_BANNER_UNIT_ID:-mission-control-frontend-shell}"
PREVIEW_BRANCH="${PREVIEW_BANNER_BRANCH:-preview/local-preview-banner}"
BASELINE_BRANCH="${PREVIEW_BANNER_BASELINE_BRANCH:-main}"
PREVIEW_DEPLOYMENT_ID="${PREVIEW_BANNER_PREVIEW_DEPLOYMENT_ID:-dep_local_preview_banner}"
BASELINE_DEPLOYMENT_ID="${PREVIEW_BANNER_BASELINE_DEPLOYMENT_ID:-dep_local_preview_banner_baseline}"

usage() {
  cat <<'EOF'
Usage:
  ./scripts/preview-banner-demo.sh activate   # seed a local frontend_shell preview context
  ./scripts/preview-banner-demo.sh baseline   # switch the local frontend_shell context back to main
  ./scripts/preview-banner-demo.sh delete     # remove the local demo frontend_shell rows
  ./scripts/preview-banner-demo.sh status     # print current preview context

This is a local/demo helper for the preview runtime banner. It does not deploy a
new Mission Control frontend container. It seeds the control-plane registry so
the existing local Mission Control UI can exercise the shell banner path.
EOF
}

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "error: required command '$1' is not installed" >&2
    exit 1
  fi
}

validate_ref() {
  local label="$1"
  local value="$2"
  if [[ ! "${value}" =~ ^[A-Za-z0-9._/-]+$ ]]; then
    echo "error: ${label} contains unsupported characters: ${value}" >&2
    exit 1
  fi
}

current_apps_commit() {
  if [[ -d "${APPS_ROOT}/.git" ]]; then
    git -C "${APPS_ROOT}" rev-parse HEAD
  else
    echo "local-demo"
  fi
}

psql_exec() {
  docker exec -i "${POSTGRES_CONTAINER}" psql -q -v ON_ERROR_STOP=1 -U "${POSTGRES_USER}" -d "${CONTROL_PLANE_DB}" "$@"
}

seed_context() {
  local active_branch="$1"
  local active_deployment_id="$2"
  local commit_sha="$3"
  local runtime_ref
  local discovery

  runtime_ref='{"service_name":"mission-control-ui","transport":{"scheme":"http","host":"mission-control-ui","port":3000},"health":{"path":"/health"},"proxy":{"base_path":""}}'
  discovery='{"preview_banner_demo":true,"description":"Local demo frontend shell context for the preview runtime banner."}'

  psql_exec \
    -v unit_id="${UNIT_ID}" \
    -v active_branch="${active_branch}" \
    -v active_deployment_id="${active_deployment_id}" \
    -v preview_deployment_id="${PREVIEW_DEPLOYMENT_ID}" \
    -v baseline_deployment_id="${BASELINE_DEPLOYMENT_ID}" \
    -v commit_sha="${commit_sha}" \
    -v runtime_ref="${runtime_ref}" \
    -v discovery="${discovery}" <<'SQL'
INSERT INTO managed_units (
  unit_id,
  display_name,
  package_owner,
  runtime_kind,
  runtime_template,
  source_path,
  deployment_status,
  health_status,
  discovery_metadata_json,
  delete_eligible,
  created_at,
  updated_at
) VALUES (
  :'unit_id',
  'Mission Control Frontend Shell',
  'space-ops-apps',
  'frontend_shell',
  'frontend-shell',
  'project/space-ops-apps/mission-control-ui',
  'healthy',
  'passing',
  :'discovery'::json,
  false,
  now(),
  now()
) ON CONFLICT (unit_id) DO UPDATE SET
  display_name = EXCLUDED.display_name,
  package_owner = EXCLUDED.package_owner,
  runtime_kind = EXCLUDED.runtime_kind,
  runtime_template = EXCLUDED.runtime_template,
  source_path = EXCLUDED.source_path,
  deployment_status = EXCLUDED.deployment_status,
  health_status = EXCLUDED.health_status,
  discovery_metadata_json = EXCLUDED.discovery_metadata_json,
  delete_eligible = EXCLUDED.delete_eligible,
  updated_at = now();

INSERT INTO deployments (
  deployment_id,
  unit_id,
  branch,
  commit_sha,
  status,
  health_status,
  runtime_ref,
  delete_eligible,
  requested_at
) VALUES (
  :'baseline_deployment_id',
  :'unit_id',
  'main',
  :'commit_sha',
  'healthy',
  'passing',
  :'runtime_ref'::json,
  false,
  now()
) ON CONFLICT (deployment_id) DO UPDATE SET
  unit_id = EXCLUDED.unit_id,
  branch = EXCLUDED.branch,
  commit_sha = EXCLUDED.commit_sha,
  status = EXCLUDED.status,
  health_status = EXCLUDED.health_status,
  runtime_ref = EXCLUDED.runtime_ref,
  delete_eligible = EXCLUDED.delete_eligible;

INSERT INTO deployments (
  deployment_id,
  unit_id,
  branch,
  commit_sha,
  status,
  health_status,
  runtime_ref,
  delete_eligible,
  requested_at
) VALUES (
  :'preview_deployment_id',
  :'unit_id',
  :'active_branch',
  :'commit_sha',
  'healthy',
  'passing',
  :'runtime_ref'::json,
  false,
  now()
) ON CONFLICT (deployment_id) DO UPDATE SET
  unit_id = EXCLUDED.unit_id,
  branch = EXCLUDED.branch,
  commit_sha = EXCLUDED.commit_sha,
  status = EXCLUDED.status,
  health_status = EXCLUDED.health_status,
  runtime_ref = EXCLUDED.runtime_ref,
  delete_eligible = EXCLUDED.delete_eligible;

UPDATE managed_units
SET active_deployment_id = :'active_deployment_id',
    deployment_status = 'healthy',
    health_status = 'passing',
    updated_at = now()
WHERE unit_id = :'unit_id';
SQL
}

delete_context() {
  psql_exec -v unit_id="${UNIT_ID}" <<'SQL'
DELETE FROM unit_health_snapshots WHERE unit_id = :'unit_id';
DELETE FROM deployment_events WHERE deployment_id IN (
  SELECT deployment_id FROM deployments WHERE unit_id = :'unit_id'
);
DELETE FROM deployments WHERE unit_id = :'unit_id';
DELETE FROM managed_units WHERE unit_id = :'unit_id';
SQL
}

print_status() {
  if command -v curl >/dev/null 2>&1; then
    curl -sS http://localhost:8100/registry/frontend-runtime/preview-context
    echo
  else
    psql_exec -v unit_id="${UNIT_ID}" <<'SQL'
SELECT unit_id, runtime_kind, runtime_template, active_deployment_id
FROM managed_units
WHERE unit_id = :'unit_id';
SQL
  fi
}

require_command docker
validate_ref "unit id" "${UNIT_ID}"
validate_ref "preview branch" "${PREVIEW_BRANCH}"
validate_ref "baseline branch" "${BASELINE_BRANCH}"
validate_ref "preview deployment id" "${PREVIEW_DEPLOYMENT_ID}"
validate_ref "baseline deployment id" "${BASELINE_DEPLOYMENT_ID}"

case "${COMMAND}" in
  activate)
    seed_context "${PREVIEW_BRANCH}" "${PREVIEW_DEPLOYMENT_ID}" "$(current_apps_commit)"
    print_status
    ;;
  baseline|deactivate|revert)
    seed_context "${BASELINE_BRANCH}" "${BASELINE_DEPLOYMENT_ID}" "$(current_apps_commit)"
    print_status
    ;;
  delete|clean|cleanup)
    delete_context
    print_status
    ;;
  status)
    print_status
    ;;
  --help|-h|help)
    usage
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
