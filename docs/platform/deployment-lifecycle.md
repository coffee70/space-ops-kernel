---
title: Deployment Lifecycle
layer: kernel
audience: ai-engineer
topics:
  - deployments
  - runtime-units
  - failure-diagnosis
status: mvp
last_verified: 2026-06-01
---

# Deployment Lifecycle

## Purpose

This doc explains what happens when a managed unit is deployed and how to diagnose failures by phase.

## Applies To

Control-plane deployments for Layer 1 managed runtime units.

## Core Concepts

A deployment turns a managed source branch and commit into a running runtime unit.

Lifecycle phases:

1. submission
2. queueing
3. manifest load
4. source materialization
5. build
6. runtime deployment
7. health check
8. registry update
9. old runtime teardown
10. final status

Important distinction: `deployed` does not mean operator-ready.

A change may be:
- deployed
- healthy
- registered
- route-valid
- operator-validated

## Procedure

Follow the lifecycle status and collect evidence for the exact phase that failed. Do not skip route or UI validation after health checks pass.

## Do Not Assume

Do not claim success until validation evidence exists.

## Validation

Confirm deployment status, health status, registry metadata, expected route response, and the operator workflow path.

## Failure Modes

| Symptom | Likely Layer |
|---|---|
| manifest cannot load | unit manifest |
| Docker build fails | build spec or source code |
| container starts then fails health | runtime code or health endpoint |
| service is healthy but not callable through app | registry/proxy/gateway |
| frontend route loads but app unavailable | app registration or loader manifest |
| preview banner mismatches active code | multi-unit preview consistency |

## Related Docs

- [Managed Unit Manifests](./managed-unit-manifests.md)
- [Validation Gates](./validation-gates.md)
- [Multi-Unit Preview Consistency Runbook](../runbooks/multi-unit-preview-consistency.md)
