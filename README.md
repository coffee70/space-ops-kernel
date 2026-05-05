# Space Ops Kernel

Layer 1 runtime orchestration for the Space Ops local stack.

Extraction baseline: `c2-infra` commit `7b4f15ace9895c440ad89a9a460566c78135c57b` (`phase1-layer-split-baseline-2026-04-20`).

## Documentation map (split checkout)

These three repositories assume a sibling directory layout (`space-ops-kernel`, `space-ops-platform`, `space-ops-apps`). **Read the sibling docs before running cross-service checks** so you pick the intended environment (host vs Compose vs Docker runner).

| Area | Humans | Agents / automation |
|------|--------|---------------------|
| **This repo — Compose, bootstrap, validation scripts** | this file | [AGENTS.md](./AGENTS.md) |
| **Layer 2 — platform APIs, backend pytest, agent-runtime service** | [../space-ops-platform/README.md](../space-ops-platform/README.md) | [../space-ops-platform/AGENTS.md](../space-ops-platform/AGENTS.md) |
| **Layer 3 — Mission Control UI, apps, Playwright workspace** | [../space-ops-apps/README.md](../space-ops-apps/README.md) | [../space-ops-apps/AGENTS.md](../space-ops-apps/AGENTS.md) |
| Playwright tooling details | [../space-ops-apps/tools/playwright/README.md](../space-ops-apps/tools/playwright/README.md) | — |

## Role

This repository owns Docker Compose wiring, service environment values, startup ordering, health checks, ports, volumes, and database bootstrap SQL. It does not own telemetry business logic, API route implementations, database schema definitions, frontend code, simulator logic, adapter logic, or vehicle configuration content.

Expected sibling checkout layout:

```text
space-ops/
  space-ops-kernel/
  space-ops-platform/
  space-ops-apps/
```

## Local stack

Start the split stack from this repository:

```bash
docker compose up -d
```

This starts:

- `postgres` on port `5432`
- `platform-api` on port `8000`
- `control-plane` on port `8100`
- `mission-control-ui` on port `3000`
- managed `satnogs-adapter-service` through the control-plane bootstrap manifest
- managed simulator services through the control-plane bootstrap manifests

Migrations run as part of service startup through Alembic for both backend services.

## Running tests

### When to use what

| Goal | Canonical entry point | Notes |
|------|------------------------|-------|
| **Node / TS — agent runtime + Mission Control** | `./scripts/validate-node.sh` | Runs **`npm ci` inside a Linux Node Docker image**, then agent-runtime `build` + `test` and Mission Control `npm run validate`. Use this instead of bare `npm test`/`npm run validate` on the host if `node_modules` might be from another OS/arch (copying deps from Compose builds is the usual culprit). Override image with `NODE_IMAGE`. |
| **Playwright — browser/E2E** | `./scripts/validate-playwright.sh …` | Runs **`npm ci` inside the upstream Playwright image** and attaches the container to the Compose Docker network. Default base URL `http://mission-control-ui:3000`; see script env vars. **`smoke`** is the usual quick target. Full options: `./scripts/validate-playwright.sh help`. |
| **Python — platform API** | [../space-ops-platform/README.md](../space-ops-platform/README.md) | `../space-ops-platform/scripts/run-backend-tests.sh` |
| **Python — control-plane (this repo)** | `./scripts/run-control-plane-tests.sh` | Needs reachable **Postgres** and a working **`git`** on the runner; fixtures create ephemeral DBs. |
| **Python — simulator** | [../space-ops-platform/README.md](../space-ops-platform/README.md) | `../space-ops-platform/scripts/run-backend-tests.sh backend/tests/simulator` |

Rough “confidence ladder”:

1. `docker compose up -d postgres` — only if you need local DB-backed tests (platform or control-plane).
2. `./scripts/validate-node.sh` — Node/TS correctness without Playwright/Browser.
3. Bring up UI/API as needed — Playwright prerequisites (often `docker compose up -d`).
4. `./scripts/validate-playwright.sh smoke` (after UI is built with **service** URLs reachable from the Playwright container — see Playwright subsection below).

