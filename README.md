# Space Ops Kernel

Layer 1 runtime orchestration for the Space Ops local stack.

Extraction baseline: `c2-infra` commit `7b4f15ace9895c440ad89a9a460566c78135c57b` (`phase1-layer-split-baseline-2026-04-20`).

## Role

This repository owns Docker Compose wiring, service environment values, startup ordering, health checks, ports, volumes, and database bootstrap SQL. It does not own telemetry business logic, API route implementations, database schema definitions, frontend code, simulator logic, adapter logic, or vehicle configuration content.

Expected sibling checkout layout:

```text
space-ops/
  c2-infra/
  space-ops-kernel/
  space-ops-platform/
  space-ops-apps/
```

## Local Stack

Start the split stack from this repository:

```bash
docker compose up -d
```

This starts:

- `postgres` on port `5432`
- `platform-api` on port `8000`
- `mission-control-ui` on port `3000`
- `simulator` on port `8001`
- `simulator2` on port `8002`
- `satnogs-adapter`

Migrations run as part of the platform API container startup.

## Runtime Wiring

Compose builds service images from sibling repositories:

- `../space-ops-platform` for `platform-api`
- `../space-ops-apps/mission-control-ui` for `mission-control-ui`
- `../space-ops-apps/simulator` for `simulator` and `simulator2`
- `../space-ops-apps/satnogs_adapter` for `satnogs-adapter`

The selected Layer 3 vehicle configuration bundle is mounted into platform and app runtimes at `/app/vehicle-configurations`, with `VEHICLE_CONFIG_ROOT=/app/vehicle-configurations`.

Common environment values:

- `DATABASE_URL=postgresql://telemetry:telemetry@postgres:5432/telemetry_db`
- `NEXT_PUBLIC_API_URL=http://localhost:8000` by default
- `API_SERVER_URL=http://platform-api:8000`
- `CORS_ORIGIN_REGEX=^http://[^/]+:3000$`
- `SATNOGS_API_TOKEN` optional

## Useful Commands

```bash
docker compose config
docker compose build
docker compose up -d
docker compose ps
docker compose logs -f platform-api
```
