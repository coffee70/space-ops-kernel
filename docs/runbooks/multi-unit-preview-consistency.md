---
title: Multi-Unit Preview Consistency Runbook
layer: kernel
audience: ai-engineer
topics:
  - preview-deployments
  - multi-unit-capabilities
  - recovery
status: mvp
last_verified: 2026-06-01
---

# Multi-Unit Preview Consistency Runbook

## Purpose

This runbook helps diagnose and recover from inconsistent preview state across related runtime units.

## Applies To

Capabilities that span a backend service, frontend app, frontend shell, registry metadata, or other managed runtime units.

## Core Concepts

Symptoms:
- Preview banner says one branch is active.
- Backend behavior reflects a different branch or commit.
- Frontend shell and app runtime appear out of sync.
- The app route exists but service calls fail.
- A fix branch was deployed for one unit but not the others.

Meaning: the logical capability spans multiple runtime units, but not all related units are running the same intended change set.

## Procedure

Evidence to collect for each related unit:
- unit ID
- runtime kind
- active branch
- active commit
- deployment status
- health status
- registry entry
- route validation result

Safe next actions:

1. Identify all units that belong to the logical capability.
2. Compare active branch/commit for each unit.
3. Redeploy stale units from the intended branch/commit.
4. Re-run validation for backend and frontend paths.
5. Update the operator-facing summary with exact units and commits.

## Do Not Assume

Do not assume:
- one preview means all units are previewed
- frontend and backend are on the same branch
- a healthy backend means the frontend points to it
- a route-visible app means loader/build state is current

## Validation

The preview is consistent only when every affected unit is running the intended branch/commit and the operator path validates through the edge proxy.

## Failure Modes

Partial preview deployment can produce correct-looking UI chrome with stale backend behavior, or a healthy backend that the active frontend never calls.

## Related Docs

- [Preview Deployments](../platform/preview-deployments.md)
- [Validation Gates](../platform/validation-gates.md)
