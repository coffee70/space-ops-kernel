---
title: Layer 1 Role
layer: kernel
audience: ai-engineer
topics:
  - runtime-orchestration
  - repo-boundaries
  - local-stack
status: mvp
last_verified: 2026-06-01
---

# Layer 1 Role

## Purpose

This doc helps the AI Engineer decide whether a change belongs in Layer 1.

## Applies To

`space-ops-kernel`, Docker Compose, edge proxy routing, control plane runtime orchestration, managed runtime manifests, deployment state, and validation entrypoints.

## Core Concepts

Layer 1 is the runtime orchestration layer.

It owns:
- Docker Compose wiring
- local stack startup
- edge proxy routing
- control plane
- managed runtime deployment
- unit manifests
- runtime registry
- preview deployment state
- validation entrypoints
- service startup ordering
- shared local environment values

It does not own:
- telemetry business logic
- backend API route implementations
- database schemas owned by platform services
- frontend application implementation
- simulator business behavior
- vehicle configs

## Procedure

Change Layer 1 when the task is about starting, routing, deploying, validating, or registering runtime units. Use Layer 2 for backend behavior and Layer 3 for Mission Control UI behavior.

## Do Not Assume

Do not place telemetry semantics, simulator behavior, or frontend application code in Layer 1 just because those capabilities run inside the local stack.

## Validation

Layer 1 validation usually runs through `./scripts/validate-node.sh`, `./scripts/validate-playwright.sh smoke`, control-plane tests, or route checks through `http://localhost:8080`.

## Failure Modes

If a service runs but is unreachable through the browser, check edge proxy routing and runtime registry state before changing app or backend code.

## Related Docs

- [Local Stack](./local-stack.md)
- [Managed Unit Manifests](../platform/managed-unit-manifests.md)
- [Deployment Lifecycle](../platform/deployment-lifecycle.md)