### Control-plane pytest (`space-ops-kernel/control-plane/tests`)

Integration tests migrate against a disposable database and exercise git-backed workflows.

**Canonical** — from **`space-ops-kernel`** (venv at **`control-plane/.venv`**, matches `.gitignore`):

```bash
# Ensure Postgres matches how you compose (telemetry user from default compose env):
docker compose up -d postgres

./scripts/run-control-plane-tests.sh
```

The script defaults `KERNEL_TEST_DATABASE_URL` to `postgresql://telemetry:telemetry@localhost:5432/postgres`. Override when your Postgres URL differs:

```bash
KERNEL_TEST_DATABASE_URL='postgresql://user:pass@host:5432/postgres' ./scripts/run-control-plane-tests.sh -q
```

Ad-hoc (manual venv):

```bash
cd control-plane
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
KERNEL_TEST_DATABASE_URL=postgresql://telemetry:telemetry@localhost:5432/postgres pytest tests
```

`DATABASE_URL` or `KERNEL_TEST_DATABASE_URL` must point at a Postgres instance where tests may `CREATE DATABASE` helpers.

### Playwright prerequisites (Compose network)

Containers address each other **by Compose service names**, not `localhost`. Before running `./scripts/validate-playwright.sh`, rebuild/start UI with URLs the browser runner can resolve:

```bash
NEXT_PUBLIC_API_URL=http://platform-api:8000 \
NEXT_PUBLIC_CONTROL_PLANE_URL=http://control-plane:8100 \
docker compose up -d --build mission-control-ui
```

Further nuance lives in [../space-ops-apps/tools/playwright/README.md](../space-ops-apps/tools/playwright/README.md).

### Ad-hoc Node commands in Docker (advanced)

The repo does **not** ship per-package runner scripts besides `validate-node.sh`. To run a narrower command yourself, reuse the **same mounts and image** pattern as `./scripts/validate-node.sh`:

```bash
WORKSPACE_ROOT=$(cd .. && pwd)   # directory that contains space-ops-kernel ../ space-ops-platform ../ space-ops-apps
docker run --rm \
  --user "$(id -u):$(id -g)" \
  --volume "${WORKSPACE_ROOT}:/workspace" \
  --workdir /workspace \
  node:20.19-alpine \
  sh -lc 'cd /workspace/space-ops-apps/mission-control-ui && npm ci && npm run test:runtime'
```

## Runtime wiring

Compose builds service images from sibling repositories:

- `../space-ops-platform` for `platform-api`
- `../space-ops-apps/mission-control-ui` for `mission-control-ui`
SatNOGS and the telemetry simulators are deployed as managed Layer 2 services from `../space-ops-platform`.

Managed platform services read vehicle configuration resources from `/app/platform/backend/resources/vehicle-configurations`.

Common environment values:

- `platform-api DATABASE_URL=postgresql://telemetry:telemetry@postgres:5432/telemetry_db`
- `control-plane DATABASE_URL=postgresql://telemetry:telemetry@postgres:5432/control_plane_db`
- `NEXT_PUBLIC_API_URL=http://localhost:8000` by default
- `API_SERVER_URL=http://platform-api:8000`
- `CORS_ORIGIN_REGEX=^http://[^/]+:3000$`
- `SATNOGS_API_TOKEN` optional

## Useful Compose commands

```bash
docker compose config
docker compose build
docker compose up -d
docker compose ps
docker compose logs -f platform-api
```

## Useful validation commands

```bash
./scripts/validate-node.sh
./scripts/validate-playwright.sh smoke
./scripts/run-control-plane-tests.sh
```

For Playwright-only help (targets / env overrides):

```bash
./scripts/validate-playwright.sh help
```
