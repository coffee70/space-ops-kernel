# Agent Instructions

This repository is Layer 1. Keep changes scoped to runtime orchestration: Compose services, environment wiring, health/dependency ordering, ports, volumes, and database bootstrap SQL.

Do not add telemetry business logic, FastAPI route code, database schema definitions, frontend code, simulator code, adapter code, or concrete vehicle configuration assets here. Those belong to sibling Layer 2 and Layer 3 repositories.
