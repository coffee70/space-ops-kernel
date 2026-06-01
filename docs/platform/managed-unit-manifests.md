---
title: Managed Unit Manifests
layer: kernel
audience: ai-engineer
topics:
  - managed-units
  - manifests
  - runtime-templates
status: mvp
last_verified: 2026-06-01
---

# Managed Unit Manifests

## Purpose

This doc explains what a managed runtime unit is and how its manifest describes build, run, health, service, and application behavior.

## Applies To

Layer 1 unit manifests for backend services, frontend native applications, embedded applications, and the Mission Control frontend shell.

## Core Concepts

Managed units are runtime-deployable capabilities controlled by Layer 1.

A unit can represent:
- backend service
- frontend native application
- frontend embedded application
- frontend shell

Unit manifests live under:

```text
manifests/units/{unit_id}.yaml
```

Important fields:
- `unit_id`
- `runtime_kind`
- `template`
- `source`
- `build`
- `run`
- `health`
- `service`
- `application`

| Runtime Kind | Meaning |
|---|---|
| `service` | Backend managed service |
| `frontend_application` | Native or embedded app mounted into Mission Control |
| `frontend_shell` | Mission Control shell itself |

## Procedure

Use service-compatible templates for services, frontend application templates for native or embedded apps, and the frontend shell template for Mission Control shell deployments.

Example service unit:

```yaml
unit_id: example-advisory-service
runtime_kind: service
template: python-service
source:
  repo: space-ops-platform
  path: backend/services/example-advisory-service
build:
  dockerfile: Dockerfile
run:
  port: 8080
health:
  path: /health
service:
  service_slug: example-advisory
  category: advisory
  api_base_path: /services/example-advisory
```

## Do Not Assume

Services must not define frontend app metadata. Frontend applications must define application metadata and native or embedded app behavior. A logical capability may require more than one unit.

## Validation

Validate a manifest by confirming it loads, builds with the selected template, passes health checks, registers expected metadata, and responds through the expected route.

## Failure Modes

Wrong runtime kind, missing app metadata, incompatible template choice, or stale route metadata can make a healthy container unavailable to operators.

## Related Docs

- [Deployment Lifecycle](./deployment-lifecycle.md)
- [Preview Deployments](./preview-deployments.md)
- [Validation Gates](./validation-gates.md)
