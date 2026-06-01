---
title: Validation Gates
layer: kernel
audience: ai-engineer
topics:
  - validation
  - deployment-evidence
  - operator-readiness
status: mvp
last_verified: 2026-06-01
---

# Validation Gates

## Purpose

This doc defines what "done" means after deployment.

## Applies To

Backend services, frontend applications, runtime applications, and Mission Control operator workflows.

## Core Concepts

The AI Engineer must distinguish these states:

| State | Meaning |
|---|---|
| deployed | runtime deployment was attempted and completed |
| healthy | health check passed |
| registered | registry contains expected service/app metadata |
| route-valid | expected HTTP/UI route responds correctly |
| operator-ready | the expected operator workflow was validated |

## Procedure

For a backend service:
- check deployment status
- check service registry
- check health endpoint
- call expected gateway route
- confirm response shape

For a frontend app:
- check deployment status
- check application registry
- check runtime application route
- check `/apps/{applicationId}`
- run browser validation when needed

## Do Not Assume

Do not claim a capability is ready until the relevant route or UI path is validated.

## Validation

Use gateway-relative HTTP validation for backend services and edge-proxy browser validation for user-facing workflows.

## Failure Modes

Deploy completion without route validation can hide broken gateway paths, stale loader manifests, or a frontend pointing at the wrong backend route.

## Related Docs

- [Deployment Lifecycle](./deployment-lifecycle.md)
- [Preview Deployments](./preview-deployments.md)
