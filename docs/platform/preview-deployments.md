---
title: Preview Deployments
layer: kernel
audience: ai-engineer
topics:
  - preview-deployments
  - runtime-units
  - revert
status: mvp
last_verified: 2026-06-01
---

# Preview Deployments

## Purpose

This doc explains preview deployment and revert behavior for managed runtime units.

## Applies To

AI Engineer preview changes deployed through Layer 1 control-plane runtime units.

## Core Concepts

Preview deployments let the AI Engineer deploy branch/commit changes for operator review without promoting them as baseline.

A preview deployment requires:
- target unit ID
- branch
- commit SHA
- changed files
- summary

For frontend applications, also include the target application ID when applicable.

Reverting restores the previous baseline runtime for the target unit.

Preview state is per runtime unit.

A logical capability may include:
- backend service unit
- frontend app unit
- frontend shell changes
- registry/application metadata changes

The preview is only trustworthy if all related units are deployed from the intended branch/commit set.

## Procedure

Resolve every affected runtime unit before deploying a preview. After deployment, compare branch and commit state for each unit and validate the operator path through the edge proxy.

## Do Not Assume

Do not assume that a preview banner for one unit means every related unit is running the same branch or commit.

For multi-unit capabilities, check every affected runtime unit.

## Validation

Validate backend routes, frontend application routes, registry state, and the visible operator workflow for the intended branch and commit set.

## Failure Modes

Inconsistent preview state can leave a frontend shell on one branch, a native app on another, and a backend service on baseline.

## Related Docs

- [Multi-Unit Preview Consistency Runbook](../runbooks/multi-unit-preview-consistency.md)
- [Validation Gates](./validation-gates.md)
