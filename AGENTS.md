# Agent instructions

**Read alongside:** this layer is meaningless without the sibling checkouts.

| Repository | Humans | Agents / automation |
|------------|--------|---------------------|
| `space-ops-kernel` | [README.md](./README.md) | this file |
| `space-ops-platform` | [../space-ops-platform/README.md](../space-ops-platform/README.md) | [../space-ops-platform/AGENTS.md](../space-ops-platform/AGENTS.md) |
| `space-ops-apps` | [../space-ops-apps/README.md](../space-ops-apps/README.md) | [../space-ops-apps/AGENTS.md](../space-ops-apps/AGENTS.md) |

If you touch tests, Compose, URLs, or anything that crosses services, skim all three READMEs (and AGENTS files) so you do not run commands in the wrong environment.

## Repo role (Layer 1)

Keep changes scoped to runtime orchestration: Compose services, environment wiring, health/dependency ordering, ports, volumes, managed runtime manifests, and database bootstrap SQL.

Do not add telemetry business logic, FastAPI route code, database schema definitions, frontend code, simulator code, adapter code, or concrete vehicle configuration assets here. Adapter code, simulator code, and operational vehicle resources belong to Layer 2.

## How to run tests (canonical)

Assume the sibling layout (`space-ops-kernel`, `space-ops-platform`, `space-ops-apps` next to each other).

1. **Node / TypeScript (agent runtime + Mission Control `validate`):** run inside Docker so `npm ci` installs the correct platform-specific native binaries (`esbuild`, etc.). Do not rely on a host `node_modules` tree copied from Linux containers or another OS.

   ```bash
   ./scripts/validate-node.sh
   ```

   Optional image override: `NODE_IMAGE` (default `node:20.19-alpine`).

2. **Browser / Playwright:** run inside the Playwright Docker image attached to the **Compose Docker network**, not bare metal against `localhost`, unless you deliberately override URLs for local debugging.

   ```bash
   ./scripts/validate-playwright.sh smoke   # minimal check
   ./scripts/validate-playwright.sh test    # full suite selection (see script help)
   ```

   Prerequisites: Compose stack reachable on the expected network (`PLAYWRIGHT_DOCKER_NETWORK`, default `space-ops-kernel_default`), UI built with browser-reachable API URLs (`README.md` Testing section).

3. **Python — platform backend (Layer 2):** `../space-ops-platform/scripts/run-backend-tests.sh` (see Layer 2 README). Layer 1 does not wrap this today.

4. **Python — control-plane in this repo:** integration tests create temporary databases via Postgres and shell out to `git`. Run **`./scripts/run-control-plane-tests.sh`** from **`space-ops-kernel`** with Postgres available (typically `docker compose up -d postgres` first); uses gitignored **`control-plane/.venv`**. Details: `README.md`.

5. **Python — simulator / SatNOGS adapter:** `../space-ops-platform/scripts/run-backend-tests.sh backend/tests/simulator backend/tests/adapters/satnogs` (see Layer 2 README).

Agents should cite these entry points in summaries instead of improvised one-off `pytest`/`npm test` paths on the host when the canonical path exists.
